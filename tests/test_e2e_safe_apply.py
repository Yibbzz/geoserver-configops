"""What apply must never get wrong, against the real GeoServer and its PostGIS: a layer the files turn
off stays off, a rule is there before its layer, only the listed attributes are published, a layer on a
materialized view is published, and a layer keeps its place in its groups when its table is away or it
moves to another schema.

    E2E_GEOSERVER_URL=http://localhost:59090/geoserver-cloud/rest E2E_DB_PORT=55433 pytest -m e2e

The project's workspace (configops_safe), its rule and its tables are removed at the end.
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

WS = "configops_safe"
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=int(os.environ.get("E2E_DB_PORT", 55433)),
          dbname="postgres", user="postgres", password=os.environ.get("E2E_DB_PASSWORD", "postgres"))
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
RULE = f"{WS}.private.r"

PROJECT = f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-safe }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }}, schema: data_layers }}
  workspace: {WS}
"""
LAYERS = {
    "off": "{ table: e2e_safe_points, enabled: false }",
    "private": "{ table: e2e_safe_points, attributes: [name] }",
    "summary": "{ table: e2e_safe_summary }",
    "moving": "{ table: e2e_safe_points }",
}
GROUP = """\
---
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata: { name: both }
spec: { layers: [moving, summary] }
"""
ACCESS = f"""\
---
apiVersion: configops/v1alpha1
kind: AccessRule
metadata: {{ name: private-readers }}
spec: {{ layer: {{ workspace: {WS}, layer: private, access: read }}, roles: [ADMIN] }}
"""


def sql(statement):
    conn = psycopg2.connect(**DB)
    with conn, conn.cursor() as cur:
        cur.execute(statement)
    conn.close()


def write(root, layers: dict[str, str]) -> None:
    text = PROJECT + "".join(f"---\napiVersion: configops/v1alpha1\nkind: Layer\nmetadata: {{ name: {name} }}\nspec: {spec}\n"
                             for name, spec in layers.items())
    (root / "project.yaml").write_text(text + GROUP + ACCESS)


@pytest.fixture(scope="module")
def gs():
    return GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD, gwc_url=os.environ.get("E2E_GWC_URL"))


@pytest.fixture(scope="module")
def project(tmp_path_factory, gs):
    root = tmp_path_factory.mktemp("safe")
    write(root, LAYERS)
    sql("CREATE EXTENSION IF NOT EXISTS postgis; CREATE SCHEMA IF NOT EXISTS data_layers;"
        "DROP SCHEMA IF EXISTS e2e_safe_other CASCADE; CREATE SCHEMA e2e_safe_other;"
        "DROP MATERIALIZED VIEW IF EXISTS data_layers.e2e_safe_summary; DROP TABLE IF EXISTS data_layers.e2e_safe_points;"
        "CREATE TABLE data_layers.e2e_safe_points (id serial PRIMARY KEY, name text, owner text, geom geometry(Point, 27700));"
        "INSERT INTO data_layers.e2e_safe_points (name, owner, geom) VALUES ('a', 'secret', ST_SetSRID(ST_MakePoint(5, 5), 27700));"
        "CREATE MATERIALIZED VIEW data_layers.e2e_safe_summary AS SELECT id, name, geom FROM data_layers.e2e_safe_points;"
        "CREATE TABLE e2e_safe_other.e2e_safe_points AS SELECT * FROM data_layers.e2e_safe_points")
    gs.delete_workspace(WS)
    gs.request("DELETE", f"security/acl/layers/{RULE}", ok=(200, 404))
    yield root
    gs.delete_workspace(WS)
    gs.request("DELETE", f"security/acl/layers/{RULE}", ok=(200, 404))
    sql("DROP SCHEMA IF EXISTS e2e_safe_other CASCADE; DROP MATERIALIZED VIEW IF EXISTS data_layers.e2e_safe_summary;"
        "DROP TABLE IF EXISTS data_layers.e2e_safe_points")


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": str(DB["port"]), "E2EDB_DATABASE": DB["dbname"], "E2EDB_USERNAME": DB["user"],
                       "E2EDB_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    if os.environ.get("E2E_GWC_URL"):
        monkeypatch.setenv("CONFIGOPS_GWC_URL", os.environ["E2E_GWC_URL"])


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def to_do(out: str) -> list[str]:
    """The plan's lines for this project's workspace and rule that apply would act on."""
    return [line.strip() for line in out.splitlines()
            if line.strip()[:2] in ("+ ", "~ ", "- ") and (WS in line or "private" in line)]


