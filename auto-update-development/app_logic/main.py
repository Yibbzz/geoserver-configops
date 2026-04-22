"""
OS Dataset Ingestion POC
========================
Stages:
  1. Discover latest version
  2. Compare with previously loaded version
  3. Download if newer
  4. Verify checksum
  5. Extract
  6. Load into staging schema
  7. Validate geometry / row counts
  8. Backup production tables
  9. Promote staging -> live
  10. Record success in metadata table
"""

import os
import hashlib
import zipfile
import shutil
import requests
import psycopg2
import subprocess
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_API   = "https://api.os.uk/downloads/v1/products"
API_KEY    = os.environ.get("OS_API_KEY", "")          # set in env if needed
WORK_DIR   = Path("/tmp/os_ingest")

DB = dict(
    dbname   = "gis",
    user     = "postgres",
    password = "postgres",
    host     = "db",
    port     = 5432,
)

# Format preference order — raster / non-spatial formats are absent from this list
# so they are silently skipped.
FORMAT_PREFERENCE = [
    "GeoPackage",
    "ESRI® Shapefile",
    "GML",
    "Vector Tiles",
    "MapInfo® TAB",
    "MapInfo",
]

# Only process these dataset IDs (leave empty [] to process ALL vector datasets)
DATASET_ALLOWLIST = []

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(stage: int, msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] STAGE {stage} | {msg}")


def get_conn():
    return psycopg2.connect(**DB)


def pick_format(available_formats: list[str]) -> str | None:
    """Return the best available format name, or None if nothing useful."""
    for preferred in FORMAT_PREFERENCE:
        if preferred in available_formats:
            return preferred
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_schema(cur, schema: str):
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


# ---------------------------------------------------------------------------
# Bootstrap – create the pipeline metadata table once
# ---------------------------------------------------------------------------

BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS public.os_ingest_log (
    id              SERIAL PRIMARY KEY,
    dataset_id      TEXT        NOT NULL,
    version         TEXT        NOT NULL,
    format_used     TEXT,
    status          TEXT        NOT NULL,   -- success | failed | skipped
    rows_loaded     BIGINT,
    sha256          TEXT,
    error           TEXT,
    started_at      TIMESTAMP   NOT NULL DEFAULT now(),
    finished_at     TIMESTAMP
);
"""

def bootstrap(conn):
    with conn.cursor() as cur:
        cur.execute(BOOTSTRAP_SQL)
    conn.commit()


# ---------------------------------------------------------------------------
# Stage 1 – discover latest version + download URL
# ---------------------------------------------------------------------------

def discover(product_id: str) -> dict | None:
    """
    Returns dict with keys: format, url, size, md5 (if available).
    Version is NOT fetched here — it lives on the product list object
    (p["version"]) and is passed in by the caller.
    """
    r = requests.get(
        f"{BASE_API}/{product_id}/downloads",
        params={"key": API_KEY} if API_KEY else {},
        timeout=30,
    )
    if r.status_code != 200:
        return None

    data = r.json()
    downloads = data if isinstance(data, list) else data.get("downloads", [])

    by_format: dict[str, dict] = {}
    for item in downloads:
        fmt = item.get("format", "")
        if fmt:
            by_format[fmt] = item

    chosen_fmt = pick_format(list(by_format.keys()))
    if chosen_fmt is None:
        return None

    item = by_format[chosen_fmt]
    return {
        "format": chosen_fmt,
        "url":    item.get("url") or item.get("downloadURL"),
        "size":   item.get("size"),
        "md5":    item.get("md5"),
    }


# ---------------------------------------------------------------------------
# Stage 2 – compare with previously loaded version
# ---------------------------------------------------------------------------

def last_loaded_version(conn, dataset_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT version FROM public.os_ingest_log
            WHERE dataset_id = %s AND status = 'success'
            ORDER BY finished_at DESC LIMIT 1
        """, (dataset_id,))
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Stage 3 – download
# ---------------------------------------------------------------------------

