"""Tile caching against the real GeoServer Cloud REST service and its GeoWebCache: a gridset of
our own, and a layer cached in it with metatiling, gutter and expiry; then import to a clean plan.

    E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_DB_PORT=55433 \
      E2E_GWC_URL=http://localhost:59090/geoserver-cloud/gwc/rest pytest -m e2e
"""

import os

import psycopg2
import pytest

from configops.cli import main
from configops.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL:
    pytest.skip("E2E_GEOSERVER_URL not set", allow_module_level=True)

WS = "configops_tiles"
GRIDSET = "e2e-british-national-grid"
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
    sql("CREATE EXTENSION IF NOT EXISTS postgis; DROP SCHEMA IF EXISTS e2e_tiles CASCADE; CREATE SCHEMA e2e_tiles;"
        "CREATE TABLE e2e_tiles.points (id serial PRIMARY KEY, geom geometry(Point, 27700));"
        "INSERT INTO e2e_tiles.points (geom) VALUES (ST_SetSRID(ST_MakePoint(500000, 150000), 27700))")
    yield gs
    gs.delete_workspace(WS)
    gs.gwc_request("DELETE", f"gridsets/{GRIDSET}")
    sql("DROP SCHEMA IF EXISTS e2e_tiles CASCADE")


@pytest.fixture
def env(monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": str(DB["port"]), "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": DB["user"],
                       "E2EDB_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    if os.environ.get("E2E_GWC_URL"):
        monkeypatch.setenv("CONFIGOPS_GWC_URL", os.environ["E2E_GWC_URL"])


def project_yaml() -> str:
    return f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-tiles }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
"""


@pytest.fixture
def project(tmp_path, env):
    (tmp_path / "project.yaml").write_text(project_yaml() + f"""---
apiVersion: configops/v1alpha1
kind: Gridset
metadata: {{ name: {GRIDSET} }}
spec:
  crs: EPSG:27700
  extent: [-238375, 0, 900000, 1376256]
  resolutions: [896, 448, 224, 112, 56, 28, 14]
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: points }}
spec:
  table: {{ schema: e2e_tiles, name: points }}
  cache: {{ gridsets: [{GRIDSET}, EPSG:900913], metaTiles: [2, 2], gutter: 10, expireCache: 60, expireClients: 30 }}
""")
    return tmp_path


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def test_apply_then_nothing_to_do(gs, project, capsys):
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    code, out = configops(capsys, "plan", "-C", str(project))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
    assert gs.gridset(GRIDSET)["resolutions"][-1] == 14
    tiles = gs.tile_layer(f"{WS}:points")
    assert tiles["metaWidthHeight"] == [2, 2] and tiles["gutter"] == 10 and tiles["expireCache"] == 60


def test_drift_and_import_round_trip(gs, project, tmp_path_factory, capsys, env):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    gs.put_tile_layer(f"{WS}:points", [GRIDSET, "EPSG:900913"], ["image/png"], {"metaTiles": [4, 4], "gutter": 10, "expireCache": 60,
                                                                                "expireClients": 30})
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2 and "cache:" in out, out
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text(project_yaml())
    assert configops(capsys, "import", WS, "-C", str(imported))[0] == 0
    text = (imported / f"{WS}.yaml").read_text()
    assert "kind: Gridset" in text and "metaTiles:" in text
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
