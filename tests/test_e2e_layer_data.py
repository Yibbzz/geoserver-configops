"""SQL views, a declared CRS, the attributes published and a style's legend against the real
GeoServer Cloud REST service; then drift, and import back to a clean plan.

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

WS = "configops_layer_data"
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=int(os.environ.get("E2E_DB_PORT", 55433)),
          dbname="postgres", user="postgres", password=os.environ.get("E2E_DB_PASSWORD", "postgres"))
SLD = """<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld">
  <NamedLayer><Name>dots</Name><UserStyle><FeatureTypeStyle><Rule><PointSymbolizer/></Rule></FeatureTypeStyle></UserStyle></NamedLayer>
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
    sql("CREATE EXTENSION IF NOT EXISTS postgis; DROP SCHEMA IF EXISTS e2e_layer_data CASCADE; CREATE SCHEMA e2e_layer_data;"
        "CREATE TABLE e2e_layer_data.sites (id serial PRIMARY KEY, name text, kind text, height int, geom geometry(Point, 27700));"
        "INSERT INTO e2e_layer_data.sites (name, kind, height, geom) VALUES "
        "('a', 'hill', 300, ST_SetSRID(ST_MakePoint(1, 1), 27700)), ('b', 'hill', 100, ST_SetSRID(ST_MakePoint(2, 2), 27700)),"
        "('c', 'lake', 0, ST_SetSRID(ST_MakePoint(3, 3), 27700))")
    yield gs
    gs.delete_workspace(WS)
    sql("DROP SCHEMA IF EXISTS e2e_layer_data CASCADE")


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
metadata: {{ name: e2e-layer-data }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
"""


@pytest.fixture
def project(tmp_path, env):
    (tmp_path / "styles").mkdir()
    (tmp_path / "styles" / "dots.sld").write_text(SLD)
    (tmp_path / "project.yaml").write_text(project_yaml() + """---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: high-hills }
spec:
  sql:
    schema: e2e_layer_data
    query: |
      select id, name, height, geom
      from e2e_layer_data.sites
      where kind = 'hill' and height > 200
    key: id
    geometry: { column: geom, type: Point, crs: EPSG:27700 }
  style: dots
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: sites }
spec:
  table: { schema: e2e_layer_data, name: sites }
  crs: EPSG:27700
  attributes: [name, kind]
---
apiVersion: configops/v1alpha1
kind: Style
metadata: { name: dots }
spec: { file: styles/dots.sld, legend: { url: "https://example.org/dots.png", width: 16, height: 16 } }
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
    ft = gs.featuretype(WS, "e2e_layer_data", "sites")["featureType"]
    assert [a["name"] for a in ft["attributes"]["attribute"]] == ["geom", "name", "kind"]
    assert ft["srs"] == "EPSG:27700" and ft["projectionPolicy"] == "FORCE_DECLARED"
    assert gs.style("dots", WS)["style"]["legend"]["onlineResource"] == "https://example.org/dots.png"


def test_drift_is_shown_and_put_back(gs, project, capsys):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    path = f"workspaces/{WS}/datastores/e2e_layer_data/featuretypes/high_hills"
    ft = gs.get(path)["featureType"]
    entry = next(e for e in ft["metadata"]["entry"] if isinstance(e, dict) and e.get("@key") == "JDBC_VIRTUAL_TABLE") \
        if isinstance(ft["metadata"]["entry"], list) else ft["metadata"]["entry"]
    entry["virtualTable"]["sql"] = "select id, name, height, geom from e2e_layer_data.sites"
    gs.request("PUT", path, {"featureType": {"metadata": ft["metadata"]}})
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2 and "sql:" in out, out
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    assert configops(capsys, "plan", "-C", str(project))[0] == 0


def test_import_round_trip(gs, project, tmp_path_factory, capsys, env):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text(project_yaml())
    assert configops(capsys, "import", WS, "-C", str(imported))[0] == 0
    text = (imported / f"{WS}.yaml").read_text()
    assert "query: |" in text and "legend:" in text
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
