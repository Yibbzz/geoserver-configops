"""A file blob store on the shared data volume against the real GeoServer Cloud REST service and
its GeoWebCache: a layer's tiles kept there, drift, and import back to a clean plan.

    E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_DB_PORT=55433 E2E_DATA_DIR=tests/e2e/data \\
      E2E_GWC_URL=http://localhost:59090/geoserver-cloud/gwc/rest pytest -m e2e
"""

import os

import psycopg2
import pytest

from gisdata.cli import main
from gisdata.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL or not os.environ.get("E2E_DATA_DIR"):
    pytest.skip("E2E_GEOSERVER_URL or E2E_DATA_DIR not set", allow_module_level=True)

WS = "gisdata_blobs"
STORE = "e2e-tiles-on-volume"
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=int(os.environ.get("E2E_DB_PORT", 55433)),
          dbname="postgres", user="postgres", password=os.environ.get("E2E_DB_PASSWORD", "postgres"))


def sql(statement):
    conn = psycopg2.connect(**DB)
    with conn, conn.cursor() as cur:
        cur.execute(statement)
    conn.close()


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD, gwc_url=os.environ.get("E2E_GWC_URL"))
    gs.delete_workspace(WS)
    sql("CREATE EXTENSION IF NOT EXISTS postgis; DROP SCHEMA IF EXISTS e2e_blobs CASCADE; CREATE SCHEMA e2e_blobs;"
        "CREATE TABLE e2e_blobs.points (id serial PRIMARY KEY, geom geometry(Point, 27700));"
        "INSERT INTO e2e_blobs.points (geom) VALUES (ST_SetSRID(ST_MakePoint(500000, 150000), 27700))")
    yield gs
    gs.delete_workspace(WS)
    gs.gwc_request("DELETE", f"blobstores/{STORE}")
    sql("DROP SCHEMA IF EXISTS e2e_blobs CASCADE")


@pytest.fixture
def env(monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": str(DB["port"]), "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": DB["user"],
                       "E2EDB_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"GISDATA_{key}", value)
    if os.environ.get("E2E_GWC_URL"):
        monkeypatch.setenv("GISDATA_GWC_URL", os.environ["E2E_GWC_URL"])


def project_yaml() -> str:
    return f"""\
apiVersion: gisdata/v1alpha1
kind: Project
metadata: {{ name: e2e-blobs }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
"""


@pytest.fixture
def project(tmp_path, env):
    (tmp_path / "project.yaml").write_text(project_yaml() + f"""---
apiVersion: gisdata/v1alpha1
kind: BlobStore
metadata: {{ name: {STORE} }}
spec: {{ type: file, path: e2e-gwc }}
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: {{ name: points }}
spec: {{ table: {{ schema: e2e_blobs, name: points }}, cache: {{ gridsets: [EPSG:900913], blobStore: {STORE} }} }}
""")
    return tmp_path


def gisdata(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def test_apply_drift_and_import_round_trip(gs, project, tmp_path_factory, capsys, env):
    code, out = gisdata(capsys, "apply", "-C", str(project))
    assert code == 0, out
    assert gisdata(capsys, "plan", "-C", str(project))[0] == 0
    assert gs.tile_layer(f"{WS}:points")["blobStoreId"] == STORE
    gs.gwc_request("PUT", f"blobstores/{STORE}", f'<FileBlobStore default="false"><id>{STORE}</id><enabled>true</enabled>'
                   "<baseDirectory>/data/elsewhere</baseDirectory><fileSystemBlockSize>4096</fileSystemBlockSize></FileBlobStore>")
    code, out = gisdata(capsys, "plan", "-C", str(project))
    assert code == 2 and 'path: "/data/elsewhere" -> "/data/e2e-gwc"' in out, out
    assert gisdata(capsys, "apply", "-C", str(project))[0] == 0
    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text(project_yaml())
    assert gisdata(capsys, "import", WS, "-C", str(imported))[0] == 0
    assert "kind: BlobStore" in (imported / f"{WS}.yaml").read_text()
    code, out = gisdata(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
