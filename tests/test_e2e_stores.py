"""Store resources against the real GeoServer Cloud REST service: a Cloud-Optimised GeoTIFF,
cascaded WMS and WMTS, and a PostGIS store with a password; then import and a clean plan.

Needs the internet (Sentinel-2 on AWS, EOX's tile services). Start the service with
tests/e2e/up.sh, then:

    E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_DB_PORT=55433 pytest -m e2e

E2E_DB_HOST_IN_GEOSERVER is the database's host as GeoServer reaches it (default: db,
its name in tests/e2e/compose.yaml). The workspace and table are removed at the end.
"""

import os

import psycopg2
import pytest

from gisdata.cli import main
from gisdata.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL:
    pytest.skip("E2E_GEOSERVER_URL not set", allow_module_level=True)

WS = "gisdata_stores"
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=int(os.environ.get("E2E_DB_PORT", 55433)),
          dbname="postgres", user="postgres", password=os.environ.get("E2E_DB_PASSWORD", "postgres"))
DB_IN_GEOSERVER = os.environ.get("E2E_DB_HOST_IN_GEOSERVER", "db")
COG = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/30/U/XC/2025/8/S2C_30UXC_20250825_0_L2A/TCI.tif"


def sql(statement):
    conn = psycopg2.connect(**DB)
    with conn, conn.cursor() as cur:
        cur.execute(statement)
    conn.close()


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    gs.delete_workspace(WS)
    sql("CREATE EXTENSION IF NOT EXISTS postgis; DROP SCHEMA IF EXISTS e2e_stores CASCADE; CREATE SCHEMA e2e_stores;"
        "CREATE TABLE e2e_stores.points (id serial PRIMARY KEY, geom geometry(Point, 27700));"
        "INSERT INTO e2e_stores.points (geom) VALUES (ST_SetSRID(ST_MakePoint(5, 5), 27700))")
    yield gs
    gs.delete_workspace(WS)
    sql("DROP SCHEMA IF EXISTS e2e_stores CASCADE")


@pytest.fixture
def env(monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": str(DB["port"]), "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": DB["user"],
                       "E2EDB_PASSWORD": DB["password"], "WAREHOUSE_USERNAME": DB["user"], "WAREHOUSE_PASSWORD": DB["password"],
                       "WAREHOUSE_CREDENTIALS_USERNAME": DB["user"], "WAREHOUSE_CREDENTIALS_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"GISDATA_{key}", value)


def project_yaml() -> str:
    return f"""\
apiVersion: gisdata/v1alpha1
kind: Project
metadata: {{ name: e2e-stores }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
"""


@pytest.fixture
def project(tmp_path, env):
    (tmp_path / "project.yaml").write_text(project_yaml() + f"""---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: {{ name: sentinel-2 }}
spec: {{ type: cog, url: "{COG}", description: Sentinel-2 true colour }}
---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: {{ name: eox-wms }}
spec: {{ type: wms, url: "https://tiles.maps.eox.at/wms?service=WMS&request=GetCapabilities" }}
---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: {{ name: eox-wmts }}
spec: {{ type: wmts, url: "https://tiles.maps.eox.at/wmts/1.0.0/WMTSCapabilities.xml" }}
---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: {{ name: warehouse }}
spec:
  type: postgis
  connection: {{ host: {DB_IN_GEOSERVER}, port: 5432, database: postgres, schema: e2e_stores }}
  credentials: {{ secret: warehouse }}
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: {{ name: imagery }}
spec: {{ store: sentinel-2, title: Sentinel-2 }}
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: {{ name: cloudless-wms }}
spec: {{ store: eox-wms, resource: s2cloudless-2024 }}
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: {{ name: cloudless-wmts }}
spec: {{ store: eox-wmts, resource: s2cloudless-2024_3857, cache: true }}
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: {{ name: points }}
spec: {{ store: warehouse, resource: points, style: point }}
""")
    return tmp_path


def gisdata(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def test_apply_then_nothing_to_do(gs, project, capsys):
    code, out = gisdata(capsys, "apply", "-C", str(project))
    assert code == 0, out
    code, out = gisdata(capsys, "plan", "-C", str(project))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
    coverage = gs.get(f"workspaces/{WS}/coveragestores/sentinel_2/coverages/imagery")["coverage"]
    assert coverage["nativeCoverageName"] == "geotiff_coverage" and coverage["srs"] == "EPSG:32630"
    assert gs.layer(WS, "points")["layer"]["defaultStyle"]["name"] == "point"


def test_drift_is_shown_and_put_back(gs, project, capsys):
    assert gisdata(capsys, "apply", "-C", str(project))[0] == 0
    gs.request("PUT", f"workspaces/{WS}/coveragestores/sentinel_2", {"coverageStore": {"description": "edited"}})
    code, out = gisdata(capsys, "plan", "-C", str(project))
    assert code == 2 and 'description: "edited" -> "Sentinel-2 true colour"' in out
    assert gisdata(capsys, "apply", "-C", str(project))[0] == 0
    assert gisdata(capsys, "plan", "-C", str(project))[0] == 0


def test_import_round_trip(gs, project, tmp_path_factory, capsys, env):
    assert gisdata(capsys, "apply", "-C", str(project))[0] == 0
    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text(project_yaml())
    code, out = gisdata(capsys, "import", WS, "-C", str(imported))
    assert code == 0 and "4 store(s), 4 layer(s)" in out, out
    assert "Secret warehouse-credentials (keys: username, password)" in out
    code, out = gisdata(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