def download(url: str, dest: Path) -> Path:
    headers = {"key": API_KEY} if API_KEY else {}
    with requests.get(url, headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    return dest


# ---------------------------------------------------------------------------
# Stage 4 – verify checksum
# ---------------------------------------------------------------------------

def verify(path: Path, expected_md5: str | None) -> str:
    sha = sha256_file(path)
    if expected_md5:
        import hashlib
        got_md5 = hashlib.md5(open(path, "rb").read()).hexdigest()
        if got_md5 != expected_md5:
            raise ValueError(f"MD5 mismatch: expected {expected_md5}, got {got_md5}")
    return sha


# ---------------------------------------------------------------------------
# Stage 5 – extract
# ---------------------------------------------------------------------------

def extract(archive: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest_dir)
    else:
        raise ValueError(f"Unsupported archive format: {archive.suffix}")
    return dest_dir


def find_file(directory: Path, ext: str) -> Path | None:
    for p in directory.rglob(f"*{ext}"):
        return p
    return None


# ---------------------------------------------------------------------------
# Stage 6 – load into staging schema via ogr2ogr
# ---------------------------------------------------------------------------

def load_staging(dataset_id: str, extract_dir: Path, fmt: str, conn) -> str:
    """Load data into staging schema, return staging table name."""

    staging_schema = "staging"
    staging_table  = dataset_id.lower().replace("-", "_")

    # pick the right file extension
    ext_map = {
        "GeoPackage":       ".gpkg",
        "ESRI® Shapefile":  ".shp",
        "GML":              ".gml",
        "MapInfo® TAB":     ".tab",
        "MapInfo":          ".tab",
    }
    ext = ext_map.get(fmt)
    if ext is None:
        raise ValueError(f"No file extension mapped for format: {fmt}")

    src_file = find_file(extract_dir, ext)
    if src_file is None:
        raise FileNotFoundError(f"No {ext} file found under {extract_dir}")

    with conn.cursor() as cur:
        ensure_schema(cur, staging_schema)
        # drop any previous staging table for this dataset
        cur.execute(f'DROP TABLE IF EXISTS {staging_schema}."{staging_table}" CASCADE')
    conn.commit()

    dsn = (
        f"PG:dbname={DB['dbname']} user={DB['user']} "
        f"password={DB['password']} host={DB['host']} port={DB['port']}"
    )

    cmd = [
        "ogr2ogr",
        "-f", "PostgreSQL",
        dsn,
        str(src_file),
        "-nln", f"{staging_schema}.{staging_table}",
        "-lco", "OVERWRITE=YES",   # overwrite the specific layer, not the whole DS
        "-progress",
        "-t_srs", "EPSG:27700",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ogr2ogr failed:\n{result.stderr}")

    return f"{staging_schema}.{staging_table}"


# ---------------------------------------------------------------------------
# Stage 7 – validate
# ---------------------------------------------------------------------------

def validate(conn, table: str) -> int:
    schema, tbl = table.split(".")
    with conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM {schema}."{tbl}"')
        count = cur.fetchone()[0]

    if count == 0:
        raise ValueError(f"Staging table {table} is empty after load")

    # basic geometry validity check — discover the actual geometry column name first
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT f_geometry_column
            FROM geometry_columns
            WHERE f_table_schema = %s AND f_table_name = %s
            LIMIT 1
        """, (schema, tbl))
        row = cur.fetchone()

    if row:
        geom_col = row[0]
        with conn.cursor() as cur:
            try:
                cur.execute(f"""
                    SELECT COUNT(*) FROM {schema}."{tbl}"
                    WHERE NOT ST_IsValid("{geom_col}")
                """)
                invalid = cur.fetchone()[0]
                if invalid > 0:
                    print(f"  WARNING: {invalid} invalid geometries in {table}")
            except Exception as e:
                conn.rollback()   # must rollback or postgres aborts all further commands
                print(f"  WARNING: geometry validity check failed ({e})")

    return count


# ---------------------------------------------------------------------------
# Stage 8 – backup production table
# ---------------------------------------------------------------------------

def backup_production(conn, dataset_id: str):
    prod_schema    = "public"
    prod_table     = dataset_id.lower().replace("-", "_")
    backup_schema  = "backup"
    backup_table   = f"{prod_table}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    with conn.cursor() as cur:
        ensure_schema(cur, backup_schema)
        cur.execute(f"""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
            )
        """, (prod_schema, prod_table))
        exists = cur.fetchone()[0]

        if exists:
            cur.execute(f"""
                CREATE TABLE {backup_schema}."{backup_table}"
                AS SELECT * FROM {prod_schema}."{prod_table}"
            """)
            print(f"  Backed up to {backup_schema}.{backup_table}")
    conn.commit()


# ---------------------------------------------------------------------------
# Stage 9 – promote staging -> live
# ---------------------------------------------------------------------------

def promote(conn, staging_table: str, dataset_id: str):
    prod_table     = dataset_id.lower().replace("-", "_")
    staging_schema, staging_tbl = staging_table.split(".")

    with conn.cursor() as cur:
        # Drop existing production table first
        cur.execute(f'DROP TABLE IF EXISTS public."{prod_table}" CASCADE')

        # Move staging table into public schema
        cur.execute(f'ALTER TABLE {staging_schema}."{staging_tbl}" SET SCHEMA public')

        # Rename only if the staging table name differs from the target prod name
        if staging_tbl != prod_table:
            cur.execute(f'ALTER TABLE public."{staging_tbl}" RENAME TO "{prod_table}"')

    conn.commit()


# ---------------------------------------------------------------------------
# Stage 10 – record result
# ---------------------------------------------------------------------------

def record(conn, dataset_id, version, fmt, status, rows=None, sha=None, error=None, started_at=None):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.os_ingest_log
                (dataset_id, version, format_used, status, rows_loaded, sha256, error, started_at, finished_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        """, (dataset_id, version, fmt, status, rows, sha, error, started_at))
    conn.commit()


