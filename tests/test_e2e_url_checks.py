"""URL checks and request logging, seen in GeoServer's services: WMS refuses a remote style URL no
URLCheck allows and fetches one that is allowed; the global switch turns the checks off; with
request logging on, WMS writes each request (and its headers) to its log.

Needs the whole chart, reached through its gateway (the kind test, or the dev cluster), and
E2E_DATA_POD (<namespace>/<pod>) for the namespace whose WMS logs to read.
"""

import os
import subprocess
import time
import uuid

import pytest
import requests
import yaml

from configops.cli import main
from configops.geoserver import GeoServer
from configops.server import URL_CHECKS_FILE

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
SERVICES = os.environ.get("E2E_SERVICES_URL")
if not URL or not SERVICES:
    pytest.skip("E2E_GEOSERVER_URL or E2E_SERVICES_URL not set", allow_module_level=True)

PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=os.environ.get("E2E_DB_PORT", "55433"),
          password=os.environ.get("E2E_DB_PASSWORD", "postgres"))
ALLOWED = "http://configops-allowed.invalid/style.sld"
OTHER = "http://configops-other.invalid/style.sld"
REFUSED = "not accepted by external URL checks"


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    before = gs.get("settings")["global"]
    checks = gs.request("GET", URL_CHECKS_FILE, ok=(200, 404))
    yield gs
    for name in ("e2e-allowed", "e2e-unmanaged"):
        gs.request("DELETE", f"urlchecks/{name}", ok=(200, 404, 500))
    if checks.status_code == 200:
        gs.request("PUT", URL_CHECKS_FILE, checks.text, content_type="application/xml")
    else:  # GeoServer keeps the last switch it read, even once the file is gone: write the default
        gs.request("PUT", URL_CHECKS_FILE, "<checks><enabled>true</enabled></checks>", content_type="application/xml")
    current = gs.get("settings")["global"]
    current.pop("updateSequence", None)
    current["metadata"] = before.get("metadata") or {}  # a PUT without metadata keeps it
    gs.request("PUT", "settings", {"global": current})
    gs.delete_workspace("configops_url_checks")  # apply makes the project's workspace


def project(tmp_path, monkeypatch, *docs) -> str:
    head = {"kind": "Project", "metadata": {"name": "e2e-url-checks"}, "spec": {
        "geoserver": {"url": URL.removesuffix("/rest"), "credentials": {"secret": "e2egs"}},
        "database": {"jndi": "java:comp/env/jdbc/mypostgis", "connection": {"secret": "e2edb"}}, "workspace": "configops_url_checks"}}
    (tmp_path / "project.yaml").write_text(yaml.safe_dump_all([{"apiVersion": "configops/v1alpha1", **d} for d in (head, *docs)]))
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"], "E2EDB_PORT": DB["port"],
                       "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": "postgres", "E2EDB_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    return str(tmp_path)


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def settings(**fields) -> dict:
    return {"kind": "Settings", "metadata": {"name": "global"}, "spec": {"global": fields}}


def remote_style(url: str) -> str:
    """WMS's answer to a GetMap whose style is at `url` (no layer needed: the style is fetched first)."""
    r = requests.get(f"{SERVICES}/wms", params={
        "service": "WMS", "version": "1.1.1", "request": "GetMap", "bbox": "-10,49,2,61", "srs": "EPSG:4326",
        "width": 10, "height": 10, "format": "image/png", "sld": url}, timeout=60)
    return r.text


def eventually(check, seconds: int = 90) -> None:
    """Other services follow REST's changes within about 20 s."""
    deadline = time.time() + seconds
    while not check():
        assert time.time() < deadline, "GeoServer's services didn't follow the change"
        time.sleep(5)


def test_url_checks_allow_only_what_they_name(gs, tmp_path, monkeypatch, capsys):
    check = {"kind": "URLCheck", "metadata": {"name": "e2e-allowed"}, "spec": {
        "description": "e2e test styles", "regex": r"^http://configops-allowed\.invalid/.*$"}}
    root = project(tmp_path, monkeypatch, check, settings(urlChecks=True))
    gs.request("POST", "urlchecks", {"regexUrlCheck": {"name": "e2e-unmanaged", "regex": "^x$", "enabled": True}})

    code, out = configops(capsys, "apply", "-C", root)
    assert code == 0, out
    code, out = configops(capsys, "plan", "-C", root)
    assert code == 0 and "? url check e2e-unmanaged" in out, out
    live = gs.get("urlchecks/e2e-allowed")["regexUrlCheck"]
    assert (live["description"], live["enabled"]) == ("e2e test styles", True)
    # allowed: WMS goes on to fetch it (and fails: the host doesn't exist); anything else is refused
    eventually(lambda: REFUSED not in remote_style(ALLOWED) and "Error while getting SLD" in remote_style(ALLOWED))
    assert REFUSED in remote_style(OTHER)

    check["spec"].update(enabled=False, description="off")
    project(tmp_path, monkeypatch, check, settings(urlChecks=True))
    code, out = configops(capsys, "plan", "-C", root)
    assert code == 2 and "~ url check e2e-allowed" in out, out
    assert configops(capsys, "apply", "-C", root)[0] == 0
    eventually(lambda: REFUSED in remote_style(ALLOWED))

    code, out = configops(capsys, "apply", "--prune", "-C", root)
    assert code == 0 and "- url check e2e-unmanaged" in out, out
    assert gs.request("GET", "urlchecks/e2e-unmanaged", ok=(200, 404, 500)).status_code != 200


