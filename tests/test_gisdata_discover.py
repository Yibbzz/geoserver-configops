"""`gisdata discover`, against a fake OS Downloads API and small local archives."""

import io
import json
import shutil
import subprocess
import textwrap
import zipfile
from pathlib import Path

import pytest

from gisdata import discover, remote, secrets
from gisdata.cli import main
from gisdata.sources import Download, NotFound, SourceError, ckan, os_downloads
from gisdata.sources.ckan import CKAN
from gisdata.sources.formats import classify
from gisdata.sources.os_downloads import OSDownloads, choose
from gisdata.sources.url import URL

BASE = os_downloads.BASE_API
SIGNED = "https://storage.example/oproad_gpkg_gb.zip?sig=abc"

PRODUCTS = {
    "OpenRoads": {
        "id": "OpenRoads", "name": "OS Open Roads", "version": "2026-10",
        "description": ["Roads."],
        "formats": [{"format": "ESRI® Shapefile"}, {"format": "GeoPackage"}, {"format": "Vector Tiles", "subformat": "(MBTiles)"}],
    },
    "MiniScale": {"id": "MiniScale", "name": "MiniScale®", "version": "2026-01", "formats": [{"format": "GeoTIFF"}]},
}

DOWNLOADS = {
    "OpenRoads": [
        {"format": "ESRI® Shapefile", "area": "GB", "fileName": "oproad_essh_gb.zip", "size": 585789648,
         "url": f"{BASE}/OpenRoads/downloads?area=GB&format=ESRI%C2%AE+Shapefile&redirect"},
        {"format": "GeoPackage", "area": "GB", "fileName": "oproad_gpkg_gb.zip", "size": 1010630367, "md5": "0c7a",
         "url": f"{BASE}/OpenRoads/downloads?area=GB&format=GeoPackage&redirect"},
        {"format": "Vector Tiles", "subformat": "(MBTiles)", "area": "GB", "fileName": "oproad_mbtiles_gb.zip",
         "size": 1352985511, "url": f"{BASE}/OpenRoads/downloads?area=GB&format=Vector+Tiles&redirect"},
    ],
    "MiniScale": [{"format": "GeoTIFF", "area": "GB", "fileName": "minisc.zip", "url": f"{BASE}/MiniScale/downloads?redirect"}],
}

LAYERS = [
    remote.Layer("motorway_junction", "Data/oproad_gb.gpkg", "Point", "EPSG:27700", 668),
    remote.Layer("road_link", "Data/oproad_gb.gpkg", "LineString", "EPSG:27700", 3980725),
]


class FakeResponse:
    def __init__(self, data=None, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}
        self.is_redirect = status in (301, 302, 303, 307, 308)

    def json(self):
        return self.data

    def close(self):
        pass


class FakeOS:
    """Stands in for requests.Session against the OS Downloads API."""

    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None, **kwargs):
        self.calls.append((url, params))
        if url == BASE:
            return FakeResponse([{"id": k, "name": v["name"], "url": f"{BASE}/{k}"} for k, v in PRODUCTS.items()])
        if "/downloads?" in url:
            return FakeResponse(status=307, headers={"Location": SIGNED})
        pid = url.removeprefix(BASE + "/")
        if pid.endswith("/downloads"):
            pid = pid.removesuffix("/downloads")
            return FakeResponse(DOWNLOADS[pid]) if pid in DOWNLOADS else FakeResponse(status=404)
        return FakeResponse(PRODUCTS[pid]) if pid in PRODUCTS else FakeResponse(status=404)


@pytest.fixture
def fake_os(monkeypatch, tmp_path):
    """The fake API behind every OSDownloads, an empty cache, and layers read from a fixed list."""
    session = FakeOS()
    real_init = OSDownloads.__init__
    monkeypatch.setattr(OSDownloads, "__init__", lambda self, api_key=None, url=None: real_init(self, api_key, url, session))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    reads = []

    def fake_url_layers(url, session=None, file_name=""):
        reads.append(url)
        return list(LAYERS)

    monkeypatch.setattr(remote, "url_layers", fake_url_layers)
    session.reads = reads
    return session


