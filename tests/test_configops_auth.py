"""Security files (configops/auth.py) without a GeoServer: reading, comparing and writing them."""

import types
import xml.etree.ElementTree as ET

import pytest

from configops.auth import compare_security, live_security, reload_security, same, set_security, set_value, to_value
from configops.geoserver import GeoServerError

CONFIG = """<security>
  <roleServiceName>default</roleServiceName>
  <authProviderNames><string>default</string></authProviderNames>
  <encryptingUrlParams>false</encryptingUrlParams>
  <filterChain>
    <filters name="rest" class="org.geoserver.security.ServiceLoginFilterChain" interceptorName="restInterceptor" path="/rest.*,/rest/**" disabled="false">
      <filter>basic</filter>
      <filter>anonymous</filter>
    </filters>
  </filterChain>
  <bruteForcePrevention><enabled>true</enabled><maxDelaySeconds>5</maxDelaySeconds></bruteForcePrevention>
</security>"""


def test_security_settings_round_trip():
    live = live_security(CONFIG)
    assert live["filterChains"] == [{"name": "rest", "paths": ["/rest.*", "/rest/**"], "filters": ["basic", "anonymous"],
                                     "class": "org.geoserver.security.ServiceLoginFilterChain", "interceptor": "restInterceptor",
                                     "disabled": False}]
    wanted = {"authProviders": ["ldap", "default"], "bruteForcePrevention": {"maxDelaySeconds": 9},
              "filterChains": [{"name": "wms", "paths": ["/wms/**"], "filters": ["basic"]},
                               {"name": "rest", "paths": ["/rest/**"], "filters": ["basic"]}]}
    assert [k for k, _, _ in compare_security(CONFIG, wanted)] == ["authProviders", "bruteForcePrevention", "filterChains",
                                                                         "filterChains.rest"]
    new = set_security(CONFIG, wanted)
    assert compare_security(new, wanted) == []
    chains = live_security(new)["filterChains"]
    assert chains[1]["interceptor"] == "restInterceptor"  # what the file leaves out stays
    assert "<enabled>true</enabled>" in new


def test_fields_with_a_class_and_lists():
    root = ET.fromstring("<x/>")
    set_value(root, "roleSource", {"class": "a.B$C", "value": "UserGroupService"})
    set_value(root, "masks", ["127.0.0.1"])
    live = {c.tag: to_value(c) for c in root}
    assert live == {"roleSource": {"class": "a.B$C", "value": "UserGroupService"}, "masks": ["127.0.0.1"]}
    assert same(live["roleSource"], {"class": "a.B$C", "value": "UserGroupService"}) and same("5", 5) and same("true", True)


class Reloading:
    """GeoServer answering 500 to a save while it reloads its security by itself, `busy` times."""

    def __init__(self, busy, status=500):
        self.rest, self.busy, self.status, self.puts = "http://gs/rest", busy, status, 0
        self.session = types.SimpleNamespace(get=lambda url, **kw: types.SimpleNamespace(text="<provider/>"))

    def request(self, method, path, body=None, **kw):
        self.puts += 1
        if self.puts <= self.busy:
            raise GeoServerError(method, path, self.status, "Cannot save security configuration")


def test_reload_security_waits_while_geoserver_is_reloading():
    gs = Reloading(busy=2)
    reload_security(gs, every=0)
    assert gs.puts == 3


def test_reload_security_gives_up_and_other_errors_are_not_retried():
    with pytest.raises(GeoServerError):
        reload_security(Reloading(busy=99), seconds=0, every=0)
    gs = Reloading(busy=1, status=403)
    with pytest.raises(GeoServerError):
        reload_security(gs, every=0)
    assert gs.puts == 1
