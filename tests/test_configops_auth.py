"""Security files (configops/auth.py) without a GeoServer: reading, comparing and writing them."""

import xml.etree.ElementTree as ET

from configops.auth import compare_security, live_security, same, set_security, set_value, to_value

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
    assert [k for k, _, _ in compare_security(CONFIG, wanted)] == ["authProviders", "bruteForcePrevention", "filterChains"]
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