SOURCES = """\
apiVersion: gisdata/v1alpha1
kind: Source
metadata: { name: os }
spec: { type: os-downloads, credentials: { secret: os-api } }
---
apiVersion: gisdata/v1alpha1
kind: Source
metadata: { name: web }
spec: { type: url }
---
apiVersion: gisdata/v1alpha1
kind: Source
metadata: { name: portal }
spec: { type: ckan, url: https://ckan.example.org }
---
apiVersion: gisdata/v1alpha1
kind: Source
metadata: { name: esri }
spec: { type: arcgis-rest, url: https://maps.example.org }
"""


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "sources.yaml").write_text(SOURCES)
    return root


# --- choosing the download


def dl(fmt, area="GB", name=""):
    return Download(format=fmt, url=f"https://x/{fmt}/{area}", area=area, file_name=name)


def test_choose_prefers_geopackage_over_shapefile():
    assert choose([dl("ESRI® Shapefile"), dl("GeoPackage"), dl("Vector Tiles")]).format == "GeoPackage"


def test_choose_takes_the_whole_country_file_and_skips_formats_only_in_tiles():
    tiles = [dl("ESRI® Shapefile", area) for area in ("HP", "NT", "SU")]
    assert choose(tiles + [dl("GML", "GB")]).format == "GML"
    assert choose(tiles + [dl("ESRI® Shapefile", "GB")]).area == "GB"
    assert choose(tiles) is None


def test_choose_nothing_loadable():
    assert choose([dl("GeoTIFF"), dl("Vector Tiles")]) is None


# --- the OS client


def test_products_carry_formats_and_what_loads(fake_os):
    products = {p.id: p for p in OSDownloads().products().products}
    assert products["OpenRoads"].loads_as == "GeoPackage"
    assert products["OpenRoads"].description == "Roads."
    assert products["MiniScale"].loads_as is None


def test_product_lists_downloads_and_the_chosen_one(fake_os):
    p = OSDownloads().product("OpenRoads")
    assert [d.format for d in p.downloads] == ["ESRI® Shapefile", "GeoPackage", "Vector Tiles"]
    assert p.chosen.file_name == "oproad_gpkg_gb.zip"
    assert p.chosen.md5 == "0c7a"


def test_unknown_product(fake_os):
    with pytest.raises(NotFound):
        OSDownloads().product("OpenRaods")


def test_download_url_follows_the_redirect_without_fetching_the_file(fake_os):
    client = OSDownloads()
    assert client.download_url(client.product("OpenRoads").chosen) == SIGNED


def test_api_key_is_sent_only_when_given(fake_os):
    OSDownloads().product("OpenRoads")
    assert all(params == {} for _, params in fake_os.calls)
    fake_os.calls.clear()
    OSDownloads(api_key="k").product("OpenRoads")
    assert all(params == {"key": "k"} for _, params in fake_os.calls)


def test_refused_key_is_explained():
    class Refusing:
        def get(self, *a, **k):
            return FakeResponse(status=401)

    with pytest.raises(SourceError, match="check the API key"):
        OSDownloads(session=Refusing()).products()

    with pytest.raises(SourceError, match="no resources to pick"):
        OSDownloads(session=Refusing()).product("OpenRoads", resource="x")


def test_search_filters_os_products(fake_os):
    listing = OSDownloads().products(search="roads")
    assert [p.id for p in listing.products] == ["OpenRoads"]
    assert listing.total == 1


# --- secrets and naming the source


def test_secret_from_environment_then_secrets_file(tmp_path, monkeypatch):
    assert secrets.env_name("os-api", "apiKey") == "GISDATA_OS_API_APIKEY"
    (tmp_path / "secrets.yaml").write_text("os-api: { apiKey: from-file }\n")
    monkeypatch.delenv("GISDATA_OS_API_APIKEY", raising=False)
    assert secrets.lookup("os-api", "apiKey", tmp_path) == "from-file"
    monkeypatch.setenv("GISDATA_OS_API_APIKEY", "from-env")
    assert secrets.lookup("os-api", "apiKey", tmp_path) == "from-env"
    assert secrets.lookup("other", "apiKey", tmp_path) is None


