"""What only standard GeoServer can set (configops/standard.py), without a GeoServer."""

from pathlib import Path

import pytest

from configops import standard
from configops.loader import load

PROJECT = """\
apiVersion: configops/v1alpha1
kind: Project
metadata: { name: p }
spec:
  geoserver: { url: http://gs:8080/geoserver, credentials: { secret: gs } }
  database: { jndi: java:comp/env/jdbc/x, connection: { secret: db } }
  workspace: uk
---
apiVersion: configops/v1alpha1
kind: Store
metadata: { name: warehouse }
spec: { type: oracle, connection: { host: oracle, database: /FREEPDB1 }, credentials: { secret: oracle } }
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: parcels }
spec: { store: warehouse, resource: PARCELS }
---
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata: { name: all }
spec:
  layers: [parcels]
  styles: [ { name: other, layers: [parcels] } ]
---
apiVersion: configops/v1alpha1
kind: Settings
metadata: { name: global }
spec: { global: { cors: { enabled: true }, contentSecurityPolicy: { frameAncestors: x }, diskQuota: { enabled: true } } }
"""


class Planner:
    def __init__(self, root: Path, cloud):
        self.files = load(root)
        self.gs = type("GS", (), {"cloud": cloud})()
        self.changes, self.unpublished = [], set()

    def add(self, action, kind, target, fields=(), note=None, run=None):
        self.changes.append((action, kind, target, note))


@pytest.fixture
def root(tmp_path):
    (tmp_path / "project.yaml").write_text(PROJECT)
    return tmp_path


def test_geoserver_cloud_leaves_them_out_with_the_reason(root):
    planner = Planner(root, cloud=True)
    standard.leave_out_on_cloud(planner)
    lines = [(a, k, t) for a, k, t, _ in planner.changes]
    assert lines == [("!", "store", "warehouse"), ("!", "layer", "parcels"), ("!", "group", "all"),
                     ("!", "settings", "global"), ("!", "settings", "global"), ("!", "settings", "global")]
    assert "GeoServer Cloud can't set styles: its catalog database" in planner.changes[2][3]
    assert planner.unpublished == {"parcels"} and "warehouse" not in planner.files.named("Store")
    assert "styles" not in planner.groups["all"].spec
    assert planner.files.named("Settings")["global"].spec == {"global": {}}


def test_standard_geoserver_keeps_them_and_isnt_asked_without_them(root):
    planner = Planner(root, cloud=False)
    standard.leave_out_on_cloud(planner)
    assert planner.changes == [] and "styles" in planner.files.named("LayerGroup")["all"].spec

    (root / "project.yaml").write_text(PROJECT.split("---")[0])
    planner = Planner(root, cloud=None)
    planner.gs = object()  # no `cloud`: asking would fail
    standard.leave_out_on_cloud(planner)
    assert planner.changes == []


def test_sizes():
    assert standard.size(5) == 5
    assert standard.size("2 GiB") == 2 * 1024 ** 3
    assert standard.size("1.5MiB") == 1572864
    assert standard.readable_size(2 * 1024 ** 3) == "2 GiB" and standard.readable_size(1000) == 1000
    with pytest.raises(ValueError):
        standard.size("2 GB")


def test_cors_from_the_settings_metadata():
    settings = {"metadata": {"map": {"entry": [
        {"string": "CogSettings.Key", "cogSettings": {}},
        {"string": "corsConfigurationSettings", standard.CORS_CLASS: {
            "enabled": True, "allowedOriginPatterns": "https://a.example.org,https://b.example.org",
            "allowedMethods": {"string": "GET"}, "allowedHeaders": {"string": ["A", "B"]}, "maxAge": 600}}]}}}
    assert standard.live_cors(settings) == {"enabled": True, "allowedOriginPatterns": ["https://a.example.org", "https://b.example.org"],
                                            "allowedMethods": ["GET"], "allowedHeaders": ["A", "B"], "maxAge": 600,
                                            "supportsCredentials": False}
    assert standard.live_cors({}) == standard.CORS_DEFAULTS


