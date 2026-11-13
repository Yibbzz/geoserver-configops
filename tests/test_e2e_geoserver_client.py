"""configops's GeoServer client against the real GeoServer Cloud REST service.

Start it with tests/e2e/up.sh, then:

    E2E_GEOSERVER_URL=http://localhost:59090/geoserver-cloud/rest E2E_DB_PORT=55433 pytest -m e2e

Uses its own workspace (configops_e2e) and table, removed at the end.
"""

import os

import psycopg2
import pytest

from configops.geoserver import GeoServer, GeoServerError

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL:
    pytest.skip("E2E_GEOSERVER_URL not set", allow_module_level=True)

WS, STORE, JNDI = "configops_e2e", "postgis", "java:comp/env/jdbc/mypostgis"
SLD = """<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld" xmlns:ogc="http://www.opengis.net/ogc">
  <NamedLayer><Name>{name}</Name><UserStyle><FeatureTypeStyle><Rule>
    <PointSymbolizer><Graphic><Mark><WellKnownName>{shape}</WellKnownName></Mark><Size>6</Size></Graphic></PointSymbolizer>
  </Rule></FeatureTypeStyle></UserStyle></NamedLayer>
</StyledLayerDescriptor>"""


def sql(statement, *args):
    conn = psycopg2.connect(
        host=os.environ.get("E2E_DB_HOST", "localhost"), port=int(os.environ.get("E2E_DB_PORT", 55433)),
        dbname="postgres", user="postgres", password=os.environ.get("E2E_DB_PASSWORD", "postgres"),
    )
    with conn, conn.cursor() as cur:
        cur.execute(statement, args)
    conn.close()


@pytest.fixture(scope="module")
def gs():
    # E2E_GWC_URL: GeoWebCache's REST address when the REST service is reached directly, not through the gateway
    client = GeoServer(URL.removesuffix("/rest"), "admin", os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver"),
                       gwc_url=os.environ.get("E2E_GWC_URL"))
    sql("CREATE EXTENSION IF NOT EXISTS postgis; CREATE SCHEMA IF NOT EXISTS data_layers;"
        "DROP TABLE IF EXISTS data_layers.gc_points;"
        "CREATE TABLE data_layers.gc_points (id serial PRIMARY KEY, name text, geom geometry(Point, 27700));"
        "INSERT INTO data_layers.gc_points (name, geom) VALUES ('a', ST_SetSRID(ST_MakePoint(100, 200), 27700)),"
        "('b', ST_SetSRID(ST_MakePoint(300, 400), 27700))")
    client.delete_workspace(WS)
    for style in ("gc-dots", "gc-squares"):
        client.delete_style(style)
    yield client
    client.delete_workspace(WS)
    for style in ("gc-dots", "gc-squares"):
        client.delete_style(style)
    sql("DROP TABLE IF EXISTS data_layers.gc_points")


def bbox(ft):
    b = ft["featureType"]["nativeBoundingBox"]
    return b["minx"], b["miny"], b["maxx"], b["maxy"]


def test_workspace_and_store(gs):
    assert gs.upsert_workspace(WS) == "created"
    assert gs.upsert_workspace(WS) == "updated"
    assert WS in gs.names("workspaces", "workspace")
    assert gs.upsert_jndi_datastore(WS, STORE, JNDI, "data_layers") == "created"
    store = gs.datastore(WS, STORE)["dataStore"]
    entries = {e["@key"]: e["$"] for e in store["connectionParameters"]["entry"]}
    assert entries["jndiReferenceName"] == JNDI and entries["schema"] == "data_layers"
    assert gs.upsert_jndi_datastore(WS, STORE, JNDI, "data_layers") == "updated"


def test_feature_type_and_its_bounding_box(gs):
    assert gs.upsert_featuretype(WS, STORE, "points", "gc_points", title="Points", abstract="Two points",
                                 keywords=["test"]) == "created"
    ft = gs.featuretype(WS, STORE, "points")
    assert ft["featureType"]["nativeName"] == "gc_points"
    assert ft["featureType"]["title"] == "Points"
    assert bbox(ft) == (100, 200, 300, 400)
    assert gs.layer(WS, "points") is not None

    # new data further out: the box follows it
    sql("INSERT INTO data_layers.gc_points (name, geom) VALUES ('c', ST_SetSRID(ST_MakePoint(900, 50), 27700))")
    gs.recalculate(WS, STORE, "points")
    assert bbox(gs.featuretype(WS, STORE, "points")) == (100, 50, 900, 400)

    assert gs.upsert_featuretype(WS, STORE, "points", "gc_points", title="Points, retitled") == "updated"
    assert gs.featuretype(WS, STORE, "points")["featureType"]["title"] == "Points, retitled"