def test_a_source_from_the_project_brings_its_key(proj, monkeypatch):
    monkeypatch.setenv("GISDATA_OS_API_APIKEY", "k")
    src = discover.resolve("os", proj)
    assert (src.type, src.client.api_key) == ("os-downloads", "k")


def test_a_missing_key_is_a_note_not_an_error(proj, monkeypatch, capsys):
    monkeypatch.delenv("GISDATA_OS_API_APIKEY", raising=False)
    assert discover.resolve("os", proj).client.api_key is None
    assert "GISDATA_OS_API_APIKEY" in capsys.readouterr().err


def test_a_source_type_works_without_a_project(tmp_path):
    assert discover.resolve("os-downloads", tmp_path).type == "os-downloads"


def test_unknown_source_and_a_type_that_cannot_list(proj):
    with pytest.raises(discover.DiscoverError, match="no Source named 'osm'") as e:
        discover.resolve("osm", proj)
    assert e.value.code == 2
    with pytest.raises(discover.DiscoverError, match="type 'arcgis-rest', which can't be discovered"):
        discover.resolve("esri", proj)


# --- the command


def test_listing_products(fake_os, proj, capsys):
    assert main(["discover", "-C", str(proj), "os"]) == 0
    out = capsys.readouterr().out
    assert "OpenRoads  2026-10  GeoPackage  OS Open Roads" in out
    assert "MiniScale  2026-01  -" in out
    assert "2 products from os; 1 have nothing gisdata can load" in out


def test_one_product_shows_downloads_and_layers(fake_os, proj, capsys):
    assert main(["discover", "-C", str(proj), "os", "OpenRoads"]) == 0
    out = capsys.readouterr().out
    assert "OS Open Roads (OpenRoads), version 2026-10" in out
    assert "  * GeoPackage: oproad_gpkg_gb.zip, 1.0 GB" in out
    assert "road_link          LineString  EPSG:27700  3,980,725" in out
    assert fake_os.reads == [SIGNED]


def test_layers_are_cached_per_version(fake_os, proj, capsys):
    main(["discover", "-C", str(proj), "os", "OpenRoads"])
    main(["discover", "-C", str(proj), "os", "OpenRoads"])
    assert len(fake_os.reads) == 1
    main(["discover", "-C", str(proj), "os", "OpenRoads", "--refresh"])
    assert len(fake_os.reads) == 2
    PRODUCTS["OpenRoads"]["version"] = "2026-11"
    try:
        main(["discover", "-C", str(proj), "os", "OpenRoads"])
    finally:
        PRODUCTS["OpenRoads"]["version"] = "2026-10"
    assert len(fake_os.reads) == 3


def test_no_layers_reads_nothing(fake_os, proj, capsys):
    assert main(["discover", "-C", str(proj), "os", "OpenRoads", "--no-layers"]) == 0
    assert fake_os.reads == []
    assert "LAYER" not in capsys.readouterr().out


def test_json(fake_os, proj, capsys):
    main(["discover", "-C", str(proj), "os", "OpenRoads", "--format", "json"])
    out = json.loads(capsys.readouterr().out)
    assert out["download"]["fileName"] == "oproad_gpkg_gb.zip"
    assert [layer["name"] for layer in out["layers"]] == ["motorway_junction", "road_link"]
    main(["discover", "-C", str(proj), "os", "--format", "json"])
    listing = json.loads(capsys.readouterr().out)
    assert {p["id"]: p["loadsAs"] for p in listing["products"]} == {"OpenRoads": "GeoPackage", "MiniScale": None}


def test_a_product_with_nothing_to_load(fake_os, proj, capsys):
    assert main(["discover", "-C", str(proj), "os", "MiniScale"]) == 0
    assert "No download gisdata can load" in capsys.readouterr().out
    assert fake_os.reads == []


def test_unknown_product_says_how_to_list(fake_os, proj, capsys):
    assert main(["discover", "-C", str(proj), "os", "OpenRaods"]) == 1
    assert "os has no product 'OpenRaods'; find one with: gisdata discover os --search <words>" in capsys.readouterr().err


