"""Run the whole pipeline against a real PostGIS.

Needs ogr2ogr and a PostGIS database, set with TEST_DB_HOST, TEST_DB_PORT,
TEST_DB_NAME, TEST_DB_USER, TEST_DB_PASSWORD. Skipped otherwise. Example:

    docker run -d --rm -p 55432:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=gis postgis/postgis:16-3.4
    TEST_DB_HOST=localhost TEST_DB_PORT=55432 pytest -m integration
"""

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import threading
import uuid
import zipfile

import pytest

import pipeline

pytestmark = pytest.mark.integration

if not os.environ.get("TEST_DB_HOST"):
    pytest.skip("TEST_DB_HOST not set", allow_module_level=True)
if shutil.which("ogr2ogr") is None:
    pytest.skip("ogr2ogr not installed", allow_module_level=True)

# One valid square and one self-intersecting "bowtie" that should be filtered out
FEATURES = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "properties": {"name": "square"},
         "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]]}},
        {"type": "Feature", "properties": {"name": "bowtie"},
         "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [10, 10], [10, 0], [0, 10], [0, 0]]]}},
    ],
}


@pytest.fixture(scope="module")
def served_zip(tmp_path_factory):
    """A zipped GeoPackage served over HTTP. Yields (url, md5, request headers seen)."""
    root = tmp_path_factory.mktemp("www")
    geojson = root / "src.geojson"
    geojson.write_text(json.dumps(FEATURES))
    gpkg = root / "polys.gpkg"
    subprocess.run(["ogr2ogr", "-f", "GPKG", str(gpkg), str(geojson), "-a_srs", "EPSG:27700"], check=True)
    with zipfile.ZipFile(root / "polys.zip", "w") as z:
        z.write(gpkg, "data/polys.gpkg")
    md5 = hashlib.md5((root / "polys.zip").read_bytes()).hexdigest()

    seen_headers = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def do_GET(self):
            seen_headers.append(dict(self.headers))
            super().do_GET()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/polys.zip", md5, seen_headers
    server.shutdown()


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Point the pipeline at the test DB, with schemas unique to this test."""
    suffix = uuid.uuid4().hex[:8]
    db = dict(
        dbname=os.environ.get("TEST_DB_NAME", "gis"),
        user=os.environ.get("TEST_DB_USER", "postgres"),
        password=os.environ.get("TEST_DB_PASSWORD", "postgres"),
        host=os.environ["TEST_DB_HOST"],
        port=int(os.environ.get("TEST_DB_PORT", 5432)),
    )
    schemas = {name: f"t_{name.lower()}_{suffix}" for name in ("PROD_SCHEMA", "STAGING_SCHEMA", "BACKUP_SCHEMA")}
    monkeypatch.setattr(pipeline, "DB", db)
    monkeypatch.setattr(pipeline, "DOWNLOAD_DIR", tmp_path / "dl")
    for name, value in schemas.items():
        monkeypatch.setattr(pipeline, name, value)
    conn = pipeline.get_conn()
    yield conn, schemas, f"Test-{suffix}"
    conn.rollback()
    with conn.cursor() as cur:
        for schema in schemas.values():
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    conn.commit()
    conn.close()


def tables(conn, schema):
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY 1", (schema,))
        return [r[0] for r in cur.fetchall()]


def log_rows(conn, dataset_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version, status, rows_loaded, rows_filtered FROM public.os_ingest_log WHERE dataset_id = %s ORDER BY id",
            (dataset_id,),
        )
        return cur.fetchall()


def dataset(dataset_id, url, md5, version):
    return {"id": dataset_id, "name": "test", "version": version, "format": "GeoPackage",
            "url": url, "md5": md5, "srs": "EPSG:27700", "headers": {"key": "test-key"}}


def test_load_filters_invalid_geometry_and_cleans_up_staging(served_zip, isolated):
    url, md5, headers = served_zip
    conn, schemas, ds_id = isolated
    table = ds_id.lower().replace("-", "_")

    pipeline.run_pipeline([dataset(ds_id, url, md5, "v1")])

    with conn.cursor() as cur:
        cur.execute(f'SELECT name FROM {schemas["PROD_SCHEMA"]}."{table}"')
        assert [r[0] for r in cur.fetchall()] == ["square"]
        cur.execute(f"SELECT indexname FROM pg_indexes WHERE schemaname = %s", (schemas["PROD_SCHEMA"],))
        assert f"{table}_geom_idx" in [r[0] for r in cur.fetchall()]
    assert tables(conn, schemas["STAGING_SCHEMA"]) == []
    assert log_rows(conn, ds_id) == [("v1", "success", 1, 1)]
    assert headers[-1].get("key") == "test-key"


def test_same_version_is_skipped_and_backups_are_capped(served_zip, isolated, monkeypatch):
    url, md5, _ = served_zip
    conn, schemas, ds_id = isolated
    monkeypatch.setattr(pipeline, "BACKUP_RETENTION", 2)
    monkeypatch.setattr(pipeline, "datetime", _Clock())

    for version in ("v1", "v1", "v2", "v3", "v4"):
        pipeline.run_pipeline([dataset(ds_id, url, md5, version)])

    statuses = [(v, s) for v, s, *_ in log_rows(conn, ds_id)]
    assert statuses == [("v1", "success"), ("v1", "skipped"), ("v2", "success"), ("v3", "success"), ("v4", "success")]
    # Three updates made three backups; only the newest two are kept
    assert len(tables(conn, schemas["BACKUP_SCHEMA"])) == 2


def test_checksum_mismatch_is_recorded_as_failed(served_zip, isolated):
    url, _, _ = served_zip
    conn, schemas, ds_id = isolated

    pipeline.run_pipeline([dataset(ds_id, url, "0" * 32, "v1")])

    assert [(v, s) for v, s, *_ in log_rows(conn, ds_id)] == [("v1", "failed")]
    assert tables(conn, schemas["PROD_SCHEMA"]) == []


class _Clock:
    """Stands in for datetime so each backup gets a distinct timestamp without sleeping."""

    def __init__(self):
        from datetime import datetime, timedelta
        self._now, self._step = datetime(2026, 1, 1), timedelta(seconds=1)

    def now(self):
        self._now += self._step
        return self._now
