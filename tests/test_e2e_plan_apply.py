"""`gisdata plan` and `apply` against the real GeoServer Cloud REST service and its PostGIS.

Start it with tests/e2e/up.sh, then:

    E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_DB_PORT=55433 pytest -m e2e

The project's workspace (gisdata_apply) and tables are removed at the end.
"""

import functools
import http.server
import json
import os
import shutil
import subprocess
import threading
import zipfile

import psycopg2
import pytest

from gisdata.cli import main
from gisdata.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL:
    pytest.skip("E2E_GEOSERVER_URL not set", allow_module_level=True)
if shutil.which("ogr2ogr") is None:
    pytest.skip("ogr2ogr not installed", allow_module_level=True)

WS = "gisdata_apply"
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=int(os.environ.get("E2E_DB_PORT", 55433)),
          dbname="postgres", user="postgres", password=os.environ.get("E2E_DB_PASSWORD", "postgres"))
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
SLD = """<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld">
  <NamedLayer><Name>areas</Name><UserStyle><FeatureTypeStyle><Rule><PolygonSymbolizer/></Rule></FeatureTypeStyle></UserStyle></NamedLayer>
</StyledLayerDescriptor>
"""


def sql(statement):
    conn = psycopg2.connect(**DB)
    with conn, conn.cursor() as cur:
        cur.execute(statement)
    conn.close()


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """A zipped GeoPackage with one polygon layer, `areas`, served over HTTP."""
    root = tmp_path_factory.mktemp("www")
    src = root / "areas.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "square"},
         "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]]}}]}))
    subprocess.run(["ogr2ogr", "-f", "GPKG", str(root / "areas.gpkg"), str(src), "-nln", "areas", "-a_srs", "EPSG:27700"],
                   check=True, capture_output=True)
    with zipfile.ZipFile(root / "areas.zip", "w") as z:
        z.write(root / "areas.gpkg", "areas.gpkg")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/areas.zip"
    httpd.shutdown()


@pytest.fixture(scope="module")
def gs():
    return GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD, gwc_url=os.environ.get("E2E_GWC_URL"))


@pytest.fixture(scope="module")
def project(tmp_path_factory, served, gs):
    root = tmp_path_factory.mktemp("project")
    (root / "styles").mkdir()
    (root / "styles" / "areas.sld").write_text(SLD)
    (root / "project.yaml").write_text(f"""\
apiVersion: gisdata/v1alpha1
kind: Project
metadata: {{ name: e2e }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
---
apiVersion: gisdata/v1alpha1
kind: Source
metadata: {{ name: web }}
spec: {{ type: url }}
---
apiVersion: gisdata/v1alpha1
kind: Dataset
metadata: {{ name: e2e-areas }}
spec: {{ source: web, url: "{served}", tables: {{ areas: {{ name: e2e_areas }} }} }}
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: {{ name: areas }}
spec: {{ dataset: e2e-areas, table: areas, title: Areas, style: areas, attribution: Test data, cache: {{ gridsets: [EPSG:900913] }} }}
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: {{ name: sites }}
spec: {{ table: {{ schema: e2e_field, name: sites }}, style: point, queryable: false }}
---
apiVersion: gisdata/v1alpha1
kind: LayerGroup
metadata: {{ name: everything }}
spec: {{ title: Everything, mode: named, layers: [areas, sites] }}
""")
    sql("CREATE EXTENSION IF NOT EXISTS postgis; DROP SCHEMA IF EXISTS e2e_field CASCADE; CREATE SCHEMA e2e_field;"
        "CREATE TABLE e2e_field.sites (id serial PRIMARY KEY, geom geometry(Point, 27700));"
        "INSERT INTO e2e_field.sites (geom) VALUES (ST_SetSRID(ST_MakePoint(5, 5), 27700));"
        "DROP TABLE IF EXISTS data_layers.e2e_areas")
    gs.delete_workspace(WS)
    yield root
    gs.delete_workspace(WS)
    sql("DROP SCHEMA IF EXISTS e2e_field CASCADE; DROP TABLE IF EXISTS data_layers.e2e_areas;"
        "DELETE FROM public.os_ingest_log WHERE dataset_id = 'e2e-areas';"
        # the backups the forced sync left
        "DO $$ DECLARE t text; BEGIN FOR t IN SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'backup' AND table_name LIKE 'e2e_areas_%' LOOP "
        "EXECUTE format('DROP TABLE backup.%I', t); END LOOP; END $$")


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": str(DB["port"]), "E2EDB_DATABASE": DB["dbname"], "E2EDB_USERNAME": DB["user"],
                       "E2EDB_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"GISDATA_{key}", value)
    if os.environ.get("E2E_GWC_URL"):
        monkeypatch.setenv("GISDATA_GWC_URL", os.environ["E2E_GWC_URL"])


