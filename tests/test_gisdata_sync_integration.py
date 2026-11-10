"""`gisdata sync` against a real PostGIS, with a file served over HTTP.

Needs ogr2ogr and PostGIS, set with TEST_DB_HOST, TEST_DB_PORT, TEST_DB_NAME,
TEST_DB_USER, TEST_DB_PASSWORD (see test_pipeline_integration.py). Each test
uses its own schemas, so runs don't interfere.
"""

import functools
import http.server
import json
import os
import shutil
import subprocess
import threading
import uuid
import zipfile

import psycopg2
import pytest

from gisdata.cli import main

pytestmark = pytest.mark.integration

if not os.environ.get("TEST_DB_HOST"):
    pytest.skip("TEST_DB_HOST not set", allow_module_level=True)
if shutil.which("ogr2ogr") is None:
    pytest.skip("ogr2ogr not installed", allow_module_level=True)

DB = dict(
    host=os.environ["TEST_DB_HOST"], port=int(os.environ.get("TEST_DB_PORT", 5432)),
    dbname=os.environ.get("TEST_DB_NAME", "gis"), user=os.environ.get("TEST_DB_USER", "postgres"),
    password=os.environ.get("TEST_DB_PASSWORD", "postgres"),
)

POLYGONS = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"name": "square"},
     "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]]}},
    {"type": "Feature", "properties": {"name": "bowtie"},  # self-intersecting: invalid
     "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [10, 10], [10, 0], [0, 10], [0, 0]]]}},
]}
POINTS = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"name": n}, "geometry": {"type": "Point", "coordinates": [i, i]}} for i, n in enumerate("abc")
]}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Serves sites.zip: Data/sites.gpkg with layers `areas` (one valid, one invalid polygon) and `points` (three)."""
    root = tmp_path_factory.mktemp("www")
    gpkg = root / "sites.gpkg"
    for layer, data in (("areas", POLYGONS), ("points", POINTS)):
        src = root / f"{layer}.geojson"
        src.write_text(json.dumps(data))
        subprocess.run(["ogr2ogr", "-f", "GPKG", "-update", "-append", str(gpkg), str(src), "-nln", layer, "-a_srs", "EPSG:27700"],
                       check=True, capture_output=True)
    with zipfile.ZipFile(root / "sites.zip", "w") as z:
        z.write(gpkg, "Data/sites.gpkg")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    handler.log_message = lambda *a: None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/sites.zip"
    httpd.shutdown()


def project(tmp_path, url, schemas, tables="", validation="", backups=1):
    (tmp_path / "project.yaml").write_text(f"""\
