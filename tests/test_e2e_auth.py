"""Authentication as code against the real GeoServer Cloud REST service: a password policy, a
request-header authentication filter, a filter chain that uses it, brute force settings; then
drift and import back to a clean plan. GeoServer's security files are put back at the end.

    E2E_GEOSERVER_URL=http://localhost:59090/rest pytest -m e2e
"""

import os

import pytest
import requests
import yaml

from configops.auth import SECURITY_FILE, live_security, reload_security
from configops.cli import main
from configops.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL:
    pytest.skip("E2E_GEOSERVER_URL not set", allow_module_level=True)

PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
FILES = ["pwpolicy/e2e-strict", "filter/e2e-header"]


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    saved = gs.request("GET", SECURITY_FILE).text
    yield gs
    gs.request("PUT", SECURITY_FILE, saved, content_type="application/xml")
    for f in FILES:
        gs.request("DELETE", f"resource/security/{f}", ok=(200, 404))
    reload_security(gs)


@pytest.fixture
def project(tmp_path, gs, monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": "localhost", "E2EDB_PORT": "55433",
                       "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": "postgres", "E2EDB_PASSWORD": "postgres"}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    chains = live_security(gs.request("GET", SECURITY_FILE).text)["filterChains"]
    chains = [c for c in chains if c["name"] != "e2e-about"]
    mine = {"name": "e2e-about", "class": "org.geoserver.security.ServiceLoginFilterChain", "paths": ["/rest/about/**"],
            "filters": ["e2e-header", "anonymous"], "interceptor": "restInterceptor", "exceptionTranslation": "exception"}
    rest = next(i for i, c in enumerate(chains) if c["name"] == "rest")
    chains.insert(rest, mine)
    docs = [
        {"kind": "Project", "metadata": {"name": "e2e-auth"}, "spec": {
            "geoserver": {"url": URL.removesuffix("/rest"), "credentials": {"secret": "e2egs"}},
            "database": {"jndi": "java:comp/env/jdbc/mypostgis", "connection": {"secret": "e2edb"}}, "workspace": "configops_auth"}},
        {"kind": "PasswordPolicy", "metadata": {"name": "e2e-strict"}, "spec": {"minLength": 12, "digit": True}},
        {"kind": "AuthFilter", "metadata": {"name": "e2e-header"}, "spec": {
            "class": "org.geoserver.security.config.RequestHeaderAuthenticationFilterConfig",
            "className": "org.geoserver.security.filter.GeoServerRequestHeaderAuthenticationFilter",
            "config": {"principalHeaderAttribute": "X-E2E-User", "userGroupServiceName": "default",
                       "roleSource": {"class": "org.geoserver.security.config.PreAuthenticatedUserNameFilterConfig$PreAuthenticatedUserNameRoleSource",
                                      "value": "UserGroupService"}}}},
        {"kind": "Settings", "metadata": {"name": "global"}, "spec": {"global": {"security": {
            "filterChains": chains, "bruteForcePrevention": {"maxDelaySeconds": 4}}}}},
    ]
    (tmp_path / "project.yaml").write_text(yaml.safe_dump_all([{"apiVersion": "configops/v1alpha1", **d} for d in docs], sort_keys=False))
    return tmp_path


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def test_apply_drift_and_import(gs, project, tmp_path_factory, capsys):
    assert configops(capsys, "validate", str(project))[0] == 0
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    code, out = configops(capsys, "plan", "-C", str(project))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
    about = f"{URL}/about/version.json"
    assert requests.get(about, headers={"X-E2E-User": "admin"}, timeout=30).status_code == 200  # let in by the new filter
    assert requests.get(about, timeout=30).status_code in (401, 403)

    policy = gs.request("GET", "resource/security/pwpolicy/e2e-strict/config.xml").text
    gs.request("PUT", "resource/security/pwpolicy/e2e-strict/config.xml", policy.replace("<minLength>12<", "<minLength>3<"),
               content_type="application/xml")
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2 and 'minLength: "3" -> 12' in out, out
    assert configops(capsys, "apply", "-C", str(project))[0] == 0

    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text((project / "project.yaml").read_text().split("\n---\n")[0] + "\n")
    code, out = configops(capsys, "import", "--settings", "--security", "-C", str(imported))
    assert code == 0, out
    text = (imported / "security.yaml").read_text() + (imported / "server.yaml").read_text()
    assert "kind: PasswordPolicy" in text and "X-E2E-User" in text and "e2e-about" in text
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
