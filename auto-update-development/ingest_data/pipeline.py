"""
pipeline.py
===========
General ingestion pipeline. Source-agnostic — expects datasets in the
standard format produced by any source module:

    {
        "id":      str,   # unique stable ID
        "name":    str,   # human readable label for logging
        "version": str,   # used to detect if a re-download is needed
        "format":  str,   # must match ext_map keys below
        "url":     str,   # direct download URL
        "size":    int,   # bytes (optional, for logging)
        "md5":     str,   # expected MD5 checksum (optional)
        "srs":     str,   # source CRS, e.g. "EPSG:27700" (optional, default EPSG:27700)
        "headers": dict,  # extra HTTP headers for the download, e.g. API key (optional)
    }

Stages:
  2.  Compare with previously loaded version
  3.  Download
  4.  Verify checksum
  5.  Extract
  6.  Load every layer of every data file into its own staging table
  7.  Validate geometry / row counts (all tables, before anything goes live)
  8.  Backup production tables (keeps the newest BACKUP_RETENTION copies)
  9.  Promote staging -> live (invalid geometries filtered on SELECT)
  9.5 Create GIST spatial index + ANALYZE
  10. Record result in metadata table
"""

import os
import re
import sys
import hashlib
import zipfile
import shutil
import subprocess
import psycopg2
import requests
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DOWNLOAD_DIR = Path("/tmp/os_ingest")

PROD_SCHEMA = os.environ.get("PROD_SCHEMA", "data_layers")
STAGING_SCHEMA = os.environ.get("STAGING_SCHEMA", "staging")
BACKUP_SCHEMA = os.environ.get("BACKUP_SCHEMA", "backup")
BACKUP_RETENTION = int(os.environ.get("BACKUP_RETENTION", 3))
DEFAULT_SRS = "EPSG:27700"

DB = dict(
    dbname=os.environ.get("DB_NAME", "gis"),
    user=os.environ.get("DB_USER", "postgres"),
    password=os.environ.get("DB_PASSWORD", "postgres"),
    host=os.environ.get("DB_HOST", "db"),
    port=int(os.environ.get("DB_PORT", 5432)),
)

# Maps the format string from source modules to a file extension for ogr2ogr
EXT_MAP = {
    "GeoPackage": ".gpkg",
    "ESRI® Shapefile": ".shp",
    "GML": ".gml",
    "MapInfo® TAB": ".tab",
    "MapInfo": ".tab",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(stage, msg: str, error: bool = False):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] STAGE {stage} | {msg}"
    if error:
        print(line, file=sys.stderr)
    else:
        print(line)


def get_conn():
    return psycopg2.connect(**DB)


def ensure_schema(cur, schema: str):
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


def get_geometry_column(conn, schema: str, table: str) -> str | None:
    """Look up the geometry column name for a given table from geometry_columns."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f_geometry_column
            FROM geometry_columns
            WHERE f_table_schema = %s AND f_table_name = %s
            LIMIT 1
            """,
            (schema, table),
        )
        row = cur.fetchone()
    return row[0] if row else None


