"""What security needs GeoServer's own files for: a role's parent (its rights inherited) and properties,
a user's properties, a group turned off. Checked by their effect on the REST service: a REST rule for
the parent role lets in a user who has only the child role, and not one whose group with that role is
off. Fresh names and passwords each run (GeoServer caches logins); everything made is removed after.
"""

import os
import secrets as random
import time

import pytest
import requests
import yaml

from configops.cli import main
from configops.geoserver import GeoServer
from configops.security import REST_FILE, ROLES_FILE, USERS_FILE, SecurityFiles

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL:
    pytest.skip("E2E_GEOSERVER_URL not set", allow_module_level=True)

PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=os.environ.get("E2E_DB_PORT", "55433"),
          password=os.environ.get("E2E_DB_PASSWORD", "postgres"))
TAG = random.token_hex(3)
PARENT, CHILD = f"E2E_PARENT_{TAG.upper()}", f"E2E_CHILD_{TAG.upper()}"
HEIR, OUTSIDER, GROUP = f"e2e-heir-{TAG}", f"e2e-outsider-{TAG}", f"e2e-off-{TAG}"


@pytest.fixture
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    rules = gs.request("GET", REST_FILE).text
    yield gs
    for path in (f"security/usergroup/user/{HEIR}", f"security/usergroup/user/{OUTSIDER}", f"security/usergroup/group/{GROUP}",
                 f"security/roles/role/{CHILD}", f"security/roles/role/{PARENT}"):
        gs.request("DELETE", path, ok=(200, 400, 404))
    gs.request("PUT", REST_FILE, rules, content_type="text/plain")
    gs.delete_workspace("configops_security_files")  # apply makes the project's workspace


@pytest.fixture
def project(tmp_path, monkeypatch):
    passwords = {HEIR: random.token_urlsafe(12), OUTSIDER: random.token_urlsafe(12)}
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"], "E2EDB_PORT": DB["port"],
                       "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": "postgres", "E2EDB_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    monkeypatch.setenv("CONFIGOPS_E2EUSERS_HEIR", passwords[HEIR])
    monkeypatch.setenv("CONFIGOPS_E2EUSERS_OUTSIDER", passwords[OUTSIDER])
    docs = [
        {"kind": "Project", "metadata": {"name": "e2e-security-files"}, "spec": {
            "geoserver": {"url": URL.removesuffix("/rest"), "credentials": {"secret": "e2egs"}},
            "database": {"jndi": "java:comp/env/jdbc/mypostgis", "connection": {"secret": "e2edb"}}, "workspace": "configops_security_files"}},
        {"kind": "Role", "metadata": {"name": "parent"}, "spec": {"name": PARENT}},
        {"kind": "Role", "metadata": {"name": "child"}, "spec": {"name": CHILD, "parent": PARENT, "properties": {"team": "maps"}}},
        {"kind": "Group", "metadata": {"name": "off"}, "spec": {"name": GROUP, "roles": [PARENT], "enabled": False}},
        {"kind": "User", "metadata": {"name": "heir"}, "spec": {"name": HEIR, "password": {"secret": "e2eusers", "key": "heir"},
                                                                 "roles": [CHILD], "properties": {"email": "heir@example.org"}}},
        {"kind": "User", "metadata": {"name": "outsider"}, "spec": {"name": OUTSIDER, "password": {"secret": "e2eusers", "key": "outsider"},
                                                                     "groups": [GROUP]}},
        {"kind": "AccessRule", "metadata": {"name": "about"}, "spec": {"rest": {"path": "/rest/about/**", "methods": ["GET"]}, "roles": [PARENT]}},
    ]
    (tmp_path / "project.yaml").write_text(yaml.safe_dump_all([{"apiVersion": "configops/v1alpha1", **d} for d in docs]))
    return tmp_path, passwords


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def status(user: str, password: str) -> int:
    return requests.get(f"{URL}/about/version.json", auth=(user, password), timeout=30).status_code


def test_role_parent_properties_and_a_group_turned_off(gs, project, capsys):
    root, passwords = project
    code, out = configops(capsys, "apply", "-C", str(root))
    assert code == 0, out
    code, out = configops(capsys, "plan", "-C", str(root))
    assert code == 0 and "~" not in out and "+" not in out, out

    files = SecurityFiles(gs)
    assert files.role(CHILD) == {"parent": PARENT, "properties": {"team": "maps"}}
    assert files.user_properties(HEIR) == {"email": "heir@example.org"}
    assert files.group_enabled(GROUP) is False

    deadline = time.time() + 60
    while status(HEIR, passwords[HEIR]) != 200 and time.time() < deadline:
        time.sleep(3)
    assert status(HEIR, passwords[HEIR]) == 200  # the child role inherits the parent's REST rule
    assert status(OUTSIDER, passwords[OUTSIDER]) == 403  # the group with the parent role is off


def test_import_writes_them_back(gs, project, capsys, tmp_path_factory):
    root, _ = project
    assert configops(capsys, "apply", "-C", str(root))[0] == 0
    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text((root / "project.yaml").read_text().split("\n---\n")[0] + "\n")
    code, out = configops(capsys, "import", "--security", "-C", str(imported))
    assert code == 0, out
    docs = {(d["kind"], d["spec"].get("name") or d["metadata"]["name"]): d["spec"]
            for d in yaml.safe_load_all((imported / "security.yaml").read_text()) if d}
    child = next(s for (k, n), s in docs.items() if k == "Role" and (n == CHILD or n.upper().replace("-", "_") == CHILD))
    assert child["parent"] == PARENT and child["properties"] == {"team": "maps"}
    assert docs[("Group", GROUP)]["enabled"] is False and docs[("User", HEIR)]["properties"] == {"email": "heir@example.org"}
