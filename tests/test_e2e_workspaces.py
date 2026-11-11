"""A Workspace's namespace and isolation, a global style and layer group details against the real
GeoServer Cloud REST service; then import back to a clean plan. (The default workspace is left
alone: it's global, and other tests rely on it.)

    E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_DB_PORT=55433 pytest -m e2e
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

WS = "configops_extras"
STYLE = "configops-e2e-green"
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=int(os.environ.get("E2E_DB_PORT", 55433)),
          dbname="postgres", user="postgres", password=os.environ.get("E2E_DB_PASSWORD", "postgres"))
SLD = """<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld">
  <NamedLayer><Name>green</Name><UserStyle><FeatureTypeStyle><Rule><PointSymbolizer/></Rule></FeatureTypeStyle></UserStyle></NamedLayer>
</StyledLayerDescriptor>
"""


def sql(statement):
    conn = psycopg2.connect(**DB)
    with conn, conn.cursor() as cur:
        cur.execute(statement)
    conn.close()


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    gs.delete_workspace(WS)
    gs.delete_style(STYLE)
    sql("CREATE EXTENSION IF NOT EXISTS postgis; DROP SCHEMA IF EXISTS e2e_extras CASCADE; CREATE SCHEMA e2e_extras;"
        "CREATE TABLE e2e_extras.points (id serial PRIMARY KEY, geom geometry(Point, 27700));"
        "INSERT INTO e2e_extras.points (geom) VALUES (ST_SetSRID(ST_MakePoint(1, 1), 27700))")
    yield gs
    gs.delete_workspace(WS)
    gs.delete_style(STYLE)
    sql("DROP SCHEMA IF EXISTS e2e_extras CASCADE")


@pytest.fixture
def env(monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": str(DB["port"]), "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": DB["user"],
                       "E2EDB_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)


def project_yaml() -> str:
    return f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-extras }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
"""


@pytest.fixture
def project(tmp_path, env):
    (tmp_path / "styles" / "global").mkdir(parents=True)
    (tmp_path / "styles" / "global" / f"{STYLE}.sld").write_text(SLD)
    (tmp_path / "project.yaml").write_text(project_yaml() + f"""---
apiVersion: configops/v1alpha1
kind: Workspace
metadata: {{ name: configops-extras }}
spec: {{ namespace: "https://example.org/extras", isolated: true }}
---
apiVersion: configops/v1alpha1
kind: Style
metadata: {{ name: {STYLE} }}
spec: {{ file: styles/global/{STYLE}.sld, global: true }}
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: points }}
spec: {{ table: {{ schema: e2e_extras, name: points }}, style: {STYLE} }}
---
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata: {{ name: hidden }}
spec: {{ layers: [points], advertised: false, metadataLinks: [ {{ url: "https://example.org/group.xml" }} ] }}
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
    assert gs.get(f"namespaces/{WS}")["namespace"]["uri"] == "https://example.org/extras"
    assert gs.get(f"workspaces/{WS}")["workspace"]["isolated"] is True
    assert gs.style(STYLE) is not None  # global, in no workspace
    assert gs.layer(WS, "points")["layer"]["defaultStyle"]["name"] == STYLE
    assert gs.layergroup("hidden", WS)["layerGroup"]["advertised"] is False


def test_import_round_trip(gs, project, tmp_path_factory, capsys, env):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text(project_yaml())
    assert configops(capsys, "import", WS, "-C", str(imported))[0] == 0
    assert (imported / "styles" / "global" / f"{STYLE}.sld").exists()
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
