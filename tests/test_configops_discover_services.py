"""`configops discover` on web services: WFS and OGC API Features, faked."""

import pytest

from configops import crs, discover, remote
from configops.cli import main
from configops.sources import NotFound, Product, SourceError, http_session
from configops.sources.ckan import CKAN
from configops.sources.formats import service, service_address
from configops.sources.ogcapi import OGCAPIFeatures, is_features
from configops.sources.wfs import WFS, parse_capabilities


class FakeResponse:
    def __init__(self, data=None, status=200, content=b""):
        self.data, self.status_code, self.content = data, status, content

    def json(self):
        if self.data is None:
            raise ValueError("not JSON")
        return self.data


# --- names


@pytest.mark.parametrize("name, expected", [
    ("urn:ogc:def:crs:EPSG::27700", "EPSG:27700"),
    ("urn:x-ogc:def:crs:EPSG:27700", "EPSG:27700"),
    ("EPSG:4326", "EPSG:4326"),
    ("http://www.opengis.net/def/crs/EPSG/0/27700", "EPSG:27700"),
    ("http://www.opengis.net/gml/srs/epsg.xml#27700", "EPSG:27700"),
    ("http://www.opengis.net/def/crs/OGC/1.3/CRS84", "CRS84"),
    ("urn:ogc:def:crs:OGC:1.3:CRS84", "CRS84"),
    ("LOCAL_CS", "LOCAL_CS"),
    (None, None),
])
def test_crs_names(name, expected):
    assert crs.short(name) == expected


@pytest.mark.parametrize("fmt, url, name, expected", [
    ("WFS", "https://x/a", "", "WFS"),
    ("OGC WFS", "https://x/a", "", "WFS"),
    ("", "https://x/spatialdata/flood/wfs", "", "WFS"),
    ("", "https://x/ows?service=WFS&request=GetCapabilities", "", "WFS"),
    ("", "https://x/spatialdata/flood/ogc/features/v1", "", "OGC API Features"),
    ("", "https://x/api", "OGC API - Features service", "OGC API Features"),
    ("", "https://x/spatialdata/flood/wms", "", "WMS"),
    ("", "https://x/arcgis/rest/services/Roads/FeatureServer/0", "", "ArcGIS REST"),
    ("", "https://x/data.zip", "", None),
])
def test_services_are_recognised(fmt, url, name, expected):
    assert service(fmt, url, name) == expected


def test_service_addresses_drop_request_details():
    assert service_address("WFS", "https://x/ows?service=WFS&request=GetCapabilities&map=roads") == "https://x/ows?map=roads"
    assert service_address("WFS", "https://x/wfs?SERVICE=wfs&VERSION=2.0.0&typeNames=a:b") == "https://x/wfs"
    assert service_address("OGC API Features", "https://x/ogc/v1/collections/roads/items?f=json") == "https://x/ogc/v1"
    assert service_address("OGC API Features", "https://x/ogc/v1/") == "https://x/ogc/v1"


def test_sessions_retry_dropped_connections():
    retry = http_session().get_adapter("https://x").max_retries
    assert retry.total == 2 and 503 in retry.status_forcelist


# --- WFS

CAPS_20 = b"""<?xml version="1.0"?>
<wfs:WFS_Capabilities version="2.0.0" xmlns:wfs="http://www.opengis.net/wfs/2.0">
  <FeatureTypeList xmlns="http://www.opengis.net/wfs/2.0">
    <FeatureType>
      <Name>geo:faults</Name><Title>Faults</Title><Abstract>Mapped faults.</Abstract>
      <DefaultCRS>urn:ogc:def:crs:EPSG::27700</DefaultCRS>
    </FeatureType>
    <FeatureType>
      <Name>geo:bedrock</Name><Title>Bedrock</Title><DefaultCRS>urn:ogc:def:crs:EPSG::27700</DefaultCRS>
    </FeatureType>
  </FeatureTypeList>
</wfs:WFS_Capabilities>"""

CAPS_11 = b"""<WFS_Capabilities version="1.1.0" xmlns="http://www.opengis.net/wfs">
  <FeatureTypeList><FeatureType>
    <Name>ds:Ancient_Woodland</Name><Title>Ancient woodland</Title><DefaultSRS>urn:x-ogc:def:crs:EPSG:27700</DefaultSRS>
  </FeatureType></FeatureTypeList>
</WFS_Capabilities>"""

CAPS_10 = b"""<WFS_Capabilities version="1.0.0" xmlns="http://www.opengis.net/wfs">
  <FeatureTypeList><FeatureType><Name>roads</Name><SRS>EPSG:4326</SRS></FeatureType></FeatureTypeList>
</WFS_Capabilities>"""


def test_capabilities_of_each_version():
    assert parse_capabilities(CAPS_20) == ("2.0.0", [
        {"name": "geo:faults", "title": "Faults", "abstract": "Mapped faults.", "crs": "EPSG:27700"},
        {"name": "geo:bedrock", "title": "Bedrock", "abstract": "", "crs": "EPSG:27700"},
    ])
    assert parse_capabilities(CAPS_11)[1][0]["crs"] == "EPSG:27700"
    assert parse_capabilities(CAPS_10)[1] == [{"name": "roads", "title": "roads", "abstract": "", "crs": "EPSG:4326"}]


@pytest.mark.parametrize("body, message", [
    (b"<ows:ExceptionReport xmlns:ows='http://www.opengis.net/ows/1.1'><ows:Exception><ows:ExceptionText>No such map</ows:ExceptionText></ows:Exception></ows:ExceptionReport>",
     "answered with an error: No such map"),
    (b"<html><body>Login</body></html>", "<html>, not a WFS capabilities document"),
    (b"not xml", "not XML"),
])
def test_capabilities_that_are_not(body, message):
    with pytest.raises(SourceError, match=message):
        parse_capabilities(body)


class FakeWFS:
    def __init__(self, caps=CAPS_20, status=200):
        self.caps, self.status, self.calls = caps, status, []

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append((url, params))
        return FakeResponse(status=self.status, content=self.caps)


def test_wfs_lists_and_searches_feature_types():
    client = WFS(url="https://x/wfs?service=WFS&request=GetCapabilities", session=FakeWFS())
    assert client.url == "https://x/wfs"
    listing = client.products()
    assert [(p.id, p.name, p.loads_as) for p in listing.products] == [("geo:faults", "Faults", "WFS"), ("geo:bedrock", "Bedrock", "WFS")]
    assert [p.id for p in client.products(search="mapped").products] == ["geo:faults"]


def test_wfs_product_with_or_without_prefix():
    client = WFS(url="https://x/wfs", session=FakeWFS())
    assert client.product("geo:faults").chosen.subformat == "2.0.0"
    assert client.product("faults").id == "geo:faults"
    with pytest.raises(NotFound):
        client.product("rivers")
    with pytest.raises(SourceError, match="no resources"):
        client.product("faults", resource="x")


def test_wfs_layers_come_from_gdal(monkeypatch):
    seen = []

    def fake_ogrinfo(path, layer=None):
        seen.append((path, layer))
        return [{"name": layer, "featureCount": 2743, "geometryFields": [{"type": "MultiLineString", "coordinateSystem": None}]}]

    monkeypatch.setattr(remote, "ogrinfo", fake_ogrinfo)
    client = WFS(url="https://x/wfs?map=geo", session=FakeWFS())
    layers = client.layers(client.product("faults"))
    assert seen == [("WFS:https://x/wfs?map=geo", "geo:faults")]
    # no CRS from GDAL: the capabilities' default
    assert [(x.name, x.geometry, x.crs, x.features) for x in layers] == [("geo:faults", "MultiLineString", "EPSG:27700", 2743)]


def test_wfs_needs_an_address_and_a_wfs():
    with pytest.raises(SourceError, match="needs the service's address"):
        WFS()
    with pytest.raises(SourceError, match="HTTP 404; is it a WFS address"):
        WFS(url="https://x/nope", session=FakeWFS(status=404)).products()


# --- OGC API Features

COLLECTIONS = {
    "roads": {"id": "roads", "title": "Roads", "description": "All roads", "itemType": "feature",
              "storageCrs": "http://www.opengis.net/def/crs/EPSG/0/27700", "links": []},
    "flood": {"id": "flood", "title": "Flood zones", "links": [{"rel": "items", "href": "x"}]},  # no itemType
    "dem": {"id": "dem", "title": "Elevation", "links": [{"rel": "coverage", "href": "x"}]},
    "meta": {"id": "meta", "title": "Metadata", "itemType": "record", "links": [{"rel": "items", "href": "x"}]},
}


class FakeOGC:
    BASE = "https://x/ogc/v1"

    def __init__(self, page_size=10):
        self.page_size, self.calls = page_size, []

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        self.calls.append((url, params, headers))
        if url.startswith(f"{self.BASE}/collections?page="):
            page = int(url.rsplit("=", 1)[1])
            return self.collections(page)
        if url == f"{self.BASE}/collections":
            return self.collections(0)
        rest = url.removeprefix(f"{self.BASE}/collections/")
        cid, _, items = rest.partition("/")
        if cid not in COLLECTIONS:
            return FakeResponse(status=404)
        if items == "items":
            if cid == "flood":
                return FakeResponse({"type": "FeatureCollection", "features": [{"geometry": {"type": "MultiPolygon"}}]})
            return FakeResponse({"type": "FeatureCollection", "numberMatched": 412, "features": [{"geometry": {"type": "LineString"}}]})
        return FakeResponse(COLLECTIONS[cid])

    def collections(self, page):
        values = list(COLLECTIONS.values())
        chunk = values[page * self.page_size:(page + 1) * self.page_size]
        links = [{"rel": "next", "href": f"{self.BASE}/collections?page={page + 1}"}] if (page + 1) * self.page_size < len(values) else []
        return FakeResponse({"collections": chunk, "links": links})


def test_feature_collections_only():
    assert [c for c in COLLECTIONS if is_features(COLLECTIONS[c])] == ["roads", "flood"]


@pytest.mark.parametrize("page_size", [10, 1])
def test_ogc_lists_feature_collections_across_pages(page_size):
    client = OGCAPIFeatures(url="https://x/ogc/v1/collections/roads/items", session=FakeOGC(page_size))
    assert client.url == "https://x/ogc/v1"
    assert [p.id for p in client.products().products] == ["roads", "flood"]


def test_ogc_product_and_layer():
    session = FakeOGC()
    client = OGCAPIFeatures(url=FakeOGC.BASE, session=session)
    p = client.product("roads")
    assert p.chosen.service and p.chosen.url == "https://x/ogc/v1/collections/roads/items"
    [layer] = client.layers(p)
    assert (layer.name, layer.geometry, layer.crs, layer.features) == ("roads", "LineString", "EPSG:27700", 412)
    assert session.calls[-1][1] == {"limit": 1}
    assert "application/geo+json" in session.calls[-1][2]["Accept"]


def test_ogc_layer_without_count_or_storage_crs():
    client = OGCAPIFeatures(url=FakeOGC.BASE, session=FakeOGC())
    [layer] = client.layers(client.product("flood"))
    assert (layer.geometry, layer.crs, layer.features) == ("MultiPolygon", "CRS84", None)


def test_ogc_collection_that_is_not_features_or_missing():
    client = OGCAPIFeatures(url=FakeOGC.BASE, session=FakeOGC())
    with pytest.raises(SourceError, match="holds record, not features"):
        client.product("meta")
    with pytest.raises(NotFound):
        client.product("rivers")


def test_ogc_not_an_api():
    class Html:
        def get(self, *a, **k):
            return FakeResponse(None)

    with pytest.raises(SourceError, match="is it an OGC API address"):
        OGCAPIFeatures(url="https://x/", session=Html()).products()


# --- the command


@pytest.fixture
def fake_services(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ogc = FakeOGC()
    real_ogc = OGCAPIFeatures.__init__
    monkeypatch.setattr(OGCAPIFeatures, "__init__", lambda self, api_key=None, url=None: real_ogc(self, api_key, url, ogc))
    real_wfs = WFS.__init__
    monkeypatch.setattr(WFS, "__init__", lambda self, api_key=None, url=None: real_wfs(self, api_key, url, FakeWFS()))
    monkeypatch.setattr(remote, "url_layers", lambda *a, **k: pytest.fail("a service needs no file"))
    return ogc


def test_service_product_output(fake_services, capsys):
    assert main(["discover", "ogc-api-features", "--url", FakeOGC.BASE, "roads"]) == 0
    out = capsys.readouterr().out
    assert "Roads (roads)\n  OGC API Features: https://x/ogc/v1/collections/roads/items\n  All roads" in out
    assert "roads  LineString  EPSG:27700  412" in out
    assert "In a Dataset: `product: roads`" in out


def test_service_listing_hint_uses_the_tidied_quoted_address(fake_services, capsys):
    assert main(["discover", "wfs", "--url", "https://x/wfs?service=WFS&request=GetCapabilities&map=a b"]) == 0
    out = capsys.readouterr().out
    assert "2 products from wfs" in out
    assert "Layers inside one: configops discover wfs --url 'https://x/wfs?map=a+b' <product>" in out


def test_a_wfs_source_from_the_project(fake_services, tmp_path, capsys):
    (tmp_path / "sources.yaml").write_text(
        "apiVersion: configops/v1alpha1\nkind: Source\nmetadata: { name: bgs }\nspec: { type: wfs, url: https://x/wfs }\n")
    assert main(["discover", "-C", str(tmp_path), "bgs", "--search", "bedrock"]) == 0
    assert "geo:bedrock" in capsys.readouterr().out


# --- CKAN packages that offer services

PACKAGE = {
    "name": "flood-zones", "title": "Flood zones", "metadata_modified": "2026-10-22T14:05:28",
    "resources": [
        {"id": "r-wfs", "name": "FloodZones_WFS", "format": "", "url": "https://x/flood/wfs"},
        {"id": "r-ogc", "name": "OGC API - Features service", "format": "", "url": "https://x/flood/ogc/features/v1"},
        {"id": "r-wms", "name": "FloodZones_WMS", "format": "", "url": "https://x/flood/wms"},
    ],
}


class FakePortal:
    def get(self, url, params=None, headers=None, timeout=None, **kw):
        if url.endswith("package_search"):
            return FakeResponse({"success": True, "result": {"count": 1, "results": [PACKAGE]}})
        return FakeResponse({"success": True, "result": PACKAGE})


@pytest.fixture
def portal(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    real = CKAN.__init__
    monkeypatch.setattr(CKAN, "__init__", lambda self, api_key=None, url=None: real(self, api_key, url, FakePortal()))


def test_ckan_services_say_how_to_discover_them(portal, capsys):
    assert main(["discover", "ckan", "--url", "https://ckan.example", "flood-zones"]) == 0
    out = capsys.readouterr().out
    assert "WFS: FloodZones_WFS (a service, not a file)  [r-wfs]\n        discover it: configops discover wfs --url https://x/flood/wfs" in out
    assert "discover it: configops discover ogc-api-features --url https://x/flood/ogc/features/v1" in out
    assert "WMS: FloodZones_WMS (not a file configops loads)" in out
    assert "No download configops can load" in out


def test_ckan_listing_marks_service_only_packages(portal, capsys):
    assert main(["discover", "ckan", "--url", "https://ckan.example", "--search", "flood"]) == 0
    out = capsys.readouterr().out
    assert "flood-zones  2026-10-22  via WFS   Flood zones" in out
    assert "1 offer only a WFS or OGC API service" in out


def test_product_dataclass_has_no_service_by_default():
    assert Product("a", "a", None).chosen is None
    assert discover.version("2026-10-22T14:05:28.1") == "2026-10-22"