# ---------------------------------------------------------------------------
# Main pipeline per dataset
# ---------------------------------------------------------------------------

def process_dataset(conn, product: dict):
    dataset_id = product["id"]
    started_at = datetime.now()
    work = WORK_DIR / dataset_id
    work.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"Dataset: {dataset_id} — {product.get('name')}")
    print(f"{'='*60}")

    # --- Stage 1: discover ---
    log(1, "Discovering latest version...")
    info = discover(dataset_id)
    if info is None:
        log(1, "No usable vector format available — skipping")
        record(conn, dataset_id, "n/a", None, "skipped", started_at=started_at)
        return

    version    = product.get("version") or "unknown"
    fmt        = info["format"]
    url        = info["url"]
    log(1, f"Latest version={version}  format={fmt}")

    # --- Stage 2: compare ---
    log(2, "Checking previously loaded version...")
    prev = last_loaded_version(conn, dataset_id)
    if prev == version:
        log(2, f"Already at version {version} — skipping")
        record(conn, dataset_id, version, fmt, "skipped", started_at=started_at)
        return
    log(2, f"Previous={prev or 'none'}  → will update")

    # --- Stage 3: download ---
    archive_path = work / "download.zip"
    log(3, f"Downloading {url} ...")
    download(url, archive_path)
    log(3, f"Downloaded {archive_path.stat().st_size / 1e6:.1f} MB")

    # --- Stage 4: verify ---
    log(4, "Verifying checksum...")
    sha = verify(archive_path, info.get("md5"))
    log(4, f"SHA256={sha[:16]}...")

    # --- Stage 5: extract ---
    extract_dir = work / "extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    log(5, "Extracting archive...")
    extract(archive_path, extract_dir)
    log(5, f"Extracted to {extract_dir}")

    # --- Stage 6: load staging ---
    log(6, "Loading into staging schema...")
    staging_table = load_staging(dataset_id, extract_dir, fmt, conn)
    log(6, f"Loaded into {staging_table}")

    # --- Stage 7: validate ---
    log(7, "Validating...")
    rows = validate(conn, staging_table)
    log(7, f"{rows:,} rows — OK")

    # --- Stage 8: backup ---
    log(8, "Backing up production table...")
    backup_production(conn, dataset_id)

    # --- Stage 9: promote ---
    log(9, "Promoting staging -> live...")
    promote(conn, staging_table, dataset_id)
    log(9, "Done")

    # --- Stage 10: record ---
    log(10, "Recording success...")
    record(conn, dataset_id, version, fmt, "success",
           rows=rows, sha=sha, started_at=started_at)

    # cleanup
    shutil.rmtree(work, ignore_errors=True)
    print(f"✓ {dataset_id} updated to {version}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    bootstrap(conn)

    products = requests.get(BASE_API, params={"key": API_KEY} if API_KEY else {}, timeout=30).json()

    if DATASET_ALLOWLIST:
        products = [p for p in products if p["id"] in DATASET_ALLOWLIST]

    for product in products:
        try:
            process_dataset(conn, product)
        except Exception as e:
            print(f"  ERROR processing {product['id']}: {e}")
            conn.rollback()   # clear any aborted transaction before next DB op
            try:
                record(conn, product["id"], "unknown", None, "failed",
                       error=str(e)[:500], started_at=datetime.now())
            except Exception as re:
                print(f"  ERROR recording failure: {re}")
                conn.rollback()

    conn.close()
    print("\nAll done.")


if __name__ == "__main__":
    main()