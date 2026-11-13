"""Role, Group, User and AccessRule resources against the real GeoServer Cloud REST service.

Start it with tests/e2e/up.sh, then:

    E2E_GEOSERVER_URL=http://localhost:59090/geoserver-cloud/rest E2E_DB_PORT=55433 pytest -m e2e

What the test makes is removed at the end, and the access rules put back as they were.
Run it against a GeoServer of its own: the prune test removes every user, group and
role the files don't name.
"""

import os
import secrets
import time

import pytest
import requests

from configops.cli import main
from configops.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL:
    pytest.skip("E2E_GEOSERVER_URL not set", allow_module_level=True)

PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=os.environ.get("E2E_DB_PORT", "55433"))
REST_FILE = "resource/security/rest.properties"
# Fresh each run: GeoServer caches a login for minutes, so a password from an earlier run still works for a while
PW = {name: f"{name}-{secrets.token_hex(4)}" for name in ("alice-1", "alice-2", "bob")}
SECURITY = """\
apiVersion: configops/v1alpha1
kind: Role
metadata: { name: e2e-editor }                 # GeoServer role E2E_EDITOR
spec: {}
---
apiVersion: configops/v1alpha1
kind: Group
metadata: { name: e2e-editors }
spec: { roles: [E2E_EDITOR] }
---
apiVersion: configops/v1alpha1
kind: User
metadata: { name: e2e-alice }
spec: { password: { secret: e2eusers, key: alice }, groups: [e2e-editors] }
---
apiVersion: configops/v1alpha1
kind: User
metadata: { name: e2e-bob }
spec: { password: { secret: e2eusers, key: bob }, enabled: false, roles: [E2E_EDITOR] }
---
apiVersion: configops/v1alpha1
kind: AccessRule
metadata: { name: private }
spec: { layer: { workspace: e2e_private, layer: "*", access: read }, roles: [E2E_EDITOR] }
---
apiVersion: configops/v1alpha1
kind: AccessRule
metadata: { name: transactions }
spec: { service: { service: wfs, operation: Transaction }, roles: [E2E_EDITOR] }
---
apiVersion: configops/v1alpha1
kind: AccessRule
metadata: { name: rest-about }
spec: { rest: { path: /rest/about/**, methods: [GET] }, roles: [E2E_EDITOR] }
"""


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    saved = {kind: gs.get(f"security/acl/{kind}") for kind in ("layers", "services")}
    saved_rest = gs.request("GET", REST_FILE).text
    yield gs
    for path in ("usergroup/user/e2e-alice", "usergroup/user/e2e-bob", "usergroup/user/e2e-carol",
                 "usergroup/group/e2e-editors", "roles/role/E2E_EDITOR"):
        gs.request("DELETE", f"security/{path}", ok=(200, 400, 404))  # 400: already gone
    for kind, rules in saved.items():
        for key in set(gs.get(f"security/acl/{kind}")) - set(rules):
            gs.request("DELETE", f"security/acl/{kind}/{key}")
        for key, roles in rules.items():
            gs.request("PUT", f"security/acl/{kind}", {key: roles})
    gs.request("PUT", REST_FILE, saved_rest, content_type="text/plain")


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "project.yaml").write_text(f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-security }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: e2e_security
---
{SECURITY}""")
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": DB["port"], "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": "postgres",
                       "E2EDB_PASSWORD": os.environ.get("E2E_DB_PASSWORD", "postgres"),
                       "E2EUSERS_ALICE": PW["alice-1"], "E2EUSERS_BOB": PW["bob"]}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    return tmp_path


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def status_as(user: str, password: str, path: str = "about/version.json", expect: int | None = None) -> int:
    """The status GeoServer answers; with `expect`, retried for 30 s while new rules load."""
    for _ in range(15):
        code = requests.get(f"{URL}/{path}", auth=(user, password), timeout=30).status_code
        if expect is None or code == expect:
            return code
        time.sleep(2)
    return code


def test_apply_then_nothing_to_do_and_rules_take_effect(gs, project, capsys):
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 0 and not [line for line in out.splitlines() if line.startswith(("  +", "  ~", "  -"))], out

    assert gs.get("security/usergroup/user/e2e-alice/groups")["groups"] in (["e2e-editors"], "e2e-editors")
    assert "E2E_EDITOR" in str(gs.get("security/roles/group/e2e-editors"))
    assert gs.get("security/acl/layers")["e2e_private.*.r"] == "E2E_EDITOR"
    assert gs.request("GET", REST_FILE).text.splitlines()[0] == "/rest/about/**;GET=E2E_EDITOR"
    # alice reaches /rest/about through her group's role; bob is disabled; a wrong password is refused
    assert status_as("e2e-alice", PW["alice-1"], expect=200) == 200
    assert status_as("e2e-alice", PW["alice-1"], "workspaces.json") == 403
    assert status_as("e2e-bob", PW["bob"]) == 401
    assert status_as("e2e-alice", "wrong") == 401


def test_a_new_password_is_applied(gs, project, capsys, monkeypatch):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    monkeypatch.setenv("CONFIGOPS_E2EUSERS_ALICE", PW["alice-2"])
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2 and 'password: "(differs)"' in out
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    assert status_as("e2e-alice", PW["alice-2"], expect=200) == 200
    assert configops(capsys, "plan", "-C", str(project))[0] == 0


def test_import_round_trip(gs, project, tmp_path_factory, capsys, monkeypatch):
    """What apply set, import writes back; with the passwords in the Secret it names, plan has nothing to do."""
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text((project / "project.yaml").read_text().split("\n---\n")[0] + "\n")
    code, out = configops(capsys, "import", "--security", "-C", str(imported))
    assert code == 0 and "1 role(s), 1 group(s), 2 user(s), 3 rule(s)" in out, out
    assert "Secret geoserver-users (keys: e2e-alice, e2e-bob)" in out
    monkeypatch.setenv("CONFIGOPS_GEOSERVER_USERS_E2E_ALICE", os.environ["CONFIGOPS_E2EUSERS_ALICE"])
    monkeypatch.setenv("CONFIGOPS_GEOSERVER_USERS_E2E_BOB", os.environ["CONFIGOPS_E2EUSERS_BOB"])
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert code == 0 and not [line for line in out.splitlines() if line.startswith(("  +", "  ~", "  -"))], out


def test_unmanaged_left_alone_until_pruned(gs, project, capsys):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    gs.request("POST", "security/usergroup/users", {"user": {"userName": "e2e-carol", "password": "x", "enabled": True}})
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 0 and "? user e2e-carol" in out
    code, out = configops(capsys, "apply", "-C", str(project), "--prune")
    assert code == 0 and "- user e2e-carol" in out
    assert "e2e-carol" not in str(gs.get("security/usergroup/users"))
    assert "admin" in str(gs.get("security/usergroup/users"))  # GeoServer's own, never pruned
