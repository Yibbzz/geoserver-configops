"""Role, Group, User and AccessRule resources (gisdata/security.py) against a fake GeoServer."""

import types

import pytest

from gisdata import ingest, security
from gisdata.geoserver import MissingCredentials
from gisdata.loader import load
from gisdata.plan import Planner, ordered
from gisdata.validate import validate

PROJECT = """\
apiVersion: gisdata/v1alpha1
kind: Project
metadata: { name: t }
spec:
  geoserver: { url: http://gs:8080/geoserver-cloud, credentials: { secret: gs } }
  database: { jndi: java:comp/env/jdbc/x, connection: { secret: pg } }
  workspace: uk
"""

SECURITY = """\
apiVersion: gisdata/v1alpha1
kind: Role
metadata: { name: editor }
spec: {}
---
apiVersion: gisdata/v1alpha1
kind: Group
metadata: { name: editors }
spec: { roles: [EDITOR] }
---
apiVersion: gisdata/v1alpha1
kind: User
metadata: { name: alice }
spec: { password: { secret: users, key: alice }, groups: [editors] }
---
apiVersion: gisdata/v1alpha1
kind: AccessRule
metadata: { name: private }
spec: { layer: { workspace: uk, layer: secret, access: read }, roles: [EDITOR] }
---
apiVersion: gisdata/v1alpha1
kind: AccessRule
metadata: { name: transactions }
spec: { service: { service: wfs, operation: Transaction }, roles: [EDITOR] }
---
apiVersion: gisdata/v1alpha1
kind: AccessRule
metadata: { name: rest-uk }
spec: { rest: { path: /rest/workspaces/uk/**, methods: [GET] }, roles: [EDITOR] }
"""

DEFAULT_REST = "/**;GET,HEAD=ADMIN\n/**;POST,DELETE,PUT=ADMIN\n"


class FakeGeoServer:
    """GeoServer's security REST API, held in memory."""

    def __init__(self):
        self.rest = "http://gs/rest"
        self.roles = {"ADMIN", "GROUP_ADMIN"}
        self.groups: dict[str, set] = {}  # group -> roles
        self.users = {"admin": {"enabled": True, "password": "geoserver", "groups": set(), "roles": {"ADMIN"}}}
        self.acl = {"layers": {"*.*.r": "*", "*.*.w": "GROUP_ADMIN,ADMIN"},
                    "services": {"wfs.CreateStoredQuery": "GROUP_ADMIN,ADMIN", "wfs.DropStoredQuery": "GROUP_ADMIN,ADMIN"}}
        self.rest_file = DEFAULT_REST

    def get(self, path):
        parts = path.split("/")
        if path == "security/roles":
            return {"roles": sorted(self.roles)}
        if path == "security/usergroup/groups":
            return {"groups": sorted(self.groups)}
        if path == "security/usergroup/users":
            return {"users": [{"userName": n, "enabled": u["enabled"], "password": None} for n, u in self.users.items()]}
        if path.startswith("security/roles/group/"):
            return {"roles": sorted(self.groups[parts[-1]])}
        if path.startswith("security/roles/user/"):
            return {"roles": sorted(self.users[parts[-1]]["roles"])}
        if path.startswith("security/usergroup/user/") and path.endswith("/groups"):
            return {"groups": sorted(self.users[parts[3]]["groups"])}
        if path.startswith("security/acl/"):
            return dict(self.acl[parts[2]])
        raise AssertionError(path)

    def request(self, method, path, body=None, content_type=None, *args, **kwargs):
        parts = path.split("/")
        if path == "resource/security/rest.properties":
            if method == "PUT":
                self.rest_file = body
            return types.SimpleNamespace(text=self.rest_file)
        if path.startswith("security/acl/"):
            kind = parts[2]
            if method in ("POST", "PUT"):
                for key, roles in body.items():
                    assert (key in self.acl[kind]) == (method == "PUT")  # POST adds, PUT changes, as GeoServer
                    self.acl[kind][key] = roles
            elif method == "DELETE":
                del self.acl[kind][parts[3]]
        elif method == "DELETE" and parts[1] == "usergroup":
            (self.users if parts[2] == "user" else self.groups).pop(parts[3])
        elif path.startswith("security/roles/role/"):
            role = parts[3]
            if len(parts) == 4:
                self.roles.add(role) if method == "POST" else self.roles.discard(role)
                for group in self.groups.values():
                    group.discard(role) if method == "DELETE" else None
            else:
                target = self.groups[parts[5]] if parts[4] == "group" else self.users[parts[5]]["roles"]
                target.add(role) if method == "POST" else target.discard(role)
        elif path == "security/usergroup/users":
            u = body["user"]
            self.users[u["userName"]] = {"enabled": u["enabled"], "password": u["password"], "groups": set(), "roles": set()}
        elif path.startswith("security/usergroup/group/"):
            self.groups[parts[3]] = set()
        elif path.startswith("security/usergroup/user/"):
            user = self.users[parts[3]]
            if len(parts) == 4:
                user.update(body["user"])
            else:
                user["groups"].add(parts[5]) if method == "POST" else user["groups"].discard(parts[5])
        else:
            raise AssertionError(path)


    def workspace(self, ws):
        return {"workspace": {"name": ws}}

    def names(self, path, key):
        return []


@pytest.fixture
def gs(monkeypatch):
    gs = FakeGeoServer()
    monkeypatch.setattr(security, "password_works", lambda g, name, pw: g.users[name]["password"] == pw)
    monkeypatch.setenv("GISDATA_USERS_ALICE", "pw-1")
    return gs