# --- reading archives (real GDAL on small local files)

needs_gdal = pytest.mark.skipif(shutil.which("ogr2ogr") is None, reason="GDAL not installed")


def geojson(geometry_type, coords):
    return json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"id": 1}, "geometry": {"type": geometry_type, "coordinates": coords}},
    ]})


@pytest.fixture
def two_layer_zip(tmp_path):
    """A zip like OS's: Data/<name>.gpkg with two layers, plus docs."""
    gpkg = tmp_path / "roads.gpkg"
    for layer, gtype, coords in [("road_link", "LineString", [[0, 0], [1, 1]]), ("road_node", "Point", [0, 0])]:
        src = tmp_path / f"{layer}.geojson"
        src.write_text(geojson(gtype, coords))
        subprocess.run(["ogr2ogr", "-f", "GPKG", "-update", "-append", str(gpkg), str(src), "-nln", layer, "-a_srs", "EPSG:27700"],
                       check=True, capture_output=True)
    archive = tmp_path / "roads.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(gpkg, "Data/roads.gpkg")
        z.writestr("Doc/readme.txt", "read me")
    return archive


@needs_gdal
def test_layers_of_a_zipped_geopackage(two_layer_zip):
    with open(two_layer_zip, "rb") as f:
        layers = remote.archive_layers(f, str(two_layer_zip))
    assert [(x.name, x.file, x.geometry, x.crs, x.features) for x in layers] == [
        ("road_link", "Data/roads.gpkg", "LineString", "EPSG:27700", 1),
        ("road_node", "Data/roads.gpkg", "Point", "EPSG:27700", 1),
    ]


@needs_gdal
def test_layers_of_a_file_that_is_not_a_zip(two_layer_zip, tmp_path):
    gpkg = tmp_path / "roads.gpkg"
    with open(gpkg, "rb") as f:
        assert [x.name for x in remote.archive_layers(f, str(gpkg))] == ["road_link", "road_node"]


def test_a_zip_without_data_files(tmp_path):
    archive = tmp_path / "docs.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("readme.txt", "nothing here")
    with open(archive, "rb") as f, pytest.raises(remote.RemoteError, match="no data files"):
        remote.archive_layers(f, str(archive))


def test_only_data_files_are_read():
    entries = ["Data/a.shp", "Data/a.dbf", "Data/a.shx", "Doc/licence.txt", "Data/B.GPKG", "Data/._a.shp"]
    assert remote.data_files(entries) == ["Data/B.GPKG", "Data/a.shp"]


class RangeServer:
    """Serves bytes over fake HTTP range requests, counting what it sends."""

    def __init__(self, data: bytes, ranges: bool = True):
        self.data, self.ranges, self.sent = data, ranges, 0

    def get(self, url, headers=None, timeout=None, stream=False):
        if not self.ranges or "Range" not in (headers or {}):
            r = FakeResponse(status=200, headers={"Content-Length": str(len(self.data))})
            r.content = self.data
            r.raise_for_status = lambda: None
            r.iter_content = lambda n: [self.data[i:i + n] for i in range(0, len(self.data), n)]
            r.__enter__ = lambda: r
            return r
        start, end = (int(x) for x in headers["Range"].removeprefix("bytes=").split("-"))
        chunk = self.data[start:end + 1]
        self.sent += len(chunk)
        r = FakeResponse(status=206, headers={"Content-Range": f"bytes {start}-{end}/{len(self.data)}"})
        r.content = chunk
        r.raise_for_status = lambda: None
        return r


def test_zip_listing_over_http_reads_only_the_end(tmp_path):
    big = io.BytesIO()
    with zipfile.ZipFile(big, "w", zipfile.ZIP_STORED) as z:
        z.writestr("Data/roads.gpkg", b"x" * 2_000_000)
        z.writestr("Doc/readme.txt", "hi")
    server = RangeServer(big.getvalue())
    with io.BufferedReader(remote.HttpFile("https://x/roads.zip", server), buffer_size=64 * 1024) as f:
        assert remote.zip_entries(f) == ["Data/roads.gpkg", "Doc/readme.txt"]
    assert server.sent < 200_000


