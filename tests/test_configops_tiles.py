"""Tile caching pieces without a GeoServer: blob store XML, parameter filters, caching defaults."""

import types
import xml.etree.ElementTree as ET

from configops.geoserver import filter_xml
from configops.plan import compared_filters, diff, wanted_cache
from configops.server import compare_gwc, set_gwc
from configops.tiles import live_blob_store, wanted_blob_store


def store(spec):
    return types.SimpleNamespace(spec=spec, name="tiles")


def test_azure_and_gcs_blob_stores_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIGOPS_AZ_ACCOUNTNAME", "acct")
    monkeypatch.setenv("CONFIGOPS_AZ_ACCOUNTKEY", "secret")
    xml, fields = wanted_blob_store(store({"type": "azure", "container": "c", "credentials": {"secret": "az"}}), tmp_path)
    assert ET.fromstring(xml).tag == "AzureBlobStore" and "<useHTTPS>true</useHTTPS>" in xml
    assert live_blob_store(xml) == {**fields, "type": "azure"}

    xml, fields = wanted_blob_store(store({"type": "gcs", "bucket": "b", "project": "p", "defaultCredentials": True}), tmp_path)
    assert ET.fromstring(xml).tag == "GoogleCloudStorageBlobStore" and "useHTTPS" not in xml
    assert live_blob_store(xml) == {**fields, "type": "gcs"}


def test_parameter_filters_as_geowebcache_writes_them():
    assert filter_xml("STYLES", {"type": "styles", "values": ["a"]}) == (
        "<styleParameterFilter><key>STYLES</key><defaultValue></defaultValue>"
        "<allowedStyles><string>a</string></allowedStyles></styleParameterFilter>")
    assert "<float>0.5</float>" in filter_xml("ELEVATION", {"type": "numbers", "values": [0.5], "threshold": 0.1})
    assert "<regex>.*</regex>" in filter_xml("TIME", {"type": "regex"})


def test_filters_compare_only_what_the_files_set():
    wanted = wanted_cache({"parameterFilters": {"ELEVATION": {"type": "numbers", "values": [0, 10]}}})["parameterFilters"]
    live = {"ELEVATION": {"type": "numbers", "values": [0.0, 10.0], "threshold": 0.001, "default": "0"},
            "STYLES": {"type": "styles", "default": "polygon"}}  # GeoWebCache's own STYLES filter
    assert compared_filters(live, wanted) == wanted


def test_a_changed_cache_shows_only_what_changed():
    old, new = {"gridsets": ["a"], "parameterFilters": {"X": {"values": [1]}}}, {"gridsets": ["a"], "parameterFilters": {"X": {"values": [2]}}}
    assert diff([("cache", old, new)]) == [("cache.parameterFilters.X.values", [1], [2])]


def test_caching_defaults_change_only_the_fields_set():
    xml = ("<GeoServerGWCConfig><cacheLayersByDefault>true</cacheLayersByDefault><gutter>0</gutter>"
           "<defaultCachingGridSetIds><string>EPSG:4326</string></defaultCachingGridSetIds></GeoServerGWCConfig>")
    wanted = {"cacheLayersByDefault": False, "defaultCachingGridSetIds": ["EPSG:900913"], "WMTSEnabled": True}
    assert [k for k, _, _ in compare_gwc(xml, wanted)] == ["cacheLayersByDefault", "defaultCachingGridSetIds", "WMTSEnabled"]
    new = set_gwc(xml, wanted)
    assert compare_gwc(new, wanted) == [] and "<gutter>0</gutter>" in new


def test_an_integers_filter_keeps_a_whole_threshold():
    """GeoWebCache refuses "2.0" as an integers filter's threshold."""
    from configops.plan import wanted_filters

    wanted = wanted_filters({"BUFFER": {"type": "integers", "values": [0, 8], "threshold": 2}})
    assert wanted["BUFFER"]["threshold"] == 2 and isinstance(wanted["BUFFER"]["threshold"], int)
    assert "<threshold>2</threshold>" in filter_xml("BUFFER", wanted["BUFFER"])


def test_a_file_blob_stores_block_size_is_compared_when_the_files_set_it():
    from configops.tiles import live_blob_store

    live = live_blob_store('<FileBlobStore default="false"><id>disk</id><enabled>true</enabled><baseDirectory>/d</baseDirectory>'
                           "<fileSystemBlockSize>4096</fileSystemBlockSize></FileBlobStore>")
    assert live["blockSize"] == 4096
