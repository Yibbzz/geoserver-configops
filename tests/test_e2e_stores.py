"""Store resources against the real GeoServer Cloud REST service: a Cloud-Optimised GeoTIFF,
cascaded WMS and WMTS, and a PostGIS store with a password; then import and a clean plan.

Needs the internet (Sentinel-2 on AWS, EOX's tile services). Start the service with
tests/e2e/up.sh, then:

    E2E_GEOSERVER_URL=http://localhost:59090/geoserver-cloud/rest E2E_DB_PORT=55433 pytest -m e2e

E2E_DB_HOST_IN_GEOSERVER is the database's host as GeoServer reaches it (default: db,
its name in tests/e2e/compose.yaml). The workspace and table are removed at the end.
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

WS = "configops_stores"
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
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    monkeypatch.setenv("CONFIGOPS_EOXLOGIN_USERNAME", "e2e-user")
    monkeypatch.setenv("CONFIGOPS_EOXLOGIN_PASSWORD", "e2e-secret")
    monkeypatch.setenv("CONFIGOPS_EOXKEY_VALUE", "e2e-key")
    monkeypatch.setenv("CONFIGOPS_EOX_WMS_HEADER_VALUE", "yes")  # the Secrets import names for them
    monkeypatch.setenv("CONFIGOPS_EOX_WMS_AUTHKEY_VALUE", "e2e-key")
    monkeypatch.setenv("CONFIGOPS_EOX_WMTS_HEADER_VALUE", "e2e-key")
    monkeypatch.setenv("CONFIGOPS_EOX_WMTS_AUTHKEY_VALUE", "e2e-wmts-key")
    for secret in ("EOX_WMS_LOGIN_CREDENTIALS", "EOX_WMTS_LOGIN_CREDENTIALS"):  # the Secrets import names
        monkeypatch.setenv(f"CONFIGOPS_{secret}_USERNAME", "e2e-user")
        monkeypatch.setenv(f"CONFIGOPS_{secret}_PASSWORD", "e2e-secret")


def project_yaml() -> str:
    return f"""\
apiVersion: configops/v1alpha1
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
apiVersion: configops/v1alpha1
kind: Store
metadata: {{ name: sentinel-2 }}
spec: {{ type: cog, url: "{COG}", description: Sentinel-2 true colour }}
---
apiVersion: configops/v1alpha1
kind: Store
metadata: {{ name: eox-wms }}
spec:
  type: wms
  url: "https://tiles.maps.eox.at/wms?service=WMS&request=GetCapabilities"
  maxConnections: 4
  readTimeout: 45
  connectTimeout: 15
  connectionPooling: false
  disableOnConnectionFailure: true
  header: {{ name: X-Configops-E2e, value: "yes" }}
  authKey: {{ secret: eoxkey, key: value }}
---
apiVersion: configops/v1alpha1
kind: Store
metadata: {{ name: eox-wmts }}
spec:
  type: wmts
  url: "https://tiles.maps.eox.at/wmts/1.0.0/WMTSCapabilities.xml"
  maxConnections: 3
  readTimeout: 40
  connectTimeout: 12
  connectionPooling: false
  header: {{ name: X-Configops-E2e, secret: eoxkey, key: value }}
  authKey: e2e-wmts-key