def plan(tmp_path, gs, extra=SECURITY, prune=False):
    (tmp_path / "project.yaml").write_text(PROJECT + "---\n" + extra)
    return ordered(Planner(load(tmp_path), tmp_path, gs, None, ingest.Database("h", 5432, "d", "u", "p"), prune).plan())


def lines(changes):
    return [(c.action, c.kind, c.target) for c in changes]


def test_from_nothing_in_order_then_nothing_to_do(tmp_path, gs):
    changes = plan(tmp_path, gs)
    assert lines(changes) == [
        ("+", "role", "EDITOR"), ("+", "user group", "editors"), ("+", "user", "alice"),
        ("+", "rule", "rest /rest/workspaces/uk/**:GET"), ("+", "rule", "layers uk.secret.r"),
        ("+", "rule", "services wfs.Transaction")]
    for c in changes:
        c.run()
    assert plan(tmp_path, gs) == []
    assert gs.users["alice"] == {"enabled": True, "password": "pw-1", "groups": {"editors"}, "roles": set()}
    assert gs.groups == {"editors": {"EDITOR"}}
    assert gs.acl["layers"]["uk.secret.r"] == "EDITOR"
    # the project's REST rule before GeoServer's catch-all /**, or it would never be reached
    assert gs.rest_file.splitlines()[0] == "/rest/workspaces/uk/**;GET=EDITOR"


def test_drift_password_and_rule_order(tmp_path, gs, monkeypatch):
    for c in plan(tmp_path, gs):
        c.run()
    monkeypatch.setenv("GISDATA_USERS_ALICE", "pw-2")
    gs.groups["editors"].discard("EDITOR")
    gs.rest_file = DEFAULT_REST + "/rest/workspaces/uk/**;GET=EDITOR\n"  # moved below /** by hand
    changes = {c.target: c.fields for c in plan(tmp_path, gs)}
    assert changes["alice"] == [("password", "(differs)", "(from secret users, key alice)")]
    assert changes["editors"] == [("roles", [], ["EDITOR"])]
    assert changes["rest /rest/workspaces/uk/**:GET"] == [("place", "after /** (never reached)", "before /**")]
    for c in plan(tmp_path, gs):
        c.run()
    assert plan(tmp_path, gs) == []
    assert gs.users["alice"]["password"] == "pw-2"


def test_unmanaged_left_alone_until_pruned_and_built_ins_never(tmp_path, gs):
    for c in plan(tmp_path, gs):
        c.run()
    gs.users["carol"] = {"enabled": True, "password": "x", "groups": set(), "roles": set()}
    gs.roles.add("OLD")
    gs.acl["layers"]["uk.other.r"] = "OLD"
    gs.rest_file = "/rest/old/**;GET=OLD\n" + gs.rest_file
    unmanaged = sorted(lines(plan(tmp_path, gs)))
    assert unmanaged == [("?", "role", "OLD"), ("?", "rule", "layers uk.other.r"), ("?", "rule", "rest /rest/old/**:GET"),
                         ("?", "user", "carol")]
    for c in plan(tmp_path, gs, prune=True):
        c.run()
    assert set(gs.users) == {"admin", "alice"} and gs.roles == {"ADMIN", "GROUP_ADMIN", "EDITOR"}
    assert "uk.other.r" not in gs.acl["layers"] and "*.*.r" in gs.acl["layers"]
    assert gs.rest_file == "/rest/workspaces/uk/**;GET=EDITOR\n" + DEFAULT_REST


def test_no_security_in_the_files_means_none_touched(tmp_path, gs):
    gs.users["carol"] = {"enabled": True, "password": "x", "groups": set(), "roles": set()}
    assert plan(tmp_path, gs, extra="apiVersion: gisdata/v1alpha1\nkind: Source\nmetadata: { name: w }\nspec: { type: url }\n", prune=True) == []


def test_a_missing_password_stops_the_plan(tmp_path, gs, monkeypatch):
    monkeypatch.delenv("GISDATA_USERS_ALICE")
    with pytest.raises(MissingCredentials, match="user alice: no password in secret users, key alice"):
        plan(tmp_path, gs)


def test_validate_names_and_references(tmp_path):
    (tmp_path / "project.yaml").write_text(PROJECT + """---
apiVersion: gisdata/v1alpha1
kind: Role
metadata: { name: admin }
spec: {}
---
apiVersion: gisdata/v1alpha1
kind: User
metadata: { name: admin }
spec: { password: { secret: s, key: k } }
---
apiVersion: gisdata/v1alpha1
kind: User
metadata: { name: dave }
spec: { password: { secret: s, key: k }, groups: [nobody], roles: [MISSING] }
---
apiVersion: gisdata/v1alpha1
kind: AccessRule
metadata: { name: a }
spec: { layer: { workspace: uk }, roles: ["*"] }
---
apiVersion: gisdata/v1alpha1
kind: AccessRule
metadata: { name: b }
spec: { layer: { workspace: uk, layer: "*", access: read }, roles: [ADMIN] }
---
apiVersion: gisdata/v1alpha1
kind: AccessRule
metadata: { name: c }
spec: { roles: [ADMIN] }
""")
    messages = [p.message for p in validate(load(tmp_path)) if p.severity == "error"]
    assert "ADMIN is GeoServer's own role; use it without defining it" in messages
    assert any(m.startswith("admin is GeoServer's own user") for m in messages)
    assert any("spec.groups[0]: no Group named 'nobody'" in m for m in messages)
    assert any("spec.roles[0]: no Role named 'MISSING'" in m for m in messages)
    assert "the same rule as AccessRule a (layers uk.*.r)" in messages
    assert any("exactly one of layer, service or rest" in m for m in messages)