def test_csp_policies_replaced_and_read_back():
    xml = (Path(standard.__file__).parent / "csp_default.xml").read_text()
    live = standard.live_csp(xml)
    assert live["enabled"] is True and live["formAction"] == "'self'" and len(live["policies"]) == 1
    wanted = {"frameAncestors": "https://portal.example.org", "reportOnly": True,
              "policies": [{"name": "p", "rules": [{"name": "all", "filter": "PATH(^.*$)", "directives": "default-src 'self';"}]}]}
    again = standard.live_csp(standard.set_csp(xml, wanted))
    assert again["frameAncestors"] == "https://portal.example.org" and again["reportOnly"] is True
    assert again["policies"] == standard.csp_policies(wanted["policies"])
    assert again["policies"][0]["rules"][0] == {"name": "all", "description": "", "enabled": True, "filter": "PATH(^.*$)",
                                                "directives": "default-src 'self';"}


def test_disk_quota_written_as_geowebcache_reads_it():
    rest = """<org.geowebcache.diskquota.DiskQuotaConfig><enabled>true</enabled><cacheCleanUpFrequency>5</cacheCleanUpFrequency>
      <cacheCleanUpUnits>MINUTES</cacheCleanUpUnits><maxConcurrentCleanUps>3</maxConcurrentCleanUps>
      <globalExpirationPolicyName>LRU</globalExpirationPolicyName><globalQuota><id>0</id><bytes>2147483648</bytes></globalQuota>
      <layerQuotas><org.geowebcache.diskquota.storage.LayerQuota><layer>uk:roads</layer><expirationPolicyName>LFU</expirationPolicyName>
      <quota><id>0</id><bytes>104857600</bytes></quota></org.geowebcache.diskquota.storage.LayerQuota></layerQuotas>
      </org.geowebcache.diskquota.DiskQuotaConfig>"""
    live = standard.live_quota(rest, "<gwcQuotaConfiguration><quotaStore>HSQL</quotaStore></gwcQuotaConfiguration>")
    wanted = standard.wanted_quota({"enabled": True, "quotaStore": "HSQL", "globalQuota": "2 GiB", "globalExpirationPolicyName": "LRU",
                                    "cacheCleanUpFrequency": 5, "cacheCleanUpUnits": "MINUTES", "maxConcurrentCleanUps": 3,
                                    "layerQuotas": [{"layer": "uk:roads", "quota": "100 MiB"}]})
    assert {k: live[k] for k in wanted} == wanted
    written = standard.quota_xml(live, with_store=True)
    assert "<globalQuota><value>2147483648</value><units>B</units></globalQuota>" in written
    assert "<quotaStore>HSQL</quotaStore>" in written and "<layer>uk:roads</layer>" in written
    assert "<quotaStore>" not in standard.quota_xml(live, with_store=False)
    assert standard.live_quota(written, None)["layerQuotas"] == live["layerQuotas"]


SETTINGS_3_0 = """\
apiVersion: configops/v1alpha1
kind: Project
metadata: { name: p }
spec:
  geoserver: { url: http://gs:8080/geoserver, credentials: { secret: gs } }
  workspace: uk
---
apiVersion: configops/v1alpha1
kind: Settings
metadata: { name: global }
spec:
  contact: { contactPerson: Map Team, internationalTitle: { cy: Teitl } }
  global: { cors: { enabled: true }, tileCaching: { seederCorePoolSize: 8, seederMaxPoolSize: 32, gutter: 10 } }
"""


@pytest.mark.parametrize("version, left_out", [("2.28.6", 4), ("3.0.2", 0), ("", 0)])
def test_what_came_in_geoserver_3_is_left_out_on_2_28(tmp_path, version, left_out):
    (tmp_path / "project.yaml").write_text(SETTINGS_3_0)
    planner = Planner(tmp_path, cloud=False)
    numbers = [int(n) for n in version.split(".")[:2]] if version else None
    planner.gs = type("GS", (), {"cloud": False, "version": version, "series": tuple(numbers) if numbers else None})()
    standard.leave_out_unsupported(planner)
    assert len(planner.changes) == left_out
    spec = planner.files.named("Settings")["global"].spec
    if left_out:
        assert "GeoServer 2.28.6 can't set global.cors: it came in GeoServer 3.0" in [c[3] for c in planner.changes]
        assert spec == {"contact": {"contactPerson": "Map Team"}, "global": {"tileCaching": {"gutter": 10}}}
    else:
        assert spec["global"]["cors"] == {"enabled": True}