---
apiVersion: configops/v1alpha1
kind: Store
metadata: {{ name: eox-wms-login }}       # a login, as a service that wants one would; no layer uses it
spec: {{ type: wms, url: "https://tiles.maps.eox.at/wms?service=WMS&request=GetCapabilities", credentials: {{ secret: eoxlogin }} }}
---
apiVersion: configops/v1alpha1
kind: Store
metadata: {{ name: eox-wmts-login }}
spec: {{ type: wmts, url: "https://tiles.maps.eox.at/wmts/1.0.0/WMTSCapabilities.xml", credentials: {{ secret: eoxlogin }} }}
---
apiVersion: configops/v1alpha1
kind: Store
metadata: {{ name: warehouse }}
spec:
  type: postgis
  connection: {{ host: {DB_IN_GEOSERVER}, port: 5432, database: postgres, schema: e2e_stores }}
  credentials: {{ secret: warehouse }}
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: imagery }}
spec: {{ store: sentinel-2, title: Sentinel-2 }}
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: cloudless-wms }}
spec:
  store: eox-wms
  resource: s2cloudless-2024
  remote: {{ style: default, styles: [default], format: image/jpeg, formats: [image/jpeg, image/png], minScale: 1000, maxScale: 50000000,
            respectBounds: true, vendorParameters: {{ tiled: true }} }}
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: cloudless-wmts }}
spec: {{ store: eox-wmts, resource: s2cloudless-2024_3857, cache: true }}
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: points }}
spec: {{ store: warehouse, resource: points, style: point }}
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
    coverage = gs.get(f"workspaces/{WS}/coveragestores/sentinel_2/coverages/imagery")["coverage"]
    assert coverage["nativeCoverageName"] == "geotiff_coverage" and coverage["srs"] == "EPSG:32630"
    assert gs.layer(WS, "points")["layer"]["defaultStyle"]["name"] == "point"
    for kind, key, name in (("wmsstores", "wmsStore", "eox_wms_login"), ("wmtsstores", "wmtsStore", "eox_wmts_login")):
        store = gs.get(f"workspaces/{WS}/{kind}/{name}")[key]
        assert store["user"] == "e2e-user" and store["password"].startswith("crypt1:"), store  # kept, and encrypted
    store = gs.get(f"workspaces/{WS}/wmsstores/eox_wms")["wmsStore"]
    assert (store["maxConnections"], store["readTimeout"], store["connectTimeout"], store["disableOnConnFailure"]) == (4, 45, 15, True)
    assert (store["headerName"], store["headerValue"], store["authKey"]) == ("X-Configops-E2e", "yes", "e2e-key")
    store = gs.get(f"workspaces/{WS}/wmtsstores/eox_wmts")["wmtsStore"]
    assert (store["maxConnections"], store["headerValue"], store["authKey"]) == (3, "e2e-key", "e2e-wmts-key")
    item = gs.get(f"workspaces/{WS}/wmsstores/eox_wms/wmslayers/cloudless_wms")["wmsLayer"]
    assert (item["forcedRemoteStyle"], item["preferredFormat"], item["minScale"], item["metadataBBoxRespected"]) == ("default", "image/jpeg", 1000, True)
    assert item["vendorParameters"]["entry"] == {"@key": "tiled", "$": "true"}


def test_drift_is_shown_and_put_back(gs, project, capsys):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    gs.request("PUT", f"workspaces/{WS}/coveragestores/sentinel_2", {"coverageStore": {"description": "edited"}})
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2 and 'description: "edited" -> "Sentinel-2 true colour"' in out
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    assert configops(capsys, "plan", "-C", str(project))[0] == 0


def test_a_password_changed_in_the_secret_is_a_change(gs, project, capsys, monkeypatch):
    """GeoServer gives no password back: apply keeps a fingerprint of each login in the store's metadata, and plan compares it."""
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    code, out = configops(capsys, "plan", "-C", str(project))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
    monkeypatch.setenv("CONFIGOPS_EOXLOGIN_PASSWORD", "e2e-rotated")
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2, out
    assert f"~ store {WS}:eox_wms_login" in out and 'credentials: "(differs)" -> "(from secret eoxlogin)"' in out
    assert "e2e-rotated" not in out and "e2e-secret" not in out
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    code, out = configops(capsys, "plan", "-C", str(project))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
    monkeypatch.setenv("CONFIGOPS_EOXLOGIN_PASSWORD", "e2e-secret")  # as the later tests have it
    assert configops(capsys, "apply", "-C", str(project))[0] == 0


def test_a_header_value_that_differs_is_not_printed(gs, project, capsys, monkeypatch):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    monkeypatch.setenv("CONFIGOPS_EOXKEY_VALUE", "e2e-other-key")
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2 and 'headerValue: "(differs)" -> "(hidden)"' in out and 'authKey: "(differs)" -> "(hidden)"' in out, out
    assert "e2e-other-key" not in out and "e2e-key" not in out


def test_import_round_trip(gs, project, tmp_path_factory, capsys, env):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text(project_yaml())
    code, out = configops(capsys, "import", WS, "-C", str(imported))
    assert code == 0 and "6 store(s), 4 layer(s)" in out, out
    assert "Secret warehouse-credentials (keys: username, password)" in out
    assert "Secret eox-wms-login-credentials" in out  # the cascaded store's user came back
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
