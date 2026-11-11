"""Loading tables into PostGIS: stage, check, then swap in, with backups.

Every layer is loaded with ogr2ogr into the staging schema under a unique
name, checked (invalid geometries dropped, row count), then all of a
dataset's tables go live in one transaction: each old table is renamed into
the backup schema and the new one renamed into place. Nothing is copied,
and if anything fails before the swap, every live table stays as it was.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2
import requests

from . import remote

LOG_TABLE = "public.os_ingest_log"  # the name the first ingest pipeline gave it; kept so history carries on
TIMEOUT = 120

# A date PostgreSQL rejects (shapefiles store "no date" as 0000/00/00): load dates as text instead
BAD_DATE = re.compile(r"date/time field value out of range|invalid input syntax for type (date|time|timestamp)|column \w+: \"0000", re.IGNORECASE)
DATES_AS_TEXT = ["-mapFieldType", "Date=String,Time=String,DateTime=String"]

BOOTSTRAP_SQL = f"""
CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
    id              SERIAL PRIMARY KEY,
    dataset_id      TEXT        NOT NULL,
    version         TEXT        NOT NULL,
    format_used     TEXT,
    status          TEXT        NOT NULL,   -- success | failed | skipped
    rows_loaded     BIGINT,
    rows_filtered   BIGINT,                 -- invalid geometries left out
    sha256          TEXT,
    error           TEXT,
    started_at      TIMESTAMP   NOT NULL DEFAULT now(),
    finished_at     TIMESTAMP
);
ALTER TABLE {LOG_TABLE} ADD COLUMN IF NOT EXISTS tables TEXT;
"""


class LoadError(Exception):
    """A load failed in a way the user can act on; the message says how."""


@dataclass
class Database:
    host: str
    port: int
    database: str
    username: str
    password: str
    live: str = "data_layers"
    staging: str = "staging"
    backup: str = "backup"

    def connect(self):
        return psycopg2.connect(host=self.host, port=self.port, dbname=self.database, user=self.username, password=self.password)

    def ogr_target(self) -> str:
        """For ogr2ogr; the password goes in the environment, not the command line."""
        return f"PG:host={self.host} port={self.port} dbname={self.database} user={self.username}"


@dataclass
class Layer:
    """One layer to load: where GDAL reads it, and the live table it becomes."""

    source: str  # a file path, or a connection such as WFS:<url>
    layer: str
    table: str
    crs: str | None = None  # assigned when given (the file declares none, or the Dataset overrides it)


@dataclass
class Staged:
    layer: Layer
    staging: str
    rows: int = 0
    dropped: int = 0
    note: str = ""


@dataclass
class LastLoad:
    version: str
    sha256: str | None
    tables: list[str] = field(default_factory=list)


def stamp(when: datetime | None = None) -> str:
    return (when or datetime.now()).strftime("%Y%m%d_%H%M%S")


def suffixed(table: str, suffix: str) -> str:
    """<table>_<suffix> within PostgreSQL's 63-character limit."""
    return f"{table[: 62 - len(suffix)]}_{suffix}"


def staging_name(table: str) -> str:
    """
    A name for this load's staging table, unique to the load. ogr2ogr names
    the table's key, index and sequence after it (…_ogc_fid_seq is the
    longest), and they move with the table into live and backup, so it is
    kept short enough that those names aren't cut short and stay unique.
    """
    tag = f"{stamp()}_{uuid.uuid4().hex[:6]}"
    return f"{table[: 63 - len('_ogc_fid_seq') - len(tag) - 1]}_{tag}"


# --- the ingest log