def test_a_server_without_ranges_is_explained():
    with pytest.raises(remote.NoRanges, match="partial downloads") as e:
        remote.HttpFile("https://x/roads.zip", RangeServer(b"abc", ranges=False))
    assert e.value.size == 3


def test_crs_names():
    assert remote.crs_name({"projjson": {"id": {"authority": "EPSG", "code": 27700}}}) == "EPSG:27700"
    assert remote.crs_name({"projjson": {"name": "Custom"}}) == "Custom"
    assert remote.crs_name(None) is None


class WholeFileServer(RangeServer):
    """A server that ignores Range; usable as a context manager like requests' responses."""

    def get(self, url, headers=None, timeout=None, stream=False):
        server = self

        class Response(FakeResponse):
            content = server.data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, n):
                return [server.data[i:i + n] for i in range(0, len(server.data), n)]

        return Response(status=200, headers={"Content-Length": str(len(self.data))})


@needs_gdal
def test_a_server_without_ranges_has_the_file_fetched_whole(two_layer_zip):
    server = WholeFileServer(two_layer_zip.read_bytes(), ranges=False)
    layers = remote.url_layers("https://x/roads.zip", server)
    assert [x.name for x in layers] == ["road_link", "road_node"]


def test_too_large_to_fetch_whole(monkeypatch):
    monkeypatch.setattr(remote, "MAX_DOWNLOAD", 10)
    with pytest.raises(remote.RemoteError, match="too large"):
        remote.url_layers("https://x/roads.zip", WholeFileServer(b"x" * 100, ranges=False))


def test_file_geodatabase_folders_are_datasets():
    entries = ["Flood.gdb/a00000001.gdbtable", "Flood.gdb/a00000001.gdbtablx", "Doc/readme.txt", "x/y.shp"]
    assert remote.data_files(entries) == ["Flood.gdb", "x/y.shp"]


# --- formats


@pytest.mark.parametrize("fmt, url, mimetype, name, expected", [
    ("GeoJSON", "https://x/a", None, "", "GeoJSON"),
    ("SHP", "https://x/a", None, "", "Shapefile"),
    ("", "https://x/data.gpkg", None, "", "GeoPackage"),
    ("https://www.iana.org/assignments/media-types/application/zip", "https://x/a", None, "", "ZIP"),
    ("ZIP", "https://x/download", None, "roads.gpkg.zip", "GeoPackage"),  # the name says what the zip holds
    ("ZIP", "https://x/Flood.gdb.zip", None, "", "File Geodatabase"),
    ("", "https://x/a", "application/geo+json", "", "GeoJSON"),
    ("HTML", "https://x/page", None, "", None),
    ("WMS", "https://x/wms", None, "", None),
    ("CSV", "https://x/a.csv", None, "", None),
])
def test_classify(fmt, url, mimetype, name, expected):
    assert classify(fmt, url, mimetype, name) == expected


# --- url sources


class LinkServer:
    """Answers the one-byte probe a url source makes."""

    def __init__(self, status=206, headers=None, final_url=None):
        self.status, self.headers, self.final_url, self.requests = status, headers or {}, final_url, []

    def get(self, url, headers=None, stream=False, timeout=None, allow_redirects=True):
        self.requests.append((url, headers))
        r = FakeResponse(status=self.status, headers=self.headers)
        r.url = self.final_url or url
        return r


def test_a_link_is_its_own_product():
    server = LinkServer(headers={"Content-Range": "bytes 0-0/848600", "ETag": '"v1"', "Content-Type": "application/zip"},
                        final_url="https://cdn.example/files/Conservation%20Areas.zip")
    p = URL(session=server).product("https://example.org/get?id=7")
    assert (p.name, p.version, p.loads_as, p.chosen.size) == ("Conservation Areas.zip", "v1", "ZIP", 848600)


