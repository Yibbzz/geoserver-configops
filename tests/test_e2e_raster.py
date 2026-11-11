"""A raster Dataset against the real GeoServer Cloud REST service: a zip of grid tiles served over
HTTP is made into one COG on the data volume, published through a Store on it, then loaded again.

gisdata runs here and writes to E2E_DATA_DIR, which tests/e2e/up.sh mounts at /data in the REST
service; it needs GDAL's command-line tools:

    E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_DB_PORT=55433 E2E_DATA_DIR=tests/e2e/data pytest -m e2e
"""

import functools
import http.server
import os
import shutil
import threading
import zipfile
from pathlib import Path

import psycopg2
import pytest

from gisdata.cli import main
from gisdata.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
DATA = os.environ.get("E2E_DATA_DIR")
if not URL or not DATA:
    pytest.skip("E2E_GEOSERVER_URL or E2E_DATA_DIR not set", allow_module_level=True)
if shutil.which("gdalbuildvrt") is None:
    pytest.skip("GDAL's command-line tools not installed", allow_module_level=True)

WS = "gisdata_raster"
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=int(os.environ.get("E2E_DB_PORT", 55433)),
          dbname="postgres", user="postgres", password=os.environ.get("E2E_DB_PASSWORD", "postgres"))
PATH = "e2e-raster/heights.tif"


def sql(statement):
    conn = psycopg2.connect(**DB)
    with conn, conn.cursor() as cur:
        cur.execute(statement)
    conn.close()


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """Two 10 x 10 grid tiles, without a CRS, in zips inside a zip, as OS packs its tiles."""
    root = tmp_path_factory.mktemp("www")
    with zipfile.ZipFile(root / "heights.zip", "w") as outer:
        for i, x in enumerate((510000, 510500)):
            inner = root / f"tile{i}.zip"
            rows = "".join(" ".join(str(10 * i + r) for _ in range(10)) + "\n" for r in range(10))
            with zipfile.ZipFile(inner, "w") as z:
                z.writestr(f"t{i}.asc", f"ncols 10\nnrows 10\nxllcorner {x}\nyllcorner 140000\ncellsize 50\n{rows}")
            outer.write(inner, f"data/{inner.name}")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/heights.zip"
    httpd.shutdown()


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    gs.delete_workspace(WS)
    shutil.rmtree(Path(DATA) / "e2e-raster", ignore_errors=True)
    sql("DELETE FROM public.os_ingest_log WHERE dataset_id = 'e2e-heights'") if table_exists() else None
    yield gs
    gs.delete_workspace(WS)
    shutil.rmtree(Path(DATA) / "e2e-raster", ignore_errors=True)
    shutil.rmtree(Path(DATA) / ".gisdata-backups" / "e2e-raster", ignore_errors=True)
    sql("DELETE FROM public.os_ingest_log WHERE dataset_id = 'e2e-heights'")


def table_exists() -> bool:
    conn = psycopg2.connect(**DB)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.os_ingest_log') IS NOT NULL")
        found = cur.fetchone()[0]
    conn.close()
    return found


@pytest.fixture
def project(tmp_path, served, monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": str(DB["port"]), "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": DB["user"],
                       "E2EDB_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"GISDATA_{key}", value)
    monkeypatch.setenv("GISDATA_DATA_DIR", str(Path(DATA).resolve()))
    (tmp_path / "project.yaml").write_text(f"""\
apiVersion: gisdata/v1alpha1
kind: Project
metadata: {{ name: e2e-raster }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
  crs: EPSG:27700
  backups: 1
---
apiVersion: gisdata/v1alpha1
kind: Source
metadata: {{ name: web }}
spec: {{ type: url }}
---
apiVersion: gisdata/v1alpha1
kind: Dataset
metadata: {{ name: e2e-heights }}
spec: {{ source: web, url: "{served}", raster: {{ path: {PATH} }} }}
---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: {{ name: heights }}
spec: {{ type: geotiff, dataset: e2e-heights }}
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: {{ name: heights }}
spec: {{ store: heights, title: Heights }}
""")
    return tmp_path


def gisdata(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def test_first_load_then_publish_then_nothing_to_do(gs, project, capsys):
    code, out = gisdata(capsys, "plan", "-C", str(project))
    assert code == 2 and f"+ raster /data/{PATH}   (first sync of dataset e2e-heights)" in out, out
    code, out = gisdata(capsys, "apply", "-C", str(project))
    assert code == 0 and "e2e-heights: loaded" in out and "(200 pixels)" in out, out
    code, out = gisdata(capsys, "plan", "-C", str(project))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
    coverage = gs.get(f"workspaces/{WS}/coveragestores/heights/coverages/heights")["coverage"]
    assert coverage["srs"] == "EPSG:27700"  # the project's, since the grids have none
    assert coverage["nativeBoundingBox"]["minx"] == 510000 and coverage["nativeBoundingBox"]["maxx"] == 511000


def test_a_new_load_is_swapped_in_and_geoserver_refreshed(gs, project, capsys):
    assert gisdata(capsys, "apply", "-C", str(project))[0] == 0
    code, out = gisdata(capsys, "sync", "-C", str(project), "e2e-heights", "--force")
    assert code == 0 and "e2e-heights: loaded" in out, out
    assert "bounds of 1 layer(s) worked out again" in out
    backups = list((Path(DATA) / ".gisdata-backups" / "e2e-raster").glob("heights.*.tif"))
    assert len(backups) == 1
    assert gisdata(capsys, "plan", "-C", str(project))[0] == 0
