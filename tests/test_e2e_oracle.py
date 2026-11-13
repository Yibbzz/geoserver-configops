"""An Oracle store against standard GeoServer with its oracle extension (GeoServer Cloud can't have one:
test_e2e_standard_only.py checks plan says so). Not run by CI (Oracle wants 2 GB); start Oracle Free
on the e2e network with a table, then point the test at it:

    docker run -d --name configops-oracle --network gscloud-e2e_default -e ORACLE_PASSWORD=oracle \\
      -e APP_USER=gis -e APP_USER_PASSWORD=gis gvenzl/oracle-free:23-slim-faststart
    # as gis in FREEPDB1: CREATE TABLE parcels (id NUMBER PRIMARY KEY, name VARCHAR2(50), geom SDO_GEOMETRY),
    # a row in user_sdo_geom_metadata (SRID 27700) and a spatial index
    E2E_GEOSERVER_URL=http://localhost:59091/geoserver/rest E2E_ORACLE_HOST=configops-oracle pytest -m e2e tests/test_e2e_oracle.py
"""

import os

import pytest
import requests

from configops.cli import main
from configops.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
HOST = os.environ.get("E2E_ORACLE_HOST")
if not URL or not HOST:
    pytest.skip("E2E_GEOSERVER_URL or E2E_ORACLE_HOST not set", allow_module_level=True)

WS = "configops_oracle"
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    gs.delete_workspace(WS)
    yield gs
    gs.delete_workspace(WS)


@pytest.fixture
def env(monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": "localhost", "E2EDB_PORT": "55433",
                       "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": "postgres", "E2EDB_PASSWORD": "postgres",
                       "ORACLE_USERNAME": "gis", "ORACLE_PASSWORD": os.environ.get("E2E_ORACLE_PASSWORD", "gis"),
                       "WAREHOUSE_CREDENTIALS_USERNAME": "gis",
                       "WAREHOUSE_CREDENTIALS_PASSWORD": os.environ.get("E2E_ORACLE_PASSWORD", "gis")}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)


def project_yaml() -> str:
    return f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-oracle }}
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
metadata: {{ name: warehouse }}
spec:
  type: oracle
  connection: {{ host: {HOST}, port: 1521, database: /FREEPDB1, schema: GIS }}
  credentials: {{ secret: oracle }}
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: parcels }}
spec: {{ store: warehouse, resource: PARCELS, crs: EPSG:27700 }}
""")
    return tmp_path


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def test_apply_import_and_nothing_to_do(gs, project, tmp_path_factory, capsys, env):
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    code, out = configops(capsys, "plan", "-C", str(project))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
    store = gs.get(f"workspaces/{WS}/datastores/warehouse")["dataStore"]
    assert store["type"] == "Oracle NG"
    ft = gs.get(f"workspaces/{WS}/datastores/warehouse/featuretypes/parcels")["featureType"]
    assert round(ft["nativeBoundingBox"]["minx"]) == 517900
    r = requests.get(f"{URL.removesuffix('/rest')}/{WS}/wms", timeout=60, params={
        "service": "WMS", "version": "1.1.1", "request": "GetMap", "layers": f"{WS}:parcels", "styles": "", "srs": "EPSG:27700",
        "bbox": "517000,150000,519000,152000", "width": 64, "height": 64, "format": "image/png"})
    assert r.headers["content-type"] == "image/png", r.text[:300]

    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text(project_yaml())
    assert configops(capsys, "import", WS, "-C", str(imported))[0] == 0
    assert "type: oracle" in (imported / f"{WS}.yaml").read_text()
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
