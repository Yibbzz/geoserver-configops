"""Settings and Service resources against the real GeoServer Cloud REST service.

Every field the schema allows is set, so a field GeoServer ignores or renames
shows up as a plan that never settles. Start the service with tests/e2e/up.sh, then:

    E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_DB_PORT=55433 pytest -m e2e

The settings are put back as they were at the end.
"""

import os

import pytest
import yaml

from configops.cli import main
from configops.geoserver import GeoServer
from configops.validate import schema

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL:
    pytest.skip("E2E_GEOSERVER_URL not set", allow_module_level=True)

WS = "configops_settings"
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=os.environ.get("E2E_DB_PORT", "55433"))
SAVED = ["settings", "settings/contact", "services/wms/settings", "services/wfs/settings",
         "services/wcs/settings", "services/wmts/settings", "security/acl/catalog"]

# A valid value for each field, where "e2e <field>" or 7 wouldn't do
VALUES = {
    "charset": "UTF-8", "schemaBaseURL": "https://schemas.example.org", "onlineResource": "https://maps.example.org",
    "proxyBaseUrl": "https://maps.example.org/geoserver-cloud", "srs": ["EPSG:27700", "EPSG:4326"],
    "keywords": ["e2e", "maps"], "enabled": True, "memoryCapacity": 0.5, "memoryThreshold": 0.75, "defaultDeflateCompressionLevel": 7, "url": "https://example.org/logo.png",
}


def restore(live: dict, saved: dict) -> dict:
    """`saved`, with the text and lists that were empty then blanked: GeoServer keeps what a PUT leaves out."""
    out = dict(saved)
    for key, value in live.items():
        if saved.get(key) not in (None, ""):
            if isinstance(value, dict) and isinstance(saved[key], dict) and "string" not in value:
                out[key] = restore(value, saved[key])
            continue
        if isinstance(value, str):
            out[key] = ""
        elif isinstance(value, dict) and "string" in value:
            out[key] = {"string": []}
    return out


def example(props: dict) -> dict:
    """A spec setting every property in a schema `properties`, from the schema itself."""
    out = {}
    for name, prop in props.items():
        if name in ("geoserver", "workspace", "masterPassword", "tileCaching", "service", "security"):  # the master password needs a real
            # Secret; tile caching defaults are a file GeoServer reads when it starts (test_e2e_tiles.py); security: test_e2e_auth.py
            continue
        if name in VALUES:
            out[name] = VALUES[name]
        elif "enum" in prop:
            out[name] = prop["enum"][-1]
        elif prop.get("type") == "boolean":
            out[name] = True
        elif prop.get("type") == "integer":
            out[name] = 7
        elif prop.get("type") == "object":
            out[name] = example(prop["properties"])
        else:
            out[name] = f"e2e {name}"
    return out


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    saved = {path: gs.get(path) for path in SAVED}
    gs.delete_workspace(WS)
    gs.upsert_workspace(WS)
    yield gs
    gs.delete_workspace(WS)
    for path, body in saved.items():
        if path == "security/acl/catalog":
            gs.request("PUT", path, body)
            continue
        (key, before), = body.items()
        before.pop("updateSequence", None)
        gs.request("PUT", path, {key: restore(gs.get(path)[key], before)})


@pytest.fixture
def project(tmp_path, monkeypatch):
    defs = schema()["$defs"]
    docs = [
        {"kind": "Project", "metadata": {"name": "e2e-settings"}, "spec": {
            "geoserver": {"url": URL.removesuffix("/rest"), "credentials": {"secret": "e2egs"}},
            "database": {"jndi": "java:comp/env/jdbc/mypostgis", "connection": {"secret": "e2edb"}}, "workspace": WS}},
        {"kind": "Settings", "metadata": {"name": "global"}, "spec": example(defs["settings"]["properties"])},
        {"kind": "Settings", "metadata": {"name": "own"}, "spec": {
            "workspace": WS, "contact": {"contactPerson": "Workspace Team"}, "numDecimals": 3}},
    ]
    docs += [{"kind": "Service", "metadata": {"name": s}, "spec": example(defs[f"service_{s}"]["properties"])}
             for s in ("wms", "wfs", "wcs", "wmts")]
    docs.append({"kind": "Service", "metadata": {"name": "own-wms"}, "spec": {
        "service": "wms", "workspace": WS, "title": "Workspace maps", "maxBuffer": 25}})
    (tmp_path / "project.yaml").write_text(yaml.safe_dump_all([{"apiVersion": "configops/v1alpha1", **d} for d in docs]))
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": DB["port"], "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": "postgres",
                       "E2EDB_PASSWORD": os.environ.get("E2E_DB_PASSWORD", "postgres")}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    return tmp_path


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def test_every_field_is_applied_then_nothing_to_do(gs, project, capsys):
    assert configops(capsys, "validate", str(project))[0] == 0
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    code, out = configops(capsys, "plan", "-C", str(project))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out

    assert gs.get("services/wms/settings")["wms"]["abstrct"] == "e2e abstract"
    assert gs.get("settings/contact")["contact"]["contactPerson"] == "e2e contactPerson"
    assert gs.get(f"workspaces/{WS}/settings")["settings"]["contact"]["contactPerson"] == "Workspace Team"
    own = gs.get(f"services/wms/workspaces/{WS}/settings")["wms"]
    assert own["title"] == "Workspace maps" and own["maxBuffer"] == 25 and own["abstrct"] == "e2e abstract"  # the rest from the global


def test_import_round_trip(gs, project, tmp_path_factory, capsys):
    """What apply set, import writes back: plan on the imported files has nothing to do."""
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text((project / "project.yaml").read_text().split("\n---\n")[0] + "\n")
    code, out = configops(capsys, "import", "--settings", "-C", str(imported))
    assert code == 0 and "server.yaml: 2 Settings, 5 Service(s)" in out, out
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out


def test_drift_is_shown_and_put_back(gs, project, capsys):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    wms = gs.get("services/wms/settings")["wms"]
    gs.request("PUT", "services/wms/settings", {"wms": {**wms, "title": "Edited in the web UI"}})
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2 and 'title: "Edited in the web UI" -> "e2e title"' in out
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    assert configops(capsys, "plan", "-C", str(project))[0] == 0