apiVersion: gisdata/v1alpha1
kind: Project
metadata: {{ name: t }}
spec:
  geoserver: {{ url: http://gs:8080/geoserver-cloud, credentials: {{ secret: gs }} }}
  database:
    jndi: java:comp/env/jdbc/x
    connection: {{ secret: testdb }}
    schemas: {{ live: {schemas[0]}, staging: {schemas[1]}, backup: {schemas[2]} }}
  workspace: t
  backups: {backups}
---
apiVersion: gisdata/v1alpha1
kind: Source
metadata: {{ name: web }}
spec: {{ type: url }}
---
apiVersion: gisdata/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec:
  source: web
  url: {url}
{tables}{validation}""")
    return tmp_path


@pytest.fixture
def env(monkeypatch):
    for key, value in {"HOST": DB["host"], "PORT": str(DB["port"]), "DATABASE": DB["dbname"],
                       "USERNAME": DB["user"], "PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"GISDATA_TESTDB_{key}", value)


def clear_log():
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.os_ingest_log')")
        if cur.fetchone()[0]:
            cur.execute("DELETE FROM public.os_ingest_log WHERE dataset_id = 'sites'")


@pytest.fixture
def schemas():
    """Fresh schemas, and no history for the `sites` dataset."""
    tag = uuid.uuid4().hex[:8]
    names = (f"live_{tag}", f"staging_{tag}", f"backup_{tag}")
    clear_log()
    yield names
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        for s in names:
            cur.execute(f'DROP SCHEMA IF EXISTS "{s}" CASCADE')
    clear_log()


def query(sql, *args):
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def tables_in(schema):
    return sorted(r[0] for r in query("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", schema))


def sync(root, *args):
    return main(["sync", "-C", str(root), "sites", *args])


def test_load_skip_force_and_backups(tmp_path, server, schemas, env, capsys):
    live, staging, backup = schemas
    root = project(tmp_path, server, schemas)

    assert sync(root) == 0
    out = capsys.readouterr().out
    assert f"{live}.sites_areas (1 rows)" in out and f"{live}.sites_points (3 rows)" in out
    assert "1 invalid geometry left out" in out
    assert tables_in(live) == ["sites_areas", "sites_points"]
    assert tables_in(staging) == []
    assert query(f'SELECT name FROM "{live}".sites_areas') == [("square",)]
    assert query(f"SELECT Find_SRID('{live}', 'sites_points', 'geom')") == [(27700,)]

    assert sync(root) == 0
    assert "up to date" in capsys.readouterr().out
    assert tables_in(backup) == []

    assert sync(root, "--force") == 0
    assert len(tables_in(backup)) == 2  # the first load of each table
    assert sync(root, "--force") == 0
    assert len(tables_in(backup)) == 2  # backups: 1 keeps one per table
    assert tables_in(staging) == []


def test_a_failed_check_leaves_live_tables_alone(tmp_path, server, schemas, env, capsys):
    live, staging, backup = schemas
    assert sync(project(tmp_path, server, schemas)) == 0
    before = query(f'SELECT count(*) FROM "{live}".sites_points')

    root = project(tmp_path, server, schemas, validation="  validation: { minRows: 2 }\n")
    assert sync(root, "--force") == 1
    err = capsys.readouterr().err
    assert "sites_areas would have 1 rows, fewer than minRows (2); the live table is unchanged" in err
    # neither table changed, though points passed its own check
    assert query(f'SELECT count(*) FROM "{live}".sites_points') == before
    assert tables_in(backup) == []
    assert tables_in(staging) == []
    assert query("SELECT status FROM public.os_ingest_log WHERE dataset_id = 'sites' ORDER BY id DESC LIMIT 1") == [("failed",)]


def test_keeping_invalid_geometries(tmp_path, server, schemas, env):
    live = schemas[0]
    root = project(tmp_path, server, schemas, validation="  validation: { dropInvalidGeometries: false }\n")
    assert sync(root) == 0
    assert query(f'SELECT count(*) FROM "{live}".sites_areas') == [(2,)]


def test_changed_tables_or_a_missing_table_reload(tmp_path, server, schemas, env, capsys):
    live = schemas[0]
    assert sync(project(tmp_path, server, schemas)) == 0

    root = project(tmp_path, server, schemas, tables="  tables: { points: { name: site_points } }\n")
    assert sync(root) == 0
    assert "loaded" in capsys.readouterr().out
    assert "site_points" in tables_in(live)

    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(f'DROP TABLE "{live}".site_points')
    assert sync(root) == 0
    assert "loaded" in capsys.readouterr().out
    assert "site_points" in tables_in(live)


def test_a_layer_the_file_lacks(tmp_path, server, schemas, env, capsys):
    root = project(tmp_path, server, schemas, tables="  tables: { roads: {} }\n")
    assert sync(root) == 1
    assert "names roads, which the source doesn't have (it has: areas, points)" in capsys.readouterr().err


def test_loads_in_quick_succession(tmp_path, server, schemas, env):
    """Several loads within a second: backup tables, and the keys and indexes that move with them, never clash."""
    live, staging, backup = schemas
    root = project(tmp_path, server, schemas, backups=5)
    for _ in range(4):
        assert sync(root, "--force") == 0
    assert len(tables_in(backup)) == 6  # 3 earlier loads of each of 2 tables
    assert tables_in(staging) == []


def test_a_single_file_keeps_its_name_so_its_layer_does(tmp_path, schemas, env, capsys):
    """GDAL names a GeoJSON file's layer after the file: squares.geojson holds layer `squares`."""
    www = tmp_path / "www"
    www.mkdir()
    (www / "squares.geojson").write_text(json.dumps(POLYGONS))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(www))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/squares.geojson"
        root = project(tmp_path, url, schemas, tables="  tables: { squares: { name: squares } }\n")
        assert sync(root) == 0, capsys.readouterr().err
        assert "squares" in tables_in(schemas[0])
    finally:
        httpd.shutdown()