def test_url_checks_can_be_switched_off(gs, tmp_path, monkeypatch, capsys):
    root = project(tmp_path, monkeypatch, settings(urlChecks=False))
    code, out = configops(capsys, "apply", "-C", root)
    assert code == 0 and "global.urlChecks" in out, out
    assert configops(capsys, "plan", "-C", root)[0] == 0
    eventually(lambda: REFUSED not in remote_style(OTHER))

    imported = tmp_path.parent / f"{tmp_path.name}-imported"  # beside the project: inside, it would be part of it
    imported.mkdir()
    (imported / "project.yaml").write_text((tmp_path / "project.yaml").read_text().split("\n---\n")[0] + "\n")
    code, out = configops(capsys, "import", "--settings", "-C", str(imported))
    assert code == 0, out
    assert imported_global(imported)["urlChecks"] is False

    project(tmp_path, monkeypatch, settings(urlChecks=True))
    assert configops(capsys, "apply", "-C", root)[0] == 0
    eventually(lambda: REFUSED in remote_style(OTHER))


def imported_global(folder) -> dict:
    """The global settings `import --settings` wrote."""
    docs = yaml.safe_load_all((folder / "server.yaml").read_text())
    return next(d for d in docs if d["kind"] == "Settings" and "workspace" not in d["spec"])["spec"]["global"]


def wms_log(since: str = "3m") -> str:
    pod, container = os.environ.get("E2E_DATA_POD"), os.environ.get("E2E_REST_CONTAINER")
    if not pod and container:  # one GeoServer in Docker: its log is the container's
        run = subprocess.run(["docker", "logs", f"--since={since}", container], capture_output=True, text=True, check=True)
        return run.stdout + run.stderr
    if not pod:
        pytest.skip("E2E_DATA_POD or E2E_REST_CONTAINER not set: no WMS log to read")
    namespace = pod.split("/")[0]
    return subprocess.run(["kubectl", "-n", namespace, "logs", "-l", "app.kubernetes.io/component=wms", f"--since={since}",
                           "--tail=-1"], capture_output=True, text=True, check=True).stdout


def test_request_logging_writes_requests_to_the_log(gs, tmp_path, monkeypatch, capsys):
    root = project(tmp_path, monkeypatch, settings(requestLogging={"enabled": True, "headers": True, "bodies": True, "bodySize": 2048}))
    code, out = configops(capsys, "apply", "-C", root)
    assert code == 0, out
    assert configops(capsys, "plan", "-C", root)[0] == 0
    entries = {e["@key"]: e["$"] for e in gs.get("settings")["global"]["metadata"]["entry"]}
    assert [entries[k] for k in ("logRequestsEnabled", "logHeadersEnabled", "logBodiesEnabled", "requestLogBufferSize")] == [
        "true", "true", "true", "2048"]

    marker = uuid.uuid4().hex

    def logged() -> bool:
        requests.post(f"{SERVICES}/wms", params={"e2e": marker}, headers={"X-Configops-E2e": marker, "Content-Type": "application/xml"},
                      data=f'<GetCapabilities service="WMS" version="1.3.0"><!-- body-{marker} --></GetCapabilities>', timeout=60)
        log = wms_log()
        return all(text in log for text in (f"e2e={marker}", f"X-Configops-E2e: {marker}", f"body-{marker}"))
    eventually(logged)

    imported = tmp_path.parent / f"{tmp_path.name}-imported"  # beside the project: inside, it would be part of it
    imported.mkdir()
    (imported / "project.yaml").write_text((tmp_path / "project.yaml").read_text().split("\n---\n")[0] + "\n")
    assert configops(capsys, "import", "--settings", "-C", str(imported))[0] == 0
    assert imported_global(imported)["requestLogging"] == {
        "enabled": True, "headers": True, "bodies": True, "bodySize": 2048}

    root = project(tmp_path, monkeypatch, settings(requestLogging={"enabled": False, "headers": False}))
    assert configops(capsys, "apply", "-C", root)[0] == 0
    time.sleep(25)
    marker = uuid.uuid4().hex
    requests.get(f"{SERVICES}/wms", params={"service": "WMS", "request": "GetCapabilities", "e2e": marker}, timeout=60)
    time.sleep(2)
    assert f"e2e={marker}" not in wms_log("1m")
