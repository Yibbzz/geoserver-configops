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


LDAP = ("<org.geoserver.security.ldap.LDAPSecurityServiceConfig><name>ldap</name><serverURL>ldap://old</serverURL>"
        "<password>{password}</password></org.geoserver.security.ldap.LDAPSecurityServiceConfig>")


class Files:
    def __init__(self, config):
        self.config = config

    def of_kind(self, kind):
        from pathlib import Path

        from configops.loader import Resource

        data = {"kind": "AuthProvider", "metadata": {"name": "ldap"}, "spec": {"config": self.config}}
        return [Resource(data, Path("security.yaml"), None)] if kind == "AuthProvider" else []


class Planned:
    def __init__(self, config, live_password, root):
        from configops.plan import Change

        outer = self

        class Response:
            status_code, text = 200, LDAP.format(password=live_password)

        class GS:
            rest = "http://gs/rest"

            def request(self, method, path, body=None, **kwargs):
                if method == "PUT":
                    outer.written = body
                return Response()

        self.gs, self.files, self.root, self.changes, self.written = GS(), Files(config), root, [], None
        self.add = lambda *a, **k: self.changes.append(Change(*a, **k))


def test_a_password_in_a_security_object_comes_from_a_secret_and_is_never_printed(tmp_path, monkeypatch, capsys):
    from configops import auth
    from configops.plan import print_plan

    monkeypatch.setenv("CONFIGOPS_LDAP_BIND_PASSWORD", "s3cret-bind")
    config = {"serverURL": "ldap://new", "password": {"secret": "ldap-bind", "key": "password"}}
    planned = Planned(config, "crypt1:LIVEVALUE", tmp_path)
    auth.plan(planned)
    assert planned.changes[0].fields == [("serverURL", "ldap://old", "ldap://new"), ("password", "(differs)", "(from the Secret)")]
    print_plan(planned.changes)
    out = capsys.readouterr().out
    assert "s3cret" not in out and "LIVEVALUE" not in out

    same = Planned({"password": {"secret": "ldap-bind", "key": "password"}}, "s3cret-bind", tmp_path)
    auth.plan(same)
    assert same.changes == []


def test_a_password_written_in_the_files_is_not_printed_either(tmp_path, capsys):
    from configops import auth
    from configops.plan import print_plan

    planned = Planned({"password": "in-the-file", "passwordEncoderName": "plain"}, "crypt1:LIVEVALUE", tmp_path)
    auth.plan(planned)
    assert planned.changes[0].fields == [("password", "(differs)", "(hidden)"), ("passwordEncoderName", None, "plain")]


def test_a_secret_that_isnt_there_stops_the_plan(tmp_path):
    from configops import auth
    from configops.geoserver import MissingCredentials

    with pytest.raises(MissingCredentials, match="AuthProvider ldap: no value for config.password in secret ldap-bind, key password"):
        auth.plan(Planned({"password": {"secret": "ldap-bind", "key": "password"}}, "x", tmp_path))


def test_the_remember_me_key_is_not_printed():
    config = CONFIG.replace("</security>", "<rememberMeService><className>c</className><key>live-key</key></rememberMeService></security>")
    assert compare_security(config, {"rememberMe": {"key": "new-key"}}) == [("rememberMe", {"className": "c", "key": "(differs)"},
                                                                             {"key": "(hidden)"})]
