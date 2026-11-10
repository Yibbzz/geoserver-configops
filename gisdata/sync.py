"""`gisdata sync`: load new versions of the project's datasets into PostGIS.

For each Dataset that is due (or named on the command line): ask its
source for the current version, and if it is new (or a table is missing,
or `tables` changed), download it, load the layers `tables` names (all of
them if it names none) and swap them in. Every check is written to the
ingest log, which is also what says when a dataset is next due.
"""

from __future__ import annotations

import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import psycopg2

from . import discover, ingest, remote, schedule, secrets
from .loader import Resource, default_table, load
from .sources import Product, SourceError
from .validate import validate

DB_KEYS = ("host", "port", "database", "username", "password")
DEFAULT_SCHEMAS = {"live": "data_layers", "staging": "staging", "backup": "backup"}
DEFAULT_CRS = "EPSG:4326"


class SyncError(Exception):
    """Stops the whole command (bad files, no database); `code` is the exit code."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def database(project: Resource, root: Path) -> ingest.Database:
    spec = project.spec.get("database") or {}
    secret = (spec.get("connection") or {}).get("secret")
    values = {key: secrets.lookup(secret, key, root) for key in DB_KEYS}
    missing = [key for key, value in values.items() if value is None and key != "port"]
    if missing:
        names = ", ".join(f"${secrets.env_name(secret, key)}" for key in missing)
        raise SyncError(f"the database secret {secret} is missing {', '.join(missing)}: set {names}, or add them to secrets.yaml")
    schemas = {**DEFAULT_SCHEMAS, **(spec.get("schemas") or {})}
    return ingest.Database(
        host=values["host"], port=int(values["port"] or 5432), database=values["database"],
        username=values["username"], password=values["password"], **schemas,
    )


def wanted_tables(dataset: Resource) -> dict[str, dict] | None:
    """`tables`: layer -> options, or None for every layer."""
    tables = dataset.spec.get("tables")
    return {layer: options or {} for layer, options in tables.items()} if tables else None


def plan_layers(dataset: Resource, found: list[tuple[str, str, str | None]], project_crs: str) -> list[ingest.Layer]:
    """The layers to load and their tables, from the (source, layer, CRS) a download or service holds."""
    wanted = wanted_tables(dataset)
    by_name = {layer: (source, crs) for source, layer, crs in found}
    if wanted:
        missing = [layer for layer in wanted if layer not in by_name]
        if missing:
            raise ingest.LoadError(
                f"spec.tables names {', '.join(missing)}, which the source doesn't have "
                f"(it has: {', '.join(sorted(by_name))}); `gisdata discover` lists them")
        chosen = list(wanted)
    else:
        chosen = [layer for _, layer, _ in found]
    layers = []
    for layer in chosen:
        source, file_crs = by_name[layer]
        options = (wanted or {}).get(layer, {})
        layers.append(ingest.Layer(
            source=source, layer=layer,
            table=options.get("name") or default_table(dataset.name, layer),
            # a CRS the Dataset gives wins; otherwise assign the project's only when the file has none
            crs=options.get("crs") or (None if file_crs else project_crs),
        ))
    tables = [x.table for x in layers]
    clashes = sorted({t for t in tables if tables.count(t) > 1})
    if clashes:
        raise ingest.LoadError(f"two layers would load into the same table: {', '.join(clashes)}; name them in spec.tables")
    return layers


def up_to_date(conn, db: ingest.Database, dataset: Resource, last: ingest.LastLoad | None, version: str | None, sha: str | None) -> bool:
    """Same version (or same file) as the last load, its tables still there, and `tables` unchanged."""
    if last is None or not last.tables:
        return False
    same = (version is not None and last.version == version) or (sha is not None and last.sha256 == sha)
    if not same:
        return False
    wanted = wanted_tables(dataset)
    if wanted:
        names = {o.get("name") or default_table(dataset.name, layer) for layer, o in wanted.items()}
        if names != set(last.tables):
            return False
    return ingest.tables_exist(conn, db.live, last.tables)


def sync_dataset(conn, db: ingest.Database, root: Path, project: Resource, dataset: Resource, force: bool) -> str:
    """Check one dataset and load it if needed; returns a line for the user."""
    started = datetime.now()
    spec = dataset.spec
    src = discover.resolve(spec["source"], root)
    product: Product = src.client.product(spec["url"] if src.type == "url" else spec["product"], spec.get("resource"))
    chosen = product.chosen
    if chosen is None:
        raise ingest.LoadError(f"{product.id} has nothing gisdata can load; `gisdata discover` shows what it offers")
    last = ingest.last_load(conn, dataset.name)
    if not force and up_to_date(conn, db, dataset, last, product.version, None):
        ingest.record(conn, dataset.name, product.version, chosen.format, "skipped", started, tables=last.tables)
        return f"{dataset.name}: up to date ({discover.version(product.version)})"

    validation = spec.get("validation") or {}
    project_crs = project.spec.get("crs") or DEFAULT_CRS
    with tempfile.TemporaryDirectory(prefix="gisdata-sync-") as tmp:
        sha = None
        if chosen.service:
            connection, layer = src.client.gdal_source(product)
            found = [(connection, layer, None)]
        else:
            work = Path(tmp)
            path = work / ("download" + (Path(chosen.file_name).suffix if chosen.file_name else ""))
            sha = ingest.download(src.client.download_url(chosen), path, chosen.md5)
            if not force and up_to_date(conn, db, dataset, last, None, sha):
                version = product.version or f"sha256:{sha[:16]}"
                ingest.record(conn, dataset.name, version, chosen.format, "skipped", started, sha=sha, tables=last.tables)
                return f"{dataset.name}: up to date (same file as last time)"
            found = ingest.file_layers(path, work)
        layers = plan_layers(dataset, found, project_crs)
        t0 = time.monotonic()
        staged = ingest.load_all(
            conn, db, layers,
            drop_invalid=validation.get("dropInvalidGeometries", True),
            min_rows=validation.get("minRows", 1),
            keep=project.spec.get("backups", 3),
        )
    version = product.version or (f"sha256:{sha[:16]}" if sha else started.strftime("%Y-%m-%dT%H:%M:%S"))
    rows, dropped = sum(s.rows for s in staged), sum(s.dropped for s in staged)
    tables = [s.layer.table for s in staged]
    ingest.record(conn, dataset.name, version, chosen.format, "success", started,
                  rows=rows, dropped=dropped, sha=sha, tables=tables)
    detail = ", ".join(f"{db.live}.{s.layer.table} ({s.rows:,} rows)" for s in staged)
    notes = ([f"{dropped:,} invalid {'geometry' if dropped == 1 else 'geometries'} left out"] if dropped else [])
    notes += [s.note for s in staged if s.note]
    return f"{dataset.name}: loaded {discover.version(version)} in {time.monotonic() - t0:.0f}s: {detail}" + "".join(f"\n  note: {n}" for n in notes)


def run(root: Path, names: list[str], force: bool = False) -> int:
    project_files = load(root)
    problems = [p for p in validate(project_files) if p.severity == "error"]
    if problems:
        for p in problems[:10]:
            print(f"{p.file}:{p.line}: {p.resource + ': ' if p.resource else ''}{p.message}", file=sys.stderr)
        raise SyncError(f"the files have {len(problems)} error(s); `gisdata validate` lists them")
    project = project_files.of_kind("Project")[0]
    datasets = project_files.named("Dataset")
    unknown = [n for n in names if n not in datasets]
    if unknown:
        raise SyncError(f"no Dataset named {', '.join(unknown)} (known: {', '.join(sorted(datasets)) or 'none'})")

    db = database(project, root)
    try:
        conn = db.connect()
    except psycopg2.OperationalError as e:
        raise SyncError(f"can't connect to the database at {db.host}:{db.port}: {str(e).strip().splitlines()[0]}", code=1) from None

    failures = 0
    try:
        ingest.bootstrap(conn)
        for name in names or sorted(datasets):
            dataset = datasets[name]
            update = dataset.spec.get("update")
            if not names and not force and not schedule.due(update, ingest.last_check(conn, name)):
                print(f"{name}: not due ({update or 'manual'})")
                continue
            try:
                print(sync_dataset(conn, db, root, project, dataset, force), flush=True)
            except (SourceError, ingest.LoadError, remote.RemoteError, discover.DiscoverError, psycopg2.Error, OSError) as e:
                failures += 1
                conn.rollback()
                print(f"{name}: failed: {e}", file=sys.stderr, flush=True)
                ingest.record(conn, name, "unknown", None, "failed", datetime.now(), error=str(e))
    finally:
        conn.close()
    return 1 if failures else 0