def find_file(directory: Path, ext: str) -> Path | None:
    for p in directory.rglob(f"*{ext}"):
        return p
    return None


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
    rows_filtered   BIGINT,                 -- invalid geometries excluded on promote
    sha256          TEXT,
    error           TEXT,
    started_at      TIMESTAMP   NOT NULL DEFAULT now(),
    finished_at     TIMESTAMP
);
-- live tables a successful load produced, comma-separated (added for multi-table products)
ALTER TABLE public.os_ingest_log ADD COLUMN IF NOT EXISTS tables TEXT;
"""


def bootstrap(conn):
    with conn.cursor() as cur:
        cur.execute(BOOTSTRAP_SQL)
    conn.commit()


# ---------------------------------------------------------------------------
# Stage 2 – compare with previously loaded version
# ---------------------------------------------------------------------------


def last_loaded_version(conn, dataset_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT version FROM public.os_ingest_log
            WHERE dataset_id = %s AND status = 'success'
            ORDER BY finished_at DESC LIMIT 1
            """,
            (dataset_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Stage 3 – download
# ---------------------------------------------------------------------------


def download(url: str, dest: Path, headers: dict | None = None) -> Path:
    with requests.get(url, headers=headers or {}, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    return dest


# ---------------------------------------------------------------------------
# Stage 4 – verify checksum
# ---------------------------------------------------------------------------


def verify(path: Path, expected_md5: str | None) -> str:
    """Stream the file once, check the MD5 if given, and return its SHA256."""
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
            md5.update(chunk)
    if expected_md5 and md5.hexdigest().lower() != expected_md5.lower():
        raise ValueError(f"MD5 mismatch: expected {expected_md5}, got {md5.hexdigest()}")
    return sha.hexdigest()


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


# ---------------------------------------------------------------------------
# Stage 6 – load into staging schema via ogr2ogr
# ---------------------------------------------------------------------------



def table_name(*parts: str) -> str:
    """A PostgreSQL table name from a dataset ID and layer name: lowercase, a-z 0-9 _ only."""
    name = re.sub(r"[^a-z0-9]+", "_", "_".join(parts).lower()).strip("_")
    if not name or name[0].isdigit():
        name = f"t_{name}"
    return name[:63]


def list_layers(path: Path) -> list[str]:
    """Names of the layers in a data file, as GDAL reports them."""
    result = subprocess.run(["ogrinfo", "-ro", "-q", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ogrinfo failed on {path.name}:\n{result.stderr}")
    layers = []
    for line in result.stdout.splitlines():
        # e.g. "1: road_link (Multi Line String)" or "2: road_node"
        m = re.match(r"^\s*\d+: (.+?)(?: \([^()]*\))?\s*$", line)
        if m:
            layers.append(m.group(1))
    return layers


def source_layers(extract_dir: Path, fmt: str) -> list[tuple[Path, str]]:
    """Every (file, layer) in an extracted download, for the given format."""
    ext = EXT_MAP.get(fmt)
    if ext is None:
        raise ValueError(f"No file extension mapped for format: {fmt}")
    files = sorted(extract_dir.rglob(f"*{ext}"))
    if not files:
        raise FileNotFoundError(f"No {ext} file found under {extract_dir}")
    return [(f, layer) for f in files for layer in list_layers(f)]


def plan_tables(dataset_id: str, layers: list[tuple[Path, str]]) -> list[tuple[Path, str, str]]:
    """
    (file, layer, table) for every layer. A product with a single layer keeps
    the table name it always had (the dataset ID); with several, each layer
    gets <dataset>_<layer>.
    """
    if len(layers) == 1:
        f, layer = layers[0]
        return [(f, layer, table_name(dataset_id))]
    planned = [(f, layer, table_name(dataset_id, layer)) for f, layer in layers]
    tables = [t for _, _, t in planned]
    duplicates = sorted({t for t in tables if tables.count(t) > 1})
    if duplicates:
        raise ValueError(f"Two layers map to the same table name: {', '.join(duplicates)}")
    return planned


def load_staging(conn, src_file: Path, layer: str, table: str, srs: str = DEFAULT_SRS) -> str:
    """Load one layer of a data file into STAGING_SCHEMA.<table>."""
    with conn.cursor() as cur:
        ensure_schema(cur, STAGING_SCHEMA)
        cur.execute(f'DROP TABLE IF EXISTS {STAGING_SCHEMA}."{table}" CASCADE')
    conn.commit()

    dsn = (
        f"PG:dbname={DB['dbname']} user={DB['user']} "
        f"password={DB['password']} host={DB['host']} port={DB['port']}"
    )
    cmd = [
        "ogr2ogr", "-f", "PostgreSQL", dsn, str(src_file), layer,
        "-nln", f"{STAGING_SCHEMA}.{table}",
        "-lco", "OVERWRITE=YES",
        "-lco", "PRECISION=NO",
        "-lco", "GEOMETRY_NAME=geom",
        "-lco", "UNLOGGED=YES",
        "-nlt", "PROMOTE_TO_MULTI",
        "-s_srs", srs,
        "-t_srs", srs,
        "-gt", "65536",
        "--config", "PG_USE_COPY", "YES",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ogr2ogr failed for layer {layer}:\n{result.stderr}")
    elif result.stderr.strip():
        print(f"  ogr2ogr warnings ({layer}):\n{result.stderr.strip()}")

    return f"{STAGING_SCHEMA}.{table}"


# ---------------------------------------------------------------------------
# Stage 7 – validate
# ---------------------------------------------------------------------------


def validate(conn, table: str) -> tuple[int, int]:
    """Returns (total_rows, invalid_count). Does not modify the table."""
    schema, tbl = table.split(".")

    with conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM {schema}."{tbl}"')
        total = cur.fetchone()[0]

    if total == 0:
        raise ValueError(f"Staging table {table} is empty after load")

    geom_col = get_geometry_column(conn, schema, tbl)
    invalid = 0

    if geom_col:
        with conn.cursor() as cur:
            try:
                cur.execute(f"""
                    SELECT COUNT(*) FROM {schema}."{tbl}"
                    WHERE NOT ST_IsValid("{geom_col}")
                    """)
                invalid = cur.fetchone()[0]
                if invalid > 0:
                    log(
                        7,
                        f"{invalid:,} invalid geometries found — will be excluded on promote",
                    )
                else:
                    log(7, "All geometries valid")
            except Exception as e:
                conn.rollback()
                log(7, f"WARNING: geometry validity check failed ({e})", error=True)

    return total, invalid


# ---------------------------------------------------------------------------
# Stage 8 – backup production table
# ---------------------------------------------------------------------------


def backup_production(conn, prod_table: str):
    backup_table = f"{prod_table}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    with conn.cursor() as cur:
        ensure_schema(cur, BACKUP_SCHEMA)
        cur.execute(
            """
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
            )
            """,
            (PROD_SCHEMA, prod_table),  # was hardcoded "public"
        )
        exists = cur.fetchone()[0]
        if exists:
            cur.execute(f"""
                CREATE TABLE {BACKUP_SCHEMA}."{backup_table}"
                AS SELECT * FROM {PROD_SCHEMA}."{prod_table}"
                """)
    conn.commit()
    prune_backups(conn, prod_table)


def prune_backups(conn, prod_table: str):
    """Drop all but the newest BACKUP_RETENTION backups of a table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = %s AND table_name ~ %s
            ORDER BY table_name DESC
            """,
            (BACKUP_SCHEMA, f"^{prod_table}_[0-9]{{8}}_[0-9]{{6}}$"),
        )
        old = [row[0] for row in cur.fetchall()][BACKUP_RETENTION:]
        for table in old:
            cur.execute(f'DROP TABLE {BACKUP_SCHEMA}."{table}"')
    conn.commit()
    if old:
        log(8, f"Dropped {len(old)} old backup(s), keeping {BACKUP_RETENTION}")


# ---------------------------------------------------------------------------
# Stage 9 – promote staging -> live
# ---------------------------------------------------------------------------


def promote(conn, staging_table: str, prod_table: str) -> int:
    staging_schema, staging_tbl = staging_table.split(".")
    geom_col = get_geometry_column(conn, staging_schema, staging_tbl)

    with conn.cursor() as cur:
        ensure_schema(cur, PROD_SCHEMA)  # make sure schema exists
        cur.execute(f'DROP TABLE IF EXISTS {PROD_SCHEMA}."{prod_table}" CASCADE')

        if geom_col:
            cur.execute(f"""
                CREATE TABLE {PROD_SCHEMA}."{prod_table}"
                AS SELECT * FROM {staging_schema}."{staging_tbl}"
                WHERE ST_IsValid("{geom_col}")
                """)
        else:
            cur.execute(
                f'ALTER TABLE {staging_schema}."{staging_tbl}" SET SCHEMA {PROD_SCHEMA}'
            )
            if staging_tbl != prod_table:
                cur.execute(
                    f'ALTER TABLE {PROD_SCHEMA}."{staging_tbl}" RENAME TO "{prod_table}"'
                )
    conn.commit()

    if geom_col:
        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {PROD_SCHEMA}."{prod_table}"')
            promoted_count = cur.fetchone()[0]
        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {staging_schema}."{staging_tbl}"')
            staging_count = cur.fetchone()[0]
            # The live table is a copy, so the staging table is no longer needed
            cur.execute(f'DROP TABLE {staging_schema}."{staging_tbl}"')
        conn.commit()
        return staging_count - promoted_count

    return 0


# ---------------------------------------------------------------------------
# Stage 9.5 – create GIST spatial index + ANALYZE
# ---------------------------------------------------------------------------


def create_spatial_index(conn, prod_table: str):
    geom_col = get_geometry_column(conn, PROD_SCHEMA, prod_table)

    if geom_col is None:
        log(
            "9.5",
            f"No geometry column found for {PROD_SCHEMA}.{prod_table} — skipping index",
        )
        return

    index_name = f"{prod_table}_geom_idx"

    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS "{index_name}"
            ON {PROD_SCHEMA}."{prod_table}"
            USING GIST ("{geom_col}")
            """)
    conn.commit()
    log("9.5", f"Created GIST index '{index_name}' on column '{geom_col}'")

    with conn.cursor() as cur:
        cur.execute(f'ANALYZE {PROD_SCHEMA}."{prod_table}"')
    conn.commit()
    log("9.5", "ANALYZE complete — query planner statistics updated")


# ---------------------------------------------------------------------------
# Stage 10 – record result
# ---------------------------------------------------------------------------


def record(
    conn,
    dataset_id,
    version,
    fmt,
    status,
    rows=None,
    rows_filtered=None,
    sha=None,
    error=None,
    started_at=None,
    tables=None,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.os_ingest_log
                (dataset_id, version, format_used, status, rows_loaded,
                 rows_filtered, sha256, error, started_at, finished_at, tables)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
            """,
            (
                dataset_id,
                version,
                fmt,
                status,
                rows,
                rows_filtered,
                sha,
                error,
                started_at,
                ",".join(tables) if tables else None,
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Per-dataset orchestration
# ---------------------------------------------------------------------------


def process_dataset(conn, dataset: dict):
    """
    Runs all pipeline stages for a single dataset dict.
    The dict must conform to the standard source format.
    """
    dataset_id = dataset["id"]
    version = dataset["version"]
    fmt = dataset["format"]
    url = dataset["url"]
    started_at = datetime.now()

    work = DOWNLOAD_DIR / dataset_id
    work.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"Dataset: {dataset_id} — {dataset.get('name', dataset_id)}")
    print(f"{'='*60}")

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
    download(url, archive_path, dataset.get("headers"))
    log(3, f"Downloaded {archive_path.stat().st_size / 1e6:.1f} MB")

    # --- Stage 4: verify ---
    log(4, "Verifying checksum...")
    sha = verify(archive_path, dataset.get("md5"))
    log(4, f"SHA256={sha[:16]}...")

    # --- Stage 5: extract ---
    extract_dir = work / "extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    log(5, "Extracting archive...")
    extract(archive_path, extract_dir)
    log(5, f"Extracted to {extract_dir}")

    # --- Stage 6: load every layer into staging ---
    srs = dataset.get("srs") or DEFAULT_SRS
    planned = plan_tables(dataset_id, source_layers(extract_dir, fmt))
    log(6, f"{len(planned)} layer(s) to load: {', '.join(layer for _, layer, _ in planned)}")
    staged = []
    for src_file, layer, table in planned:
        staged.append((load_staging(conn, src_file, layer, table, srs), table))
        log(6, f"Loaded {layer} into {staged[-1][0]}")

    # --- Stage 7: validate all of them before anything goes live ---
    log(7, "Validating...")
    total_rows = 0
    for staging_table, _ in staged:
        rows, invalid = validate(conn, staging_table)
        total_rows += rows
        log(7, f"{staging_table}: {rows:,} rows, {invalid:,} invalid geometries to filter")

    # --- Stages 8, 9, 9.5: back up, promote and index each table ---
    rows_filtered = 0
    for staging_table, table in staged:
        log(8, f"Backing up {PROD_SCHEMA}.{table}...")
        backup_production(conn, table)
        log(9, f"Promoting {staging_table} -> {PROD_SCHEMA}.{table}...")
        filtered = promote(conn, staging_table, table)
        if filtered:
            log(9, f"Filtered out {filtered:,} invalid geometry rows")
        rows_filtered += filtered
        create_spatial_index(conn, table)

    # --- Stage 10: record ---
    log(10, "Recording success...")
    rows_loaded = total_rows - rows_filtered
    tables = [table for _, table in staged]
    record(
        conn,
        dataset_id,
        version,
        fmt,
        "success",
        rows=rows_loaded,
        rows_filtered=rows_filtered,
        sha=sha,
        started_at=started_at,
        tables=tables,
    )

    shutil.rmtree(work, ignore_errors=True)
    print(f"✓ {dataset_id} updated to {version}  ({rows_loaded:,} rows in {len(tables)} table(s): {', '.join(tables)})")


# ---------------------------------------------------------------------------
# Entry point — called by main.py with a pre-built list of datasets
# ---------------------------------------------------------------------------


def run_pipeline(datasets: list[dict]):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    bootstrap(conn)

    for dataset in datasets:
        try:
            process_dataset(conn, dataset)
        except Exception as e:
            log("ERR", f"ERROR processing {dataset['id']}: {e}", error=True)
            conn.rollback()
            try:
                record(
                    conn,
                    dataset["id"],
                    dataset.get("version", "unknown"),
                    dataset.get("format"),
                    "failed",
                    error=str(e)[:500],
                    started_at=datetime.now(),
                )
            except Exception as re:
                log("ERR", f"ERROR recording failure: {re}", error=True)
                conn.rollback()

    conn.close()
    print("\nAll done.")