def attributes(gs, name: str, store: str = "data_layers") -> list[str]:
    listed = gs.featuretype(WS, store, name)["featureType"]["attributes"]["attribute"]
    return sorted(a["name"] for a in (listed if isinstance(listed, list) else [listed]))


def test_apply_makes_the_rule_first_and_leaves_nothing_to_do(project, gs, capsys):
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    applied = out.split("Applying:", 1)[1]
    assert applied.index(f"rule layers {RULE}") < applied.index(f"layer {WS}:private")  # never readable by everyone
    assert gs.get("security/acl/layers")[RULE] == "ADMIN"

    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 0 and to_do(out) == [], out


def test_a_layer_the_files_turn_off_is_off(project, gs, capsys):
    assert gs.featuretype(WS, "data_layers", "off")["featureType"]["enabled"] is False
    assert gs.featuretype(WS, "data_layers", "moving")["featureType"]["enabled"] is True
    code, out = configops(capsys, "refresh", "-C", str(project), "data_layers.e2e_safe_points")  # after new data
    assert code == 0, out
    assert gs.featuretype(WS, "data_layers", "off")["featureType"]["enabled"] is False
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 0 and to_do(out) == [], out


def test_only_the_listed_attributes_are_published_and_the_layer_is_on(project, gs):
    assert attributes(gs, "private") == ["geom", "name"]
    assert gs.featuretype(WS, "data_layers", "private")["featureType"]["enabled"] is True


def test_a_materialized_view_is_published(project, gs):
    assert gs.featuretype(WS, "data_layers", "summary")["featureType"]["nativeName"] == "e2e_safe_summary"
    assert gs.layer(WS, "summary") is not None


def test_a_change_elsewhere_keeps_the_attributes(project, gs, capsys):
    write(project, {**LAYERS, "private": "{ table: e2e_safe_points, title: Private points, attributes: [name] }"})
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    ft = gs.featuretype(WS, "data_layers", "private")["featureType"]
    assert ft["title"] == "Private points" and ft["enabled"] is True
    assert attributes(gs, "private") == ["geom", "name"]
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 0 and to_do(out) == [], out
    write(project, LAYERS)
    assert configops(capsys, "apply", "-C", str(project))[0] == 0


def members(gs) -> list[str]:
    published = gs.layergroup("both", WS)["layerGroup"]["publishables"]["published"]
    return [p["name"] for p in (published if isinstance(published, list) else [published])]


def test_a_layer_whose_table_is_away_keeps_its_place(project, gs, capsys):
    sql("ALTER MATERIALIZED VIEW data_layers.e2e_safe_summary RENAME TO e2e_safe_summary_loading")
    try:
        code, out = configops(capsys, "plan", "-C", str(project))
        assert code == 3 and to_do(out) == [], out  # only its ! line: the group isn't changed
        assert f"! layer {WS}:summary" in out and "left as GeoServer has it" in out
        code, out = configops(capsys, "apply", "-C", str(project))
        assert code == 0, out
        assert members(gs) == [f"{WS}:moving", f"{WS}:summary"]
    finally:
        sql("ALTER MATERIALIZED VIEW data_layers.e2e_safe_summary_loading RENAME TO e2e_safe_summary")


def test_a_layer_moved_to_another_schema_stays_in_its_group(project, gs, capsys):
    write(project, {**LAYERS, "moving": "{ table: { schema: e2e_safe_other, name: e2e_safe_points } }"})
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    assert gs.featuretype(WS, "e2e_safe_other", "moving") is not None
    assert gs.featuretype(WS, "data_layers", "moving") is None
    assert members(gs) == [f"{WS}:moving", f"{WS}:summary"]
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 0 and to_do(out) == [], out