def bootstrap(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(BOOTSTRAP_SQL)
    conn.commit()


def last_load(conn, dataset: str) -> LastLoad | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT version, sha256, tables FROM {LOG_TABLE} WHERE dataset_id = %s AND status = 'success' "
            "ORDER BY finished_at DESC LIMIT 1",
            (dataset,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return LastLoad(row[0], row[1], [t for t in (row[2] or "").split(",") if t])


def last_check(conn, dataset: str) -> datetime | None:
    """When the dataset was last checked; a failed attempt doesn't count, so the next run tries again."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT max(started_at) FROM {LOG_TABLE} WHERE dataset_id = %s AND status <> 'failed'", (dataset,))
        return cur.fetchone()[0]


def record(conn, dataset: str, version: str, fmt: str | None, status: str, started_at: datetime,
           rows: int | None = None, dropped: int | None = None, sha: str | None = None,
           error: str | None = None, tables: list[str] | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {LOG_TABLE} (dataset_id, version, format_used, status, rows_loaded, rows_filtered, "
            "sha256, error, started_at, finished_at, tables) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)",
            (dataset, version, fmt, status, rows, dropped, sha, error[:1000] if error else None, started_at,
             ",".join(tables) if tables else None),
        )
    conn.commit()


def tables_exist(conn, schema: str, tables: list[str]) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s AND table_name = ANY(%s)",
            (schema, tables),
        )
        return cur.fetchone()[0] == len(set(tables))


# --- getting the data


def download(url: str, dest: Path, md5: str | None = None, session: requests.Session | None = None) -> str:
    """Stream `url` to `dest`; check the MD5 when the source gives one; return the SHA256."""
    sha, md5sum = hashlib.sha256(), hashlib.md5()
    with (session or requests).get(url, stream=True, timeout=TIMEOUT) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                sha.update(chunk)
                md5sum.update(chunk)
    if md5 and md5sum.hexdigest().lower() != md5.lower():
        raise LoadError(f"the download's MD5 is {md5sum.hexdigest()}, not {md5} as the source says; try again later")
    return sha.hexdigest()


def file_layers(path: Path, work: Path) -> list[tuple[str, str, str | None]]:
    """(GDAL path, layer, CRS) of every layer in a downloaded file, unzipping it first."""
    if zipfile.is_zipfile(path):
        out = work / "extracted"
        with zipfile.ZipFile(path) as z:
            z.extractall(out)
            files = [str(out / f) for f in remote.data_files([i.filename for i in z.infolist() if not i.is_dir()])]
        if not files:
            raise LoadError("the download holds no data files GDAL can read")
    else:
        files = [str(path)]
    found = []
    for f in files:
        for layer in remote.to_layers(remote.ogrinfo(f), f):
            found.append((f, layer.name, layer.crs))
    return found


# --- loading


def ensure_schemas(conn, *schemas: str) -> None:
    with conn.cursor() as cur:
        for schema in schemas:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    conn.commit()


def gdal_errors(stderr: str) -> str:
    """The lines of GDAL's output that say what went wrong, without its boilerplate."""
    lines = []
    for line in stderr.splitlines():
        line = line.strip()
        if not line or "Terminating translation" in line or "use -skipfailures" in line or line in lines:
            continue
        if line.startswith(("ERROR", "CONTEXT", "DETAIL")):
            lines.append(line.removeprefix("ERROR 1: ").removeprefix("ERROR:").strip())
    return "; ".join(lines[-3:]) or (stderr.strip().splitlines() or ["no details"])[-1]


def load(db: Database, layer: Layer, staging: str) -> str:
    """ogr2ogr one layer into <staging schema>.<staging>; returns a note for the user, or ""."""
    if shutil.which("ogr2ogr") is None:
        raise LoadError("loading needs GDAL's ogr2ogr on the PATH")
    cmd = [
        "ogr2ogr", "-f", "PostgreSQL", db.ogr_target(), layer.source, layer.layer,
        "-nln", f"{db.staging}.{staging}",
        "-lco", "GEOMETRY_NAME=geom", "-lco", "PRECISION=NO",
        "-nlt", "PROMOTE_TO_MULTI", "-gt", "65536",
        "--config", "PG_USE_COPY", "YES",
    ]
    if layer.crs:
        cmd += ["-a_srs", layer.crs]
    env = {**remote.GDAL_ENV, **os.environ, "PGPASSWORD": db.password}
    result = subprocess.run(cmd + ["-overwrite"], capture_output=True, text=True, env=env)
    if result.returncode != 0 and BAD_DATE.search(result.stderr):
        result = subprocess.run(cmd + ["-overwrite"] + DATES_AS_TEXT, capture_output=True, text=True, env=env)
        if result.returncode == 0:
            return f"{layer.table}: dates loaded as text, since some aren't valid dates"
    if result.returncode != 0:
        raise LoadError(f"ogr2ogr couldn't load layer {layer.layer}: {gdal_errors(result.stderr)}")
    return ""


def geometry_column(conn, schema: str, table: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT f_geometry_column FROM geometry_columns WHERE f_table_schema = %s AND f_table_name = %s LIMIT 1",
                    (schema, table))
        row = cur.fetchone()
    return row[0] if row else None


def check(conn, db: Database, staged: Staged, drop_invalid: bool, min_rows: int) -> None:
    """Drop invalid geometries if asked, count rows, and refuse a table below `min_rows`."""
    name = f'"{db.staging}"."{staged.staging}"'
    with conn.cursor() as cur:
        geom = geometry_column(conn, db.staging, staged.staging)
        if drop_invalid and geom:
            cur.execute(f'DELETE FROM {name} WHERE NOT ST_IsValid("{geom}")')
            staged.dropped = cur.rowcount
        cur.execute(f"SELECT count(*) FROM {name}")
        staged.rows = cur.fetchone()[0]
    conn.commit()
    if staged.rows < min_rows:
        raise LoadError(f"{staged.layer.table} would have {staged.rows:,} rows, fewer than minRows ({min_rows}); "
                        "the live table is unchanged")
    with conn.cursor() as cur:
        cur.execute(f"ANALYZE {name}")
    conn.commit()


def swap(conn, db: Database, staged: list[Staged], keep: int) -> None:
    """In one transaction: each live table to the backup schema, each staged table into its place."""
    with conn.cursor() as cur:
        for s in staged:
            table = s.layer.table
            cur.execute("SELECT to_regclass(%s)", (f'"{db.live}"."{table}"',))
            if cur.fetchone()[0] is not None:
                backup = free_backup_name(cur, db, table)
                cur.execute(f'ALTER TABLE "{db.live}"."{table}" RENAME TO "{backup}"')
                cur.execute(f'ALTER TABLE "{db.live}"."{backup}" SET SCHEMA "{db.backup}"')
            cur.execute(f'ALTER TABLE "{db.staging}"."{s.staging}" SET SCHEMA "{db.live}"')
            cur.execute(f'ALTER TABLE "{db.live}"."{s.staging}" RENAME TO "{table}"')
    conn.commit()
    for s in staged:
        prune(conn, db, s.layer.table, keep)


def free_backup_name(cur, db: Database, table: str) -> str:
    """<table>_<now>, a second later if two loads land in the same second."""
    when = datetime.now()
    while True:
        name = suffixed(table, stamp(when))
        cur.execute("SELECT to_regclass(%s)", (f'"{db.backup}"."{name}"',))
        if cur.fetchone()[0] is None:
            return name
        when += timedelta(seconds=1)


def prune(conn, db: Database, table: str, keep: int) -> None:
    """Drop all but the newest `keep` backups of a table."""
    prefix = table[: 62 - len("YYYYmmdd_HHMMSS")]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_name ~ %s "
            "ORDER BY table_name DESC",
            (db.backup, f"^{prefix}_[0-9]{{8}}_[0-9]{{6}}$"),
        )
        for (old,) in cur.fetchall()[keep:]:
            cur.execute(f'DROP TABLE "{db.backup}"."{old}"')
    conn.commit()


def drop_staged(conn, db: Database, staged: list[Staged]) -> None:
    conn.rollback()
    with conn.cursor() as cur:
        for s in staged:
            cur.execute(f'DROP TABLE IF EXISTS "{db.staging}"."{s.staging}"')
    conn.commit()


def load_all(conn, db: Database, layers: list[Layer], drop_invalid: bool = True, min_rows: int = 1, keep: int = 3) -> list[Staged]:
    """Stage and check every layer, then swap them all in; on any failure, live tables are untouched."""
    ensure_schemas(conn, db.live, db.staging, db.backup)
    staged: list[Staged] = []
    try:
        for layer in layers:
            s = Staged(layer, staging_name(layer.table))
            staged.append(s)
            s.note = load(db, layer, s.staging)
            check(conn, db, s, drop_invalid, min_rows)
        swap(conn, db, staged, keep)
    except BaseException:
        drop_staged(conn, db, staged)
        raise
    return staged