def test_a_link_to_a_web_page_is_not_loadable():
    p = URL(session=LinkServer(status=200, headers={"Content-Type": "text/html; charset=utf-8"})).product("https://example.org/")
    assert p.chosen is None and p.downloads[0].loadable is False


def test_a_url_source_has_no_product_list():
    with pytest.raises(SourceError, match="no list of products"):
        URL().products()
    with pytest.raises(SourceError, match="isn't a link"):
        URL().product("conservation-areas")


def test_a_broken_link_says_so():
    with pytest.raises(SourceError, match="may be broken at the source"):
        URL(session=LinkServer(status=400)).product("https://example.org/a.zip")


def test_url_names_the_file_from_content_disposition():
    server = LinkServer(headers={"Content-Disposition": 'attachment; filename="roads.gpkg"', "Content-Range": "bytes 0-0/10"})
    assert URL(session=server).product("https://example.org/download").loads_as == "GeoPackage"


# --- CKAN


def resource(rid, fmt, url, name="", size=None):
    return {"id": rid, "format": fmt, "url": url, "name": name, "size": size}


PACKAGES = {
    "conservation-areas": {
        "id": "uuid-1", "name": "conservation-areas", "title": "Conservation Areas", "metadata_modified": "2026-11-04T10:00:00.1",
        "notes": "Areas.", "resources": [
            resource("r-shp", "SHP", "https://files.example/ca.zip", "Conservation Areas"),
            resource("r-gpkg", "Geopackages", "https://files.example/ca.gpkg", "Conservation Areas GPKG", 4096),
            resource("r-wms", "WMS", "https://maps.example/wms", "Web Map Service"),
            resource("r-page", "HTML", "https://council.example/page", "About"),
        ],
    },
    "only-services": {"id": "uuid-2", "name": "only-services", "title": {"en": "Only services", "fr": "Services"},
                      "resources": [resource("r1", "WFS", "https://x/wfs")]},
}


class FakeCKAN:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.calls.append((url, params, headers))
        if url.endswith("/package_search"):
            words = (params.get("q") or "").split()
            found = [p for p in PACKAGES.values() if all(w in p["name"] for w in words)]
            return FakeResponse({"success": True, "result": {"count": len(found), "results": found[: params["rows"]]}})
        if url.endswith("/package_show"):
            pkg = PACKAGES.get(params["id"])
            if pkg is None:
                return FakeResponse({"success": False, "error": {"__type": "Not Found Error", "message": "Not found"}}, status=404)
            return FakeResponse({"success": True, "result": pkg})
        r = FakeResponse(status=206, headers={"Content-Range": "bytes 0-0/4096"})  # a file host
        r.url = url
        return r


@pytest.fixture
def fake_ckan(monkeypatch, tmp_path):
    session = FakeCKAN()
    real_init = CKAN.__init__
    monkeypatch.setattr(CKAN, "__init__", lambda self, api_key=None, url=None: real_init(self, api_key, url, session))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return session


def test_ckan_api_address():
    assert ckan.action_base("https://data.gov.uk") == "https://data.gov.uk/api/3/action"
    assert ckan.action_base("https://portal.example/api/3/action/") == "https://portal.example/api/3/action"


def test_ckan_needs_the_portal_address():
    with pytest.raises(SourceError, match="needs the portal's address"):
        CKAN()


def test_ckan_search(fake_ckan):
    listing = CKAN(url="https://ckan.example.org").products(search="conservation", limit=5)
    assert [(p.id, p.loads_as) for p in listing.products] == [("conservation-areas", "GeoPackage")]
    url, params, _ = fake_ckan.calls[0]
    assert url == "https://ckan.example.org/api/3/action/package_search"
    assert params == {"q": "conservation", "rows": 5}


def test_ckan_package_picks_the_best_file(fake_ckan):
    p = CKAN(url="https://ckan.example.org").product("conservation-areas")
    assert p.chosen.id == "r-gpkg"
    assert [d.loadable for d in p.downloads] == [True, True, False, False]
    assert p.version.startswith("2026-11-04")


