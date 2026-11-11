"""An app-schema store (complex features) against the real GeoServer Cloud REST service: a mapping
on the shared data volume from a PostGIS table to a feature type of our own schema, published,
then import and a clean plan. tests/e2e/up.sh switches the extension on, as values-app-schema.yaml does.

    E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_DB_PORT=55433 E2E_DATA_DIR=tests/e2e/data pytest -m e2e
"""

import os
import shutil
from pathlib import Path

import psycopg2
import pytest

from configops.cli import main
from configops.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
DATA = os.environ.get("E2E_DATA_DIR")
if not URL or not DATA:
    pytest.skip("E2E_GEOSERVER_URL or E2E_DATA_DIR not set", allow_module_level=True)

WS = "configops_appschema"
NAMESPACE = "http://example.org/ex"  # the mapping's target namespace: the workspace must have it
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=int(os.environ.get("E2E_DB_PORT", 55433)),
          dbname="postgres", user="postgres", password=os.environ.get("E2E_DB_PASSWORD", "postgres"))
FIXTURES = Path(__file__).parent / "e2e" / "fixtures" / "appschema"


def sql(statement):
    conn = psycopg2.connect(**DB)
    with conn, conn.cursor() as cur:
        cur.execute(statement)
    conn.close()


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    gs.delete_workspace(WS)
    folder = Path(DATA) / "e2e-appschema"
    shutil.rmtree(folder, ignore_errors=True)
    shutil.copytree(FIXTURES, folder, dirs_exist_ok=True)  # what GeoServer wrote may be left, and not ours to delete
    folder.chmod(0o777)  # GeoServer downloads the schemas into app-schema-cache here
    sql("CREATE EXTENSION IF NOT EXISTS postgis; DROP SCHEMA IF EXISTS e2e_appschema CASCADE; CREATE SCHEMA e2e_appschema;"
        "CREATE TABLE e2e_appschema.sites (id serial PRIMARY KEY, geom geometry(Point, 4326));"
        "INSERT INTO e2e_appschema.sites (geom) VALUES (ST_SetSRID(ST_MakePoint(1, 1), 4326)), (ST_SetSRID(ST_MakePoint(2, 2), 4326))")
    yield gs
    gs.delete_workspace(WS)
    shutil.rmtree(folder, ignore_errors=True)
    sql("DROP SCHEMA IF EXISTS e2e_appschema CASCADE")


@pytest.fixture
def env(monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": str(DB["port"]), "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": DB["user"],
                       "E2EDB_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    monkeypatch.setenv("CONFIGOPS_DATA_DIR", str(Path(DATA).resolve()))


def project_yaml() -> str:
    return f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-appschema }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
"""


@pytest.fixture
def project(tmp_path, env):
    (tmp_path / "project.yaml").write_text(project_yaml() + f"""---
apiVersion: configops/v1alpha1
kind: Workspace
metadata: {{ name: {WS.replace("_", "-")} }}
spec: {{ name: {WS}, namespace: "{NAMESPACE}" }}
---
apiVersion: configops/v1alpha1
kind: Store
metadata: {{ name: sites }}
spec: {{ type: appschema, path: e2e-appschema/sites.xml }}
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: site }}
spec: {{ store: sites, resource: Site, title: Sites }}
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
    ft = gs.get(f"workspaces/{WS}/datastores/sites/featuretypes/site")["featureType"]
    assert ft["nativeBoundingBox"]["maxx"] == 2  # read through the mapping from the table
    assert (Path(DATA) / "e2e-appschema" / "app-schema-cache").is_dir()

    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text(project_yaml())
    assert configops(capsys, "import", WS, "-C", str(imported))[0] == 0
    assert "type: appschema" in (imported / f"{WS}.yaml").read_text()
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
