"""Settings and Service resources (configops/server.py) against a fake GeoServer."""

import copy

from configops import ingest, server
from configops.loader import load
from configops.plan import Planner, ordered

PROJECT = """\
apiVersion: configops/v1alpha1
kind: Project
metadata: { name: t }
spec:
  geoserver: { url: http://gs:8080/geoserver-cloud, credentials: { secret: gs } }
  database: { jndi: java:comp/env/jdbc/x, connection: { secret: pg } }
  workspace: uk
"""

SERVER = """\
apiVersion: configops/v1alpha1
kind: Settings
metadata: { name: global }
spec:
  contact: { contactPerson: Map Team }
  numDecimals: 6
  global: { webUIMode: REDIRECT }
---
apiVersion: configops/v1alpha1
kind: Settings
metadata: { name: uk }
spec: { workspace: uk, contact: { contactPerson: UK Team } }
---
apiVersion: configops/v1alpha1
kind: Service
metadata: { name: wms }
spec:
  title: Maps
  abstract: All of them
  keywords: [maps]
  srs: [EPSG:27700, EPSG:4326]
  watermark: { position: BOT_LEFT }
  geoserver: { rootLayerTitle: Everything }
"""


class FakeGeoServer:
    """The settings objects as GeoServer's REST API shows them; PUT replaces, as GeoServer does."""

    def __init__(self):
        self.objects = {
            "settings": {"global": {"settings": {"charset": "UTF-8", "numDecimals": 4, "contact": {"id": "contact"}},
                                    "webUIMode": "DEFAULT", "jai": {"tileThreads": 7}, "updateSequence": 9}},
            "settings/contact": {"contact": {"id": "contact"}},
            "workspaces/uk/settings": {"settings": {"charset": "UTF-8", "contact": {"id": "contact"}}},  # defaults, not its own
            "services/wms/settings": {"wms": {"enabled": True, "title": "", "keywords": {"string": "old"},
                                              "watermark": {"enabled": False, "position": "BOT_RIGHT", "transparency": 100}}},
        }
        self.puts = []

    def get(self, path):
        return copy.deepcopy(self.objects.get(path))

    def request(self, method, path, body=None, *args, **kwargs):
        assert method == "PUT"
        self.puts.append(path)
        self.objects[path] = copy.deepcopy(body)
        if path == "settings/contact":
            self.objects["settings"]["global"]["settings"]["contact"] = copy.deepcopy(body["contact"])

    def workspace(self, ws):
        return {"workspace": {"name": ws}}

    def read(self, path):  # no style images, fonts or templates
        return None

    def files(self, folder):
        return []

    def names(self, path, key):
        return []


def plan(tmp_path, gs, extra=SERVER):
    (tmp_path / "project.yaml").write_text(PROJECT + "---\n" + extra)
    return ordered(Planner(load(tmp_path), tmp_path, gs, None, ingest.Database("h", 5432, "d", "u", "p")).plan())


def test_only_the_fields_the_files_set_are_compared(tmp_path):
    changes = {(c.action, c.kind, c.target): c for c in plan(tmp_path, FakeGeoServer())}
    assert set(changes) == {("~", "settings", "global"), ("+", "settings", "uk (workspace)"), ("~", "service", "wms")}
    assert changes[("~", "settings", "global")].fields == [
        ("contact.contactPerson", None, "Map Team"), ("numDecimals", 4, 6), ("global.webUIMode", "DEFAULT", "REDIRECT")]
    wms = dict((name, (live, wanted)) for name, live, wanted in changes[("~", "service", "wms")].fields)
    assert wms["abstract"] == (None, "All of them")  # shown by the spec's name, not GeoServer's abstrct
    assert wms["keywords"] == (["old"], ["maps"])  # one item comes back as a plain string
    assert wms["watermark.position"] == ("BOT_RIGHT", "BOT_LEFT")
    assert wms["rootLayerTitle"] == (None, "Everything")  # passed through from geoserver:


def test_apply_merges_into_what_is_there_then_nothing_to_do(tmp_path):
    gs = FakeGeoServer()
    for c in plan(tmp_path, gs):
        c.run()
    assert plan(tmp_path, gs) == []
    g = gs.objects["settings"]["global"]
    assert g["jai"] == {"tileThreads": 7} and g["settings"]["charset"] == "UTF-8"  # fields the files don't set survive
    assert "updateSequence" not in g
    assert gs.objects["settings/contact"]["contact"]["contactPerson"] == "Map Team"
    ws = gs.objects["workspaces/uk/settings"]["settings"]
    assert ws["workspace"] == {"name": "uk"} and ws["contact"]["contactPerson"] == "UK Team"
    wms = gs.objects["services/wms/settings"]["wms"]
    assert wms["abstrct"] == "All of them" and wms["srs"] == {"string": ["EPSG:27700", "EPSG:4326"]}
    assert wms["watermark"] == {"enabled": False, "position": "BOT_LEFT", "transparency": 100}


def test_no_settings_in_the_files_means_none_touched(tmp_path):
    gs = FakeGeoServer()
    assert plan(tmp_path, gs, extra="apiVersion: configops/v1alpha1\nkind: Source\nmetadata: { name: web }\nspec: { type: url }\n") == []


def test_compare_and_merge():
    assert server.compare({"a": {"b": 1, "c": 2}}, {"a": {"b": 3}}) == [("a.b", 1, 3)]
    assert server.compare({}, {"srs": {"string": []}}) == []  # GeoServer drops an empty list
    assert server.merge({"a": {"b": 1, "c": 2}, "l": {"string": ["x"]}}, {"a": {"b": 3}, "l": {"string": ["y"]}}) == \
        {"a": {"b": 3, "c": 2}, "l": {"string": ["y"]}}
    assert server.to_geoserver({"abstract": "x", "keywords": ["k"], "geoserver": {"other": 1}}) == \
        {"abstrct": "x", "keywords": {"string": ["k"]}, "other": 1}


def test_request_logging_is_written_as_metadata_text():
    assert server.logging_entries({"enabled": True, "headers": False, "bodySize": 2048}) == {
        "logRequestsEnabled": "true", "logHeadersEnabled": "false", "requestLogBufferSize": "2048"}


def test_a_single_metadata_entry_is_read_like_a_list():
    one = {"metadata": {"entry": {"@key": "logRequestsEnabled", "$": "true"}}}
    many = {"metadata": {"entry": [{"@key": "logRequestsEnabled", "$": "true"}, {"@key": "requestLogBufferSize", "$": "64"}]}}
    assert server.metadata(one) == {"logRequestsEnabled": "true"}
    assert server.metadata(many) == {"logRequestsEnabled": "true", "requestLogBufferSize": "64"}
    assert server.metadata({}) == {}
    assert (server.typed("true"), server.typed("64")) == (True, 64)
