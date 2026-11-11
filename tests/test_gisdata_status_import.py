"""`gisdata status` and `gisdata import` without a GeoServer: the pieces that decide what they show and write."""

from datetime import datetime, timedelta

import pytest

from gisdata.importer import Importer, cache_setting, default_style, dump, resource_name
from gisdata.schedule import next_check

NOW = datetime(2026, 11, 10, 2, 0)


@pytest.mark.parametrize("update, last, expected", [
    ("manual", NOW, None),
    ("daily", None, NOW),  # never checked: due now
    ("weekly", NOW - timedelta(days=1), NOW - timedelta(days=1) + timedelta(days=7) - timedelta(hours=1)),
    ("weekly", NOW - timedelta(days=8), NOW),
    ("0 3 * * *", NOW - timedelta(minutes=30), datetime(2026, 11, 10, 3, 0)),
])
def test_next_check(update, last, expected):
    assert next_check(update, last, NOW) == expected


@pytest.mark.parametrize("gs_name, expected", [
    ("road_links", ("road-links", None)),
    ("roads", ("roads", None)),
    ("Roads", ("roads", "Roads")),           # case doesn't round trip
    ("road-links", ("road-links", "road-links")),  # dashes would come back as underscores
    ("os.roads", ("os-roads", "os.roads")),
])
def test_resource_names(gs_name, expected):
    assert resource_name(gs_name) == expected


def ft(binding):
    return {"attributes": {"attribute": [{"name": "id", "binding": "java.lang.Integer"},
                                         {"name": "geom", "binding": f"org.locationtech.jts.geom.{binding}"}]}}


def test_default_style_from_geometry():
    assert default_style(ft("MultiPolygon")) == "polygon"
    assert default_style(ft("Point")) == "point"
    assert default_style(ft("MultiLineString")) == "line"
    assert default_style(ft("Geometry")) == "generic"
    assert default_style({"attributes": {"attribute": {"name": "id", "binding": "java.lang.Integer"}}}) == "generic"


def test_cache_settings():
    tile = lambda grids, formats: {"enabled": True, "gridSubsets": [{"gridSetName": g} for g in grids], "mimeFormats": formats}
    assert cache_setting(None) is None
    assert cache_setting(tile(["EPSG:900913", "EPSG:4326"], ["image/png"])) is True
    assert cache_setting(tile(["EPSG:27700"], ["image/png"])) == {"gridsets": ["EPSG:27700"], "formats": ["image/png"]}
    assert cache_setting({**tile(["EPSG:4326"], ["image/png"]), "enabled": False}) is None


class FakeGeoServer:
    """A workspace `uk` with one layer and one group, as GeoServer's JSON."""

    def names(self, path, key):
        return {"dataStore": ["data_layers"], "layer": ["road_links"], "layerGroup": ["base"], "style": ["roads"]}.get(key, [])

    def datastore(self, ws, name):
        return {"dataStore": {"connectionParameters": {"entry": [
            {"@key": "schema", "$": "data_layers"}, {"@key": "jndiReferenceName", "$": "java:comp/env/jdbc/x"}]}}}

    def layer(self, ws, name):
        return {"layer": {"defaultStyle": {"name": "uk:roads"}, "styles": {"style": {"name": "line"}}, "queryable": True,
                          "attribution": {"title": "Contains OS data"},
                          "resource": {"href": "http://gs/rest/workspaces/uk/datastores/data_layers/featuretypes/road_links.json"}}}

    def featuretype(self, ws, store, name):
        return {"featureType": {"nativeName": "roads_road_link", "title": "road_links", "enabled": True, "advertised": False,
                                "keywords": {"string": ["features", "roads_road_link"]}, **ft("MultiLineString")}}

    def tile_layer(self, name):
        return None

    def layergroup(self, name, ws):
        return {"layerGroup": {"title": "Base", "mode": "OPAQUE_CONTAINER", "publishables": {"published": [
            {"@type": "layer", "name": "uk:road_links"}, {"@type": "layer", "name": "other:thing"}]},
            "styles": {"style": [{"name": "line"}, ""]}}}

    def style(self, name, ws):
        return {"style": {"format": "sld"}}

    def style_body(self, name, ws):
        return "<sld/>"


def test_import_writes_only_what_differs_from_the_defaults():
    imp = Importer(FakeGeoServer(), "uk", "uk", "data_layers", {"roads_road_link": ("roads", "road_link")})
    stores = imp.stores()
    [layer] = imp.layers(stores)
    assert layer == {"apiVersion": "gisdata/v1alpha1", "kind": "Layer", "metadata": {"name": "road-links"}, "spec": {
        "dataset": "roads", "table": "road_link",  # the project's Dataset loads this table
        "style": "roads", "styles": ["line"], "advertised": False, "attribution": "Contains OS data"}}
    [group] = imp.groups()
    assert group["spec"] == {"title": "Base", "mode": "opaque", "layers": [{"layer": "road-links", "style": "line"}]}
    assert "group base: member other:thing isn't in uk's imported layers; left out" in imp.notes
    imp.workspace_styles()
    assert imp.styles == {"roads": (".sld", "<sld/>")}


def test_import_in_another_workspace_names_it_and_its_own_tables():
    imp = Importer(FakeGeoServer(), "uk", "elsewhere", "data_layers", {})
    [layer] = imp.layers(imp.stores())
    assert layer["spec"]["workspace"] == "uk"
    assert layer["spec"]["table"] == {"schema": "data_layers", "name": "roads_road_link"}


def test_dumped_files_are_yaml_documents():
    text = dump([{"a": 1}, {"b": "é"}])
    assert text == "a: 1\n---\nb: é\n"
