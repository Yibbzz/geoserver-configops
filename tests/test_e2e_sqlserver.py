"""A SQL Server store against the real GeoServer Cloud REST service. Not run by CI (SQL Server
wants 2 GB); start one on the e2e network with a table, then point the test at it:

    docker run -d --name mssql --network gscloud-e2e_default -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD=... \\
      mcr.microsoft.com/mssql/server:2022-latest
    # CREATE DATABASE gis; CREATE TABLE dbo.sites (id int IDENTITY PRIMARY KEY, name nvarchar(50), geom geometry)
    E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_SQLSERVER_HOST=mssql E2E_SQLSERVER_PASSWORD=... pytest -m e2e
"""

import os

import pytest

from configops.cli import main
from configops.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
HOST = os.environ.get("E2E_SQLSERVER_HOST")
if not URL or not HOST:
    pytest.skip("E2E_GEOSERVER_URL or E2E_SQLSERVER_HOST not set", allow_module_level=True)

WS = "configops_sqlserver"
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
                       "SQLSERVER_USERNAME": "sa", "SQLSERVER_PASSWORD": os.environ["E2E_SQLSERVER_PASSWORD"],
                       "SITES_CREDENTIALS_USERNAME": "sa", "SITES_CREDENTIALS_PASSWORD": os.environ["E2E_SQLSERVER_PASSWORD"]}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)


def project_yaml() -> str:
    return f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-sqlserver }}
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
metadata: {{ name: sites }}
spec:
  type: sqlserver
  connection: {{ host: {HOST}, port: 1433, database: gis, schema: dbo }}
  credentials: {{ secret: sqlserver }}
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: sites }}
spec: {{ store: sites, resource: sites, crs: EPSG:27700 }}
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
    ft = gs.get(f"workspaces/{WS}/datastores/sites/featuretypes/sites")["featureType"]
    assert round(ft["nativeBoundingBox"]["minx"]) == 517900  # SQL Server rounds the extent a little

    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text(project_yaml())
    assert configops(capsys, "import", WS, "-C", str(imported))[0] == 0
    assert "type: sqlserver" in (imported / f"{WS}.yaml").read_text()
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
