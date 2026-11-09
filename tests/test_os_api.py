"""OS Downloads API source, with the HTTP calls faked."""

import pytest

import pipeline
from sources import os_api

PRODUCTS = [
    {"id": "OpenRoads", "name": "OS Open Roads", "version": "2026-04"},
    {"id": "OpenUPRN", "name": "OS Open UPRN", "version": "2026-04"},
    {"id": "Terrain50", "name": "OS Terrain 50", "version": "2025-07"},
    {"id": "BoundaryLine", "name": "Boundary-Line"},
]

DOWNLOADS = {
    "OpenRoads": [
        {"format": "ESRI® Shapefile", "url": "https://x/roads.shp.zip"},
        {"format": "GeoPackage", "url": "https://x/roads.gpkg.zip", "size": 10, "md5": "abc"},
        {"format": "Vector Tiles", "url": "https://x/roads.mbtiles"},
    ],
    "OpenUPRN": [{"format": "GeoPackage", "url": "https://x/uprn.zip"}],
    "Terrain50": [{"format": "ASCII Grid and GML (Grid)", "url": "https://x/t50.zip"}],
    "BoundaryLine": [{"format": "Vector Tiles", "url": "https://x/bl.mbtiles"}, {"format": "GML", "url": "https://x/bl.zip"}],
}


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


@pytest.fixture
def fake_api(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        if url == os_api.BASE_API:
            return FakeResponse(PRODUCTS)
        pid = url.removeprefix(os_api.BASE_API + "/").removesuffix("/downloads")
        if pid in DOWNLOADS:
            return FakeResponse(DOWNLOADS[pid])
        return FakeResponse({}, status=404)

    monkeypatch.setattr(os_api.requests, "get", fake_get)
    monkeypatch.setattr(os_api, "API_KEY", "secret-key")
    monkeypatch.setattr(os_api, "ALLOWLIST", [])
    return calls


def test_picks_preferred_format_and_skips_unusable(fake_api):
    datasets = {d["id"]: d for d in os_api.get_datasets()}
    # UPRN is block-listed, Terrain50 has no vector format
    assert set(datasets) == {"OpenRoads", "BoundaryLine"}
    roads = datasets["OpenRoads"]
    assert roads["format"] == "GeoPackage"
    assert roads["url"] == "https://x/roads.gpkg.zip"
    assert roads["md5"] == "abc"
    assert roads["version"] == "2026-04"
    # Vector Tiles is never chosen, even when listed first
    assert datasets["BoundaryLine"]["format"] == "GML"
    assert datasets["BoundaryLine"]["version"] == "unknown"


def test_datasets_carry_srs_and_api_key_header(fake_api):
    for d in os_api.get_datasets():
        assert d["srs"] == "EPSG:27700"
        assert d["headers"] == {"key": "secret-key"}
    assert all(params == {"key": "secret-key"} for _, params in fake_api)


def test_no_api_key_sends_no_key(fake_api, monkeypatch):
    monkeypatch.setattr(os_api, "API_KEY", "")
    for d in os_api.get_datasets():
        assert d["headers"] == {}


def test_allowlist_limits_products(fake_api, monkeypatch):
    monkeypatch.setattr(os_api, "ALLOWLIST", ["OpenRoads"])
    assert [d["id"] for d in os_api.get_datasets()] == ["OpenRoads"]


def test_every_preferred_format_can_be_loaded_by_the_pipeline():
    """A format the source may pick but the pipeline can't load fails at runtime."""
    assert set(os_api.FORMAT_PREFERENCE) <= set(pipeline.EXT_MAP)
