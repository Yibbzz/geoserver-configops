"""Service options seen in GeoServer's own services: a disabled WMS version is refused, the root
layer's authority URL and identifier show in capabilities, and the OGC API Features conformance
classes follow the WFS settings.

Needs the whole chart, reached through its gateway (the kind test, or the dev cluster).
"""

import os
import time

import pytest
import requests
import yaml

from configops.cli import main
from configops.geoserver import GeoServer
from tests.test_e2e_settings import restore

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
SERVICES = os.environ.get("E2E_SERVICES_URL")
if not URL or not SERVICES:
    pytest.skip("E2E_GEOSERVER_URL or E2E_SERVICES_URL not set", allow_module_level=True)

PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=os.environ.get("E2E_DB_PORT", "55433"),
          password=os.environ.get("E2E_DB_PASSWORD", "postgres"))
SEARCH = "http://www.opengis.net/spec/ogcapi-features-5/1.0/req/search"


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    saved = {s: gs.get(f"services/{s}/settings") for s in ("wms", "wfs")}
    yield gs
    for s, body in saved.items():
        before = body[s]
        gs.request("PUT", f"services/{s}/settings", {s: restore(gs.get(f"services/{s}/settings")[s], before)})


@pytest.fixture
def project(tmp_path, monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"], "E2EDB_PORT": DB["port"],
                       "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": "postgres", "E2EDB_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)

    def write(*docs) -> str:
        head = {"kind": "Project", "metadata": {"name": "e2e-service-options"}, "spec": {
            "geoserver": {"url": URL.removesuffix("/rest"), "credentials": {"secret": "e2egs"}},
            "database": {"jndi": "java:comp/env/jdbc/mypostgis", "connection": {"secret": "e2edb"}}, "workspace": "configops_options"}}
        (tmp_path / "project.yaml").write_text(yaml.safe_dump_all([{"apiVersion": "configops/v1alpha1", **d} for d in (head, *docs)]))
        return str(tmp_path)
    return write


def apply(root: str, capsys) -> None:
    code = main(["apply", "-C", root])
    out = capsys.readouterr()
    assert code == 0, out.out + out.err
    assert main(["plan", "-C", root]) == 0
    capsys.readouterr()


def eventually(check, seconds: int = 90) -> None:
    """Other services follow REST's changes within about 20 s."""
    deadline = time.time() + seconds
    while not check():
        assert time.time() < deadline, "GeoServer's services didn't follow the change"
        time.sleep(5)


def capabilities(version: str) -> str:
    return requests.get(f"{SERVICES}/wms", params={"service": "WMS", "request": "GetCapabilities", "version": version}, timeout=60).text


def conformance() -> list[str]:
    r = requests.get(f"{SERVICES}/ogc/features/v1/conformance", params={"f": "json"}, timeout=60)
    return r.json()["conformsTo"] if r.ok else []


def service(name: str, **spec) -> dict:
    return {"kind": "Service", "metadata": {"name": name}, "spec": spec}


def test_wms_versions_authorities_and_ogc_api_conformance(gs, project, capsys):
    root = project(
        service("wms", disabledVersions=["1.1.1"], authorityURLs=[{"name": "e2e", "href": "https://example.org/authority"}],
                identifiers=[{"authority": "e2e", "identifier": "e2e-root-layer"}]),
        service("wfs", metadata={"ogcapiFeatures": {"search": False}}))
    apply(root, capsys)
    eventually(lambda: "<WMT_MS_Capabilities" not in capabilities("1.1.1") and SEARCH not in conformance())
    caps = capabilities("1.3.0")
    assert 'name="e2e"' in caps and "https://example.org/authority" in caps and "e2e-root-layer" in caps, caps[:2000]

    root = project(service("wms", disabledVersions=[]), service("wfs", metadata={"ogcapiFeatures": {"search": True}}))
    apply(root, capsys)
    eventually(lambda: "<WMT_MS_Capabilities" in capabilities("1.1.1") and SEARCH in conformance())