def gisdata(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def test_apply_from_nothing_then_nothing_to_do(project, gs, capsys):
    code, out = gisdata(capsys, "plan", "-C", str(project))
    assert code == 2, out
    assert "+ table data_layers.e2e_areas" in out and f"+ group {WS}:everything" in out

    code, out = gisdata(capsys, "apply", "-C", str(project))
    assert code == 0, out
    assert "e2e-areas: loaded" in out

    code, out = gisdata(capsys, "plan", "-C", str(project))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files.")

    ft = gs.featuretype(WS, "data_layers", "areas")["featureType"]
    assert ft["nativeName"] == "e2e_areas" and ft["title"] == "Areas"
    assert gs.layer(WS, "areas")["layer"]["defaultStyle"]["name"] == f"{WS}:areas"
    assert gs.layer(WS, "sites")["layer"]["queryable"] is False
    group = gs.layergroup("everything", WS)["layerGroup"]
    assert group["mode"] == "NAMED"
    assert gs.layer(WS, "areas")["layer"]["attribution"]["title"] == "Test data"
    # cached as the file says; the cache GeoServer made for the other new layer is gone
    assert [g["gridSetName"] for g in gs.tile_layer(f"{WS}:areas")["gridSubsets"]] == ["EPSG:900913"]
    assert gs.tile_layer(f"{WS}:sites") is None
    assert gs.tile_layer(f"{WS}:everything") is None


def test_sync_refreshes_geoserver_after_a_load(project, capsys):
    code, out = gisdata(capsys, "sync", "-C", str(project), "e2e-areas", "--force")
    assert code == 0, out
    assert "GeoServer: bounds of 1 layer(s) worked out again, cached tiles of 1 layer(s) or group(s) cleared" in out


def test_drift_is_shown_and_put_back(project, gs, capsys):
    gs.request("PUT", f"workspaces/{WS}/datastores/data_layers/featuretypes/areas", {"featureType": {"title": "Edited"}})
    gs.update_layer(WS, "sites", queryable=True)
    code, out = gisdata(capsys, "plan", "-C", str(project))
    assert code == 2
    assert 'title: "Edited" -> "Areas"' in out and "queryable: True -> False" in out
    assert gisdata(capsys, "apply", "-C", str(project))[0] == 0
    assert gisdata(capsys, "plan", "-C", str(project))[0] == 0


def test_unmanaged_left_alone_until_pruned(project, gs, capsys):
    gs.upsert_featuretype(WS, "e2e_field", "by_hand", "sites", title="Made by hand")
    code, out = gisdata(capsys, "plan", "-C", str(project))
    assert code == 0 and f"? layer {WS}:by_hand" in out
    assert gisdata(capsys, "apply", "-C", str(project))[0] == 0
    assert gs.layer(WS, "by_hand") is not None
    code, out = gisdata(capsys, "apply", "-C", str(project), "--prune")
    assert code == 0 and f"- layer {WS}:by_hand" in out
    assert gs.layer(WS, "by_hand") is None
    assert gs.featuretype(WS, "e2e_field", "by_hand") is None
    assert gisdata(capsys, "plan", "-C", str(project), "--prune")[0] == 0


def test_status(project, capsys):
    code, out = gisdata(capsys, "status", "-C", str(project), "--format", "json")
    assert code == 0, out
    report = json.loads(out)
    [dataset] = report["datasets"]
    assert dataset["dataset"] == "e2e-areas" and dataset["version"] and dataset["tables"] == ["e2e_areas"]
    layers = {x["name"]: x for x in report["layers"]}
    assert layers[f"{WS}:areas"] == {"kind": "layer", "name": f"{WS}:areas", "published": True, "drifted": [],
                                     "cached": True, "problem": None}
    assert layers[f"{WS}:everything"]["published"] is True


def test_import_round_trip(project, tmp_path, monkeypatch, capsys):
    """Import the workspace apply made: the files it writes need no changes."""
    for key, value in {"GEOSERVER_ADMIN_USERNAME": "admin", "GEOSERVER_ADMIN_PASSWORD": PASSWORD,
                       "GEOSERVER_PGCONFIG_SECRET_HOST": DB["host"], "GEOSERVER_PGCONFIG_SECRET_PORT": str(DB["port"]),
                       "GEOSERVER_PGCONFIG_SECRET_DATABASE": DB["dbname"], "GEOSERVER_PGCONFIG_SECRET_USERNAME": DB["user"],
                       "GEOSERVER_PGCONFIG_SECRET_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"GISDATA_{key}", value)
    target = tmp_path / "imported"
    code, out = gisdata(capsys, "import", WS, "-C", str(target), "--geoserver", URL.removesuffix("/rest"))
    assert code == 0, out
    assert (target / "styles" / "areas.sld").read_text().strip() == SLD.strip()
    assert gisdata(capsys, "validate", str(target))[0] == 0
    code, out = gisdata(capsys, "plan", "-C", str(target))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
