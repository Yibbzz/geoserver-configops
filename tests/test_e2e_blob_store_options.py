"""Blob store options that need no storage behind them: a folder laid out as TMS, S3 and Azure through
a proxy, a Google Cloud quota project. The stores are switched off, so GeoWebCache never connects:
apply, then plan has nothing to do, GeoWebCache holds each value, and import writes them back.
"""

import os

import pytest
import yaml

from configops.cli import main
from configops.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL:
    pytest.skip("E2E_GEOSERVER_URL not set", allow_module_level=True)

WS = "configops_blob_options"
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=os.environ.get("E2E_DB_PORT", "55433"),
          password=os.environ.get("E2E_DB_PASSWORD", "postgres"))
STORES = ("e2e-tms-off", "e2e-s3-proxy", "e2e-azure-proxy", "e2e-gcs-quota")


@pytest.fixture
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD, gwc_url=os.environ.get("E2E_GWC_URL"))
    yield gs
    for store in STORES:
        gs.gwc_request("DELETE", f"blobstores/{store}")
    gs.delete_workspace(WS)


@pytest.fixture
def project(tmp_path, monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"], "E2EDB_PORT": DB["port"],
                       "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": "postgres", "E2EDB_PASSWORD": DB["password"],
                       "E2ES3_ACCESSKEY": "e2e-access", "E2ES3_SECRETKEY": "e2e-secret", "E2EPROXY_USERNAME": "e2e-proxy-user",
                       "E2EPROXY_PASSWORD": "e2e-proxy-password", "AZURE_ACCOUNTNAME": "e2e", "AZURE_ACCOUNTKEY": "ZTJlLWtleQ=="}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    if os.environ.get("E2E_GWC_URL"):
        monkeypatch.setenv("CONFIGOPS_GWC_URL", os.environ["E2E_GWC_URL"])
    head = {"kind": "Project", "metadata": {"name": "e2e-blob-options"}, "spec": {
        "geoserver": {"url": URL.removesuffix("/rest"), "credentials": {"secret": "e2egs"}},
        "database": {"jndi": "java:comp/env/jdbc/mypostgis", "connection": {"secret": "e2edb"}}, "workspace": WS}}
    proxy = {"host": "proxy.example.org", "port": 3128}
    docs = [head,
            {"kind": "BlobStore", "metadata": {"name": "e2e-tms-off"}, "spec": {"type": "file", "enabled": False, "path": "e2e-tms", "layout": "tms"}},
            {"kind": "BlobStore", "metadata": {"name": "e2e-s3-proxy"}, "spec": {
                "type": "s3", "enabled": False, "bucket": "e2e", "credentials": {"secret": "e2es3"},
                "proxy": {**proxy, "domain": "EXAMPLE", "workstation": "e2e-ws", "credentials": {"secret": "e2eproxy"}}}},
            {"kind": "BlobStore", "metadata": {"name": "e2e-azure-proxy"}, "spec": {
                "type": "azure", "enabled": False, "container": "e2e", "credentials": {"secret": "azure"},
                "proxy": {**proxy, "credentials": {"secret": "e2eproxy"}}}},
            {"kind": "BlobStore", "metadata": {"name": "e2e-gcs-quota"}, "spec": {
                "type": "gcs", "enabled": False, "bucket": "e2e", "project": "e2e", "quotaProject": "e2e-billing"}}]
    (tmp_path / "project.yaml").write_text(yaml.safe_dump_all([{"apiVersion": "configops/v1alpha1", **d} for d in docs]))
    return tmp_path


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def test_blob_store_options_are_kept(gs, project, capsys):
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    code, out = configops(capsys, "plan", "-C", str(project))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
    xml = {s: gs.gwc_request("GET", f"blobstores/{s}", accept="application/xml").text for s in STORES}
    assert "<pathGeneratorType>TMS</pathGeneratorType>" in xml["e2e-tms-off"]
    for part in ("<proxyHost>proxy.example.org</proxyHost>", "<proxyPort>3128</proxyPort>", "<proxyDomain>EXAMPLE</proxyDomain>",
                 "<proxyWorkstation>e2e-ws</proxyWorkstation>", "<proxyUsername>e2e-proxy-user</proxyUsername>",
                 "<proxyPassword>e2e-proxy-password</proxyPassword>"):
        assert part in xml["e2e-s3-proxy"], (part, xml["e2e-s3-proxy"])
    assert "<proxyHost>proxy.example.org</proxyHost>" in xml["e2e-azure-proxy"] and "<proxyPassword>" in xml["e2e-azure-proxy"]
    assert "<quotaProjectId>e2e-billing</quotaProjectId>" in xml["e2e-gcs-quota"]