def test_ckan_resource_by_id_or_name(fake_ckan):
    client = CKAN(url="https://ckan.example.org")
    assert client.product("conservation-areas", resource="r-shp").chosen.format == "Shapefile"
    assert client.product("conservation-areas", resource="conservation areas").chosen.id == "r-shp"
    with pytest.raises(SourceError, match="no resource 'nope'"):
        client.product("conservation-areas", resource="nope")
    with pytest.raises(SourceError, match="not a file gisdata can load"):
        client.product("conservation-areas", resource="r-wms")


def test_ckan_titles_in_several_languages(fake_ckan):
    p = CKAN(url="https://ckan.example.org").product("only-services")
    assert p.name == "Only services" and p.chosen is None


def test_ckan_not_found(fake_ckan):
    with pytest.raises(NotFound):
        CKAN(url="https://ckan.example.org").product("missing")


def test_ckan_key_goes_only_to_the_portal(fake_ckan):
    client = CKAN(api_key="token", url="https://ckan.example.org")
    client.product("conservation-areas")
    assert fake_ckan.calls[-1][2] == {"Authorization": "token"}
    assert client._auth("https://files.example/ca.gpkg") == {}


def test_ckan_command(fake_ckan, proj, capsys, monkeypatch):
    reads = []
    monkeypatch.setattr(remote, "url_layers", lambda url, session=None, file_name="": reads.append((url, file_name)) or list(LAYERS))
    assert main(["discover", "-C", str(proj), "portal", "conservation-areas"]) == 0
    out = capsys.readouterr().out
    assert "Conservation Areas (conservation-areas), version 2026-11-04" in out
    assert "  * GeoPackage: Conservation Areas GPKG, ca.gpkg, 4.1 KB  [r-gpkg]" in out
    assert "WMS: Web Map Service (not a file gisdata loads)" in out
    assert "resource: r-gpkg" in out
    assert reads == [("https://files.example/ca.gpkg", "ca.gpkg")]


def test_ckan_listing_by_type_with_url_and_options_anywhere(fake_ckan, tmp_path, capsys):
    assert main(["discover", "ckan", "--search", "conservation", "--url", "https://ckan.example.org"]) == 0
    out = capsys.readouterr().out
    assert "conservation-areas  2026-11-04  GeoPackage  Conservation Areas" in out
    assert "Layers inside one: gisdata discover ckan --url https://ckan.example.org <product>" in out
    assert main(["discover", "ckan", "--url", "https://ckan.example.org", "conservation-areas", "--no-layers"]) == 0


def test_layers_error_still_shows_downloads(fake_ckan, proj, capsys, monkeypatch):
    def broken(url, session=None, file_name=""):
        raise remote.RemoteError("the server doesn't allow partial downloads (HTTP 200), and the file is too large")

    monkeypatch.setattr(remote, "url_layers", broken)
    assert main(["discover", "-C", str(proj), "portal", "conservation-areas"]) == 1
    captured = capsys.readouterr()
    assert "* GeoPackage" in captured.out
    assert "couldn't read layers: the server doesn't allow partial downloads" in captured.err


def test_search_and_resource_need_the_right_form(fake_ckan, proj, capsys):
    assert main(["discover", "-C", str(proj), "portal", "conservation-areas", "--search", "x"]) == 2
    assert main(["discover", "-C", str(proj), "portal", "--resource", "r-shp"]) == 2
    assert main(["discover", "ckan"]) == 2  # no portal address


def test_secret_from_a_mounted_kubernetes_secret(tmp_path, monkeypatch):
    (tmp_path / "geoserver-pgconfig-secret").mkdir()
    (tmp_path / "geoserver-pgconfig-secret" / "password").write_text("s3cret\n")
    monkeypatch.setenv("GISDATA_SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("GISDATA_GEOSERVER_PGCONFIG_SECRET_PASSWORD", raising=False)
    assert secrets.lookup("geoserver-pgconfig-secret", "password") == "s3cret"
    assert secrets.lookup("geoserver-pgconfig-secret", "host") is None
    monkeypatch.setenv("GISDATA_GEOSERVER_PGCONFIG_SECRET_PASSWORD", "override")
    assert secrets.lookup("geoserver-pgconfig-secret", "password") == "override"
