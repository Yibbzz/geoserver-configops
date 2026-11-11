"""Layer details (gisdata/details.py): the files' fields, GeoServer's form, and back."""

from gisdata import details

SPEC = {
    "filter": "zone = 'FZ3'",
    "wfs": {"maxFeatures": 500, "decimals": 3, "skipNumberMatched": True, "otherCRS": ["EPSG:4326", "EPSG:3857"]},
    "metadataLinks": [{"url": "https://example.org/meta.xml", "standard": "FGDC"}],
    "httpCache": {"maxAge": 600},
    "dimensions": {"time": {"attribute": "observed", "presentation": "continuous", "nearestMatch": True}},
    "opaque": True,
    "buffer": 12,
    "attribution": {"title": "EA", "link": "https://ea.example", "logo": {"url": "https://ea.example/logo.png", "width": 20}},
}

# As GeoServer gives them back: one item as an object, CRS codes as numbers, metadata entries keyed
RESOURCE = {"cqlFilter": "zone = 'FZ3'", "maxFeatures": 500, "numDecimals": 3, "skipNumberMatched": True,
            "overridingServiceSRS": True, "responseSRS": {"string": [4326, 3857]},
            "metadataLinks": {"metadataLink": {"type": "text/html", "metadataType": "FGDC", "content": "https://example.org/meta.xml"}},
            "metadata": {"entry": [{"@key": "cachingEnabled", "$": "true"}, {"@key": "cacheAgeMax", "$": "600"},
                                   {"@key": "time", "dimensionInfo": {"enabled": True, "attribute": "observed",
                                                                       "presentation": "CONTINUOUS_INTERVAL", "nearestMatchEnabled": True,
                                                                       "defaultValue": ""}},
                                   {"@key": "other", "$": "kept"}]}}
LAYER = {"opaque": True, "metadata": {"entry": {"@key": "buffer", "$": "12"}},
         "attribution": {"title": "EA", "href": "https://ea.example", "logoURL": "https://ea.example/logo.png", "logoWidth": 20}}


def test_live_matches_the_spec():
    wanted = details.wanted_resource(SPEC)
    assert details.live_resource(RESOURCE, wanted) == wanted
    wanted = details.wanted_layer(SPEC)
    assert details.live_layer(LAYER, wanted) == wanted


def test_fields_not_in_the_file_are_not_compared():
    assert details.wanted_resource({"title": "x"}) == {}
    assert details.wanted_layer({"attribution": "plain text, handled elsewhere"}) == {}


def test_patches_are_partial_and_merge_metadata():
    patch = details.patch_resource(RESOURCE, details.wanted_resource({"httpCache": {"maxAge": 60}}))
    assert set(patch) == {"metadata"}
    entries = {e["@key"]: e for e in patch["metadata"]["entry"]}
    assert entries["cacheAgeMax"]["$"] == "60" and entries["other"]["$"] == "kept" and "time" in entries
    patch = details.patch_resource({}, details.wanted_resource({"wfs": {"otherCRS": ["EPSG:27700"]}}))
    assert patch == {"overridingServiceSRS": True, "responseSRS": {"string": ["27700"]}}
    assert details.patch_layer({}, {"buffer": 4}) == {"metadata": {"entry": [{"@key": "buffer", "$": "4"}]}}


def test_drift_shows_by_field():
    wanted = details.wanted_resource(SPEC)
    edited = {**RESOURCE, "maxFeatures": 10, "cqlFilter": None}
    live = details.live_resource(edited, wanted)
    assert [k for k in wanted if live[k] != wanted[k]] == ["filter", "wfs.maxFeatures"]


def test_import_writes_only_what_differs_from_the_defaults():
    assert details.to_spec({"maxFeatures": 0, "numDecimals": 0, "metadata": {"entry": []}}, {"opaque": False}) == {}
    spec = details.to_spec(RESOURCE, LAYER)
    assert spec["wfs"]["otherCRS"] == ["EPSG:3857", "EPSG:4326"]
    assert spec["dimensions"]["time"] == {"attribute": "observed", "presentation": "continuous", "nearestMatch": True}
    assert spec["attribution"] == {"title": "EA", "link": "https://ea.example", "logo": {"url": "https://ea.example/logo.png", "width": 20}}
    # and what import writes, plan reads back the same
    assert details.live_resource(RESOURCE, details.wanted_resource(spec)) == details.wanted_resource(spec)