def test_styles_create_replace_read_delete(gs):
    assert gs.upsert_style("gc-dots", SLD.format(name="gc-dots", shape="circle"), ".sld") == "created"
    assert "circle" in gs.style_body("gc-dots")
    assert gs.upsert_style("gc-dots", SLD.format(name="gc-dots", shape="triangle"), ".sld") == "updated"
    body = gs.style_body("gc-dots")
    assert "triangle" in body and "circle" not in body
    assert gs.upsert_style("gc-squares", SLD.format(name="gc-squares", shape="square"), ".sld", ws=WS) == "created"
    assert gs.style("gc-squares", ws=WS) is not None


def test_layer_settings(gs):
    gs.update_layer(WS, "points", style="gc-dots", styles=[f"{WS}:gc-squares"], queryable=False)
    layer = gs.layer(WS, "points")["layer"]
    assert layer["defaultStyle"]["name"] == "gc-dots"
    extra = layer["styles"]["style"]
    extra = extra if isinstance(extra, list) else [extra]
    assert [s["name"] for s in extra] == [f"{WS}:gc-squares"]
    assert layer["queryable"] is False


def test_layer_group(gs):
    assert gs.upsert_layergroup("gc-group", [("layer", f"{WS}:points", "gc-dots")], ws=WS, title="Group") == "created"
    group = gs.layergroup("gc-group", ws=WS)["layerGroup"]
    published = group["publishables"]["published"]
    published = published if isinstance(published, list) else [published]
    assert [p["name"] for p in published] == [f"{WS}:points"]
    assert group["title"] == "Group"
    assert gs.upsert_layergroup("gc-group", [("layer", f"{WS}:points", None)], ws=WS, mode="opaque") == "updated"
    assert gs.layergroup("gc-group", ws=WS)["layerGroup"]["mode"] == "OPAQUE_CONTAINER"


def test_tile_cache(gs):
    name = f"{WS}:points"
    gs.put_tile_layer(name, ["EPSG:900913"], ["image/png", "image/jpeg"])
    layer = gs.tile_layer(name)
    assert [g["gridSetName"] for g in layer["gridSubsets"]] == ["EPSG:900913"]
    assert sorted(layer["mimeFormats"]) == ["image/jpeg", "image/png"]
    gs.truncate(name)
    assert gs.delete_tile_layer(name) is True
    assert gs.tile_layer(name) is None
    assert gs.delete_tile_layer(name) is False


def test_attribution(gs):
    gs.update_layer(WS, "points", attribution="Contains test data")
    assert gs.layer(WS, "points")["layer"]["attribution"]["title"] == "Contains test data"


def test_deleting(gs):
    assert gs.delete_layergroup("gc-group", ws=WS) is True
    assert gs.layergroup("gc-group", ws=WS) is None
    assert gs.delete_style("gc-dots") is True
    assert gs.style("gc-dots") is None
    assert gs.delete_style("gc-dots") is False  # already gone
    assert gs.delete_featuretype(WS, STORE, "points") is True
    assert gs.layer(WS, "points") is None


def test_errors_say_what_failed(gs):
    with pytest.raises(GeoServerError, match=r"POST workspaces/configops_e2e/datastores/postgis/featuretypes with HTTP"):
        gs.upsert_featuretype(WS, STORE, "nothing", "no_such_table")


@pytest.mark.parametrize("suffix, body, marker", [
    (".css", "* { stroke: #ff0000; stroke-width: 2; }", "#ff0000"),
    (".mbstyle", '{"version": 8, "name": "gc-mb", "layers": [{"id": "l", "type": "line", "paint": {"line-color": "#0000ff"}}]}', "#0000ff"),
])
def test_css_and_mbstyle_styles(gs, suffix, body, marker):
    name = "gc-style" + suffix.replace(".", "-")
    try:
        assert gs.upsert_style(name, body, suffix) == "created"
        assert gs.style(name)["style"]["format"] == suffix.lstrip(".")
        assert marker in gs.style_body(name)
    finally:
        gs.delete_style(name)
