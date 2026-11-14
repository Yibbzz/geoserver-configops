"""Security as code: Role, Group, User and AccessRule resources.

GeoServer's default user/group service and role service, and its access rules
for layers, services and the REST API. A project manages security only when it
has at least one of these resources; then users, groups, roles and rules the
files don't mention are shown as unmanaged and removed only with --prune.
GeoServer's own (the admin user, the ADMIN and GROUP_ADMIN roles, the default
rules) are never touched unless a file names them.

Passwords come from Secrets and can't be read back. GeoServer keeps a salted digest of each
in its users file, so plan checks the Secret's password against that; only when it can't tell
(another encoding, a file it can't read) or the digest doesn't match does it log in as the
user, where 401 means the password differs. A scheduled plan therefore makes no logins, failed
or otherwise, while the passwords are as the Secrets have them.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import unicodedata

import requests

from . import secrets
from .geoserver import GeoServerError, MissingCredentials, q
from .loader import Resource

BUILT_IN_USERS = {"admin"}
BUILT_IN_ROLES = {"ADMIN", "GROUP_ADMIN"}
DEFAULT_RULES = {
    "layers": {"*.*.r", "*.*.w"},
    "services": {"wfs.CreateStoredQuery", "wfs.DropStoredQuery"},
    "rest": {"/**:GET,HEAD", "/**:POST,DELETE,PUT"},
}
# The roles each has in a fresh GeoServer. One with other roles (everyone may write, say) is shown as unmanaged
DEFAULT_RULE_ROLES = {"*.*.r": ["*"], "*.*.w": ["ADMIN", "GROUP_ADMIN"], "wfs.CreateStoredQuery": ["ADMIN", "GROUP_ADMIN"],
                      "wfs.DropStoredQuery": ["ADMIN", "GROUP_ADMIN"], "/**:GET,HEAD": ["ADMIN"], "/**:POST,DELETE,PUT": ["ADMIN"]}


def changed_default(add, kind: str, key: str, roles: list[str]) -> None:
    """A `?` line for one of GeoServer's own rules whose roles aren't the ones it comes with."""
    if roles != DEFAULT_RULE_ROLES[key]:
        add("?", "rule", f"{kind} {key}", [("roles", roles, DEFAULT_RULE_ROLES[key])],
            note="GeoServer's own rule, changed; a rule in the files with the same key sets it")
ACCESS = {"read": "r", "write": "w", "admin": "a"}
KINDS = ("Role", "Group", "User", "AccessRule")


def role_name(r: Resource) -> str:
    return r.spec.get("name") or (r.name or "").upper().replace("-", "_")


def account_name(r: Resource) -> str:
    """A user's or group's name in GeoServer."""
    return r.spec.get("name") or r.name or ""


def rule_key(r: Resource) -> tuple[str, str]:
    """(rule list, key) as GeoServer's REST API names them."""
    spec = r.spec
    if "layer" in spec:
        layer = spec["layer"]
        return "layers", f"{layer.get('workspace', '*')}.{layer.get('layer', '*')}.{ACCESS[layer.get('access', 'read')]}"
    if "service" in spec:
        service = spec["service"]
        return "services", f"{service.get('service', '*')}.{service.get('operation', '*')}"
    rest = spec["rest"]
    return "rest", f"{rest['path']}:{','.join(rest.get('methods', ['GET']))}"


def roles_text(roles: list[str]) -> str:
    return ",".join(roles)


def roles_list(text: str | None) -> list[str]:
    return sorted(r.strip() for r in (text or "").split(",") if r.strip())


def names(data: dict | None, key: str) -> list[str]:
    items = (data or {}).get(key) or []
    items = items if isinstance(items, list) else [items]
    return sorted(i.get("userName") if isinstance(i, dict) else i for i in items)


def plan(planner) -> None:
    files, gs = planner.files, planner.gs
    if not any(files.of_kind(k) for k in KINDS):
        return
    prune = planner.prune
    add = planner.add

    def unmanaged(kind: str, target: str, delete) -> None:
        if prune:
            add("-", kind, target, note="not in the files", run=delete)
        else:
            add("?", kind, target, note="unmanaged; removed with --prune")

    # Roles
    live_roles = set(names(gs.get("security/roles"), "roles"))
    wanted_roles = {role_name(r) for r in files.of_kind("Role")}
    for name in sorted(wanted_roles - live_roles):
        add("+", "role", name, run=lambda n=name: gs.request("POST", f"security/roles/role/{q(n)}"))
    for name in sorted(live_roles - wanted_roles - BUILT_IN_ROLES):
        unmanaged("role", name, lambda n=name: drop(gs, f"security/roles/role/{q(n)}"))
    role_files = SecurityFiles(gs)
    for r in files.of_kind("Role"):
        want = {k: r.spec[k] for k in ("parent", "properties") if k in r.spec}
        if want:
            have = role_files.role(role_name(r))
            changed = [(k, have.get(k), v) for k, v in want.items() if have.get(k) != v]
            if changed:
                add("~", "role", role_name(r), changed, run=lambda n=role_name(r), w=want: SecurityFiles(gs).set_role(n, w))

    # Groups
    live_groups = set(names(gs.get("security/usergroup/groups"), "groups"))
    wanted_groups = {}
    for r in files.of_kind("Group"):
        wanted_groups[account_name(r)] = sorted(r.spec.get("roles", []))
    for name, roles in sorted(wanted_groups.items()):
        current = names(gs.get(f"security/roles/group/{q(name)}"), "roles") if name in live_groups else []
        run = lambda n=name, want=roles, have=current, new=name not in live_groups: set_group(gs, n, want, have, new)
        if name not in live_groups:
            add("+", "user group", name, note=f"roles: {', '.join(roles) or 'none'}", run=run)
        elif current != roles:
            add("~", "user group", name, [("roles", current, roles)], run=run)
    for r in files.of_kind("Group"):
        if "enabled" in r.spec and role_files.group_enabled(account_name(r)) != r.spec["enabled"]:
            add("~", "user group", account_name(r), [("enabled", not r.spec["enabled"], r.spec["enabled"])],
                run=lambda n=account_name(r), on=r.spec["enabled"]: SecurityFiles(gs).set_group_enabled(n, on))
    for name in sorted(live_groups - set(wanted_groups)):
        unmanaged("user group", name, lambda n=name: drop(gs, f"security/usergroup/group/{q(n)}"))

    # Users
    live_users = {u["userName"]: u for u in as_items((gs.get("security/usergroup/users") or {}).get("users"))}
    wanted_users = set()
    for r in files.of_kind("User"):
        name = account_name(r)
        wanted_users.add(name)
        spec = r.spec
        ref = spec.get("password") or {}
        password = secrets.lookup(ref.get("secret", ""), ref.get("key", ""), planner.root)
        if password is None:
            raise MissingCredentials(f"user {name}: no password in secret {ref.get('secret')}, key {ref.get('key')}")
        want = {"enabled": spec.get("enabled", True), "groups": sorted(spec.get("groups", [])), "roles": sorted(spec.get("roles", []))}
        live = live_users.get(name)
        if live is None:
            add("+", "user", name, note=f"groups: {', '.join(want['groups']) or 'none'}; roles: {', '.join(want['roles']) or 'none'}",
                run=lambda n=name, w=want, p=password: set_user(gs, n, w, p, {"groups": [], "roles": []}, new=True))
            continue
        have = {"enabled": live.get("enabled", True),
                "groups": names(gs.get(f"security/usergroup/user/{q(name)}/groups"), "groups"),
                "roles": names(gs.get(f"security/roles/user/{q(name)}"), "roles")}
        changed = [(k, have[k], want[k]) for k in want if have[k] != want[k]]
        new_password = None
        if want["enabled"] and stored_password_matches(role_files.user_password(name), password) is not True \
                and not password_works(gs, name, password):
            changed.append(("password", "(differs)", f"(from secret {ref['secret']}, key {ref['key']})"))
            new_password = password
        if changed:
            add("~", "user", name, changed, run=lambda n=name, w=want, p=new_password, h=have: set_user(gs, n, w, p, h, new=False))
    for r in files.of_kind("User"):
        if "properties" in r.spec:
            have = role_files.user_properties(account_name(r))
            want = {str(k): str(v) for k, v in r.spec["properties"].items()}
            if have != want:
                add("~", "user", account_name(r), [("properties", have, want)],
                    run=lambda n=account_name(r), w=want: SecurityFiles(gs).set_user_properties(n, w))
    for name in sorted(set(live_users) - wanted_users - BUILT_IN_USERS):
        unmanaged("user", name, lambda n=name: drop(gs, f"security/usergroup/user/{q(n)}"))

    # Access rules
    wanted_rules: dict[str, dict[str, list[str]]] = {"layers": {}, "services": {}, "rest": {}}
    for r in files.of_kind("AccessRule"):
        kind, key = rule_key(r)
        wanted_rules[kind][key] = sorted(r.spec["roles"])
    plan_rest_rules(gs, wanted_rules.pop("rest"), add, unmanaged)
    for kind, wanted in wanted_rules.items():
        live = {k: roles_list(v) for k, v in (gs.get(f"security/acl/{kind}") or {}).items()}
        for key, roles in sorted(wanted.items()):
            body = {key: roles_text(roles)}
            if key not in live:
                add("+", "rule", f"{kind} {key}", note=f"roles: {', '.join(roles)}",
                    run=lambda k=kind, b=body: gs.request("POST", f"security/acl/{k}", b))
            elif live[key] != roles:
                add("~", "rule", f"{kind} {key}", [("roles", live[key], roles)],
                    run=lambda k=kind, b=body: gs.request("PUT", f"security/acl/{k}", b))
        for key in sorted(set(live) - set(wanted) - DEFAULT_RULES[kind]):
            unmanaged("rule", f"{kind} {key}", lambda k=kind, key=key: delete_rule(gs, k, key))
        for key in sorted((set(live) & DEFAULT_RULES[kind]) - set(wanted)):
            changed_default(add, kind, key, live[key])


def as_items(value) -> list[dict]:
    if not value:
        return []
    return value if isinstance(value, list) else [value]


def set_group(gs, name: str, want: list[str], have: list[str], new: bool) -> None:
    if new:
        gs.request("POST", f"security/usergroup/group/{q(name)}")
    for role in sorted(set(want) - set(have)):
        gs.request("POST", f"security/roles/role/{q(role)}/group/{q(name)}")
    for role in sorted(set(have) - set(want)):
        gs.request("DELETE", f"security/roles/role/{q(role)}/group/{q(name)}")


def set_user(gs, name: str, want: dict, password: str | None, have: dict, new: bool) -> None:
    user = {"enabled": want["enabled"]}
    if password is not None:
        user["password"] = password
    if new:
        gs.request("POST", "security/usergroup/users", {"user": {"userName": name, **user}})
    else:
        gs.request("POST", f"security/usergroup/user/{q(name)}", {"user": user})
    for group in sorted(set(want["groups"]) - set(have["groups"])):
        gs.request("POST", f"security/usergroup/user/{q(name)}/group/{q(group)}")
    for group in sorted(set(have["groups"]) - set(want["groups"])):
        gs.request("DELETE", f"security/usergroup/user/{q(name)}/group/{q(group)}")
    for role in sorted(set(want["roles"]) - set(have["roles"])):
        gs.request("POST", f"security/roles/role/{q(role)}/user/{q(name)}")
    for role in sorted(set(have["roles"]) - set(want["roles"])):
        gs.request("DELETE", f"security/roles/role/{q(role)}/user/{q(name)}")


def stored_password_matches(stored: str | None, password: str) -> bool | None:
    """Whether `password` is the one GeoServer's users file holds, or None when configops can't tell.
    `digest1:` is GeoServer's default: base64 of a 16-byte salt and SHA-256 over salt and password, 100 000 times."""
    if not stored:
        return None
    if stored.startswith("plain:"):
        return hmac.compare_digest(stored[len("plain:"):].encode(), password.encode())
    if not stored.startswith("digest1:"):
        return None
    try:
        raw = base64.b64decode(stored[len("digest1:"):], validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) != 48:
        return None
    digest = hashlib.sha256(raw[:16] + unicodedata.normalize("NFC", password).encode()).digest()
    for _ in range(99_999):
        digest = hashlib.sha256(digest).digest()
    return hmac.compare_digest(digest, raw[16:])


def password_works(gs, name: str, password: str) -> bool:
    """Whether GeoServer accepts this login: 401 means it doesn't (403 is a login without REST rights)."""
    try:
        r = requests.get(f"{gs.rest}/about/version.json", auth=(name, password), timeout=30)
    except requests.RequestException:
        return True  # can't tell; the admin calls around it will report the outage
    return r.status_code != 401


def drop(gs, path: str) -> None:
    """Delete a user, group or role; GeoServer Cloud answers 400 when it's already gone."""
    gs.request("DELETE", path, ok=(200, 400, 404))


def delete_rule(gs, kind: str, key: str) -> None:
    gs.request("DELETE", f"security/acl/{kind}/{q(key)}", ok=(200, 404))


# --- REST rules. GeoServer checks them in file order and the first match wins, so a rule
# after the default /** is never reached; and its DELETE can't take the slashes in a path.
# So they are kept in their file, security/rest.properties: the project's rules first, in
# the files' order, then other rules, then the /** defaults.

REST_FILE = "resource/security/rest.properties"


def read_rest_rules(gs) -> list[tuple[str, list[str]]]:
    """(path:methods, roles) in file order."""
    rules = []
    for row in gs.request("GET", REST_FILE).text.splitlines():
        row = row.strip()
        if not row or row.startswith("#") or "=" not in row:
            continue
        pattern, roles = row.split("=", 1)
        path, _, methods = pattern.partition(";")
        rules.append((f"{path}:{methods}", roles_list(roles)))
    return rules


def write_rest_rules(gs, rules: list[tuple[str, list[str]]]) -> None:
    text = "".join(f"{key.rsplit(':', 1)[0]};{key.rsplit(':', 1)[1]}={roles_text(roles)}\n" for key, roles in rules)
    gs.request("PUT", REST_FILE, text, content_type="text/plain")


def plan_rest_rules(gs, wanted: dict[str, list[str]], add, unmanaged) -> None:
    live = read_rest_rules(gs)
    live_roles = dict(live)
    catch_all = next((i for i, (key, _) in enumerate(live) if key.startswith("/**:")), len(live))
    reachable = {key for key, _ in live[:catch_all]}
    others = [(k, r) for k, r in live if k not in wanted and k not in DEFAULT_RULES["rest"]]

    def run(keep_others: bool):
        def write():
            current = read_rest_rules(gs)  # as it is when apply gets here
            kept = [(k, r) for k, r in current if k not in wanted and k not in DEFAULT_RULES["rest"]] if keep_others else []
            last = [(k, r) for k, r in current if k in DEFAULT_RULES["rest"] and k not in wanted]
            write_rest_rules(gs, list(wanted.items()) + kept + last)
        return write

    for key, roles in wanted.items():
        target = f"rest {key}"
        if key not in live_roles:
            add("+", "rule", target, note=f"roles: {', '.join(roles)}", run=run(keep_others=True))
        else:
            changed = [("roles", live_roles[key], roles)] if live_roles[key] != roles else []
            if key not in reachable and not key.startswith("/**:"):
                changed.append(("place", "after /** (never reached)", "before /**"))
            if changed:
                add("~", "rule", target, changed, run=run(keep_others=True))
    for key, _ in others:
        unmanaged("rule", f"rest {key}", run(keep_others=False))
    for key, roles in live:
        if key in DEFAULT_RULES["rest"] and key not in wanted:
            changed_default(add, "rest", key, roles)


# --- What the REST API doesn't reach: a role's parent and properties, a user's properties, a group
# turned off. They are in the default services' files, edited through /rest/resource; GeoServer reads
# them again after a security reload.

ROLES_FILE = "resource/security/role/default/roles.xml"
USERS_FILE = "resource/security/usergroup/default/users.xml"
ROLES_NS = "http://www.geoserver.org/security/roles"
USERS_NS = "http://www.geoserver.org/security/users"


class SecurityFiles:
    def __init__(self, gs):
        self.gs = gs
        self._roles = self._users = None

    def _read(self, path: str, ns: str):
        import xml.etree.ElementTree as ET

        ET.register_namespace("", ns)
        r = self.gs.request("GET", path, ok=(200, 404))
        return ET.fromstring(r.text) if r.status_code == 200 and r.text.lstrip().startswith("<") else None

    @property
    def roles(self):
        if self._roles is None:
            self._roles = self._read(ROLES_FILE, ROLES_NS)
        return self._roles

    @property
    def users(self):
        if self._users is None:
            self._users = self._read(USERS_FILE, USERS_NS)
        return self._users

    def _find(self, root, ns: str, path: str, attr: str, name: str):
        if root is None:
            return None
        return next((e for e in root.iterfind(path, {"s": ns}) if e.get(attr) == name), None)

    @staticmethod
    def _properties(element) -> dict:
        return {p.get("name"): p.text or "" for p in element if p.tag.endswith("property")} if element is not None else {}

    @staticmethod
    def _set_properties(element, ns: str, values: dict) -> None:
        import xml.etree.ElementTree as ET

        for p in [p for p in element if p.tag.endswith("property")]:
            element.remove(p)
        for k, v in values.items():
            ET.SubElement(element, f"{{{ns}}}property", {"name": k}).text = v

    def _write(self, path: str, root) -> None:
        import xml.etree.ElementTree as ET

        from . import auth

        self.gs.request("PUT", path, ET.tostring(root, encoding="unicode", xml_declaration=True), content_type="application/xml")
        auth.reload_security(self.gs)

    def role(self, name: str) -> dict:
        e = self._find(self.roles, ROLES_NS, "s:roleList/s:role", "id", name)
        out: dict = {}
        if e is not None and e.get("parentID"):
            out["parent"] = e.get("parentID")
        props = self._properties(e)
        if props:
            out["properties"] = props
        return out

    def set_role(self, name: str, want: dict) -> None:
        e = self._find(self.roles, ROLES_NS, "s:roleList/s:role", "id", name)
        if e is None:
            raise GeoServerError("PUT", ROLES_FILE, 404, f"role {name} isn't in GeoServer's default role service")
        if "parent" in want:
            if want["parent"]:
                e.set("parentID", want["parent"])
            else:
                e.attrib.pop("parentID", None)
        if "properties" in want:
            self._set_properties(e, ROLES_NS, {str(k): str(v) for k, v in want["properties"].items()})
        self._write(ROLES_FILE, self.roles)

    def group_enabled(self, name: str) -> bool:
        e = self._find(self.users, USERS_NS, "s:groups/s:group", "name", name)
        return e is None or e.get("enabled", "true") == "true"

    def set_group_enabled(self, name: str, on: bool) -> None:
        e = self._find(self.users, USERS_NS, "s:groups/s:group", "name", name)
        if e is None:
            raise GeoServerError("PUT", USERS_FILE, 404, f"group {name} isn't in GeoServer's default user/group service")
        e.set("enabled", str(on).lower())
        self._write(USERS_FILE, self.users)

    def user_password(self, name: str) -> str | None:
        """The user's password as the users file holds it (`digest1:...`), or None if it can't be read."""
        try:
            e = self._find(self.users, USERS_NS, "s:users/s:user", "name", name)
        except GeoServerError:
            return None
        return e.get("password") if e is not None else None

    def user_properties(self, name: str) -> dict:
        return self._properties(self._find(self.users, USERS_NS, "s:users/s:user", "name", name))

    def set_user_properties(self, name: str, want: dict) -> None:
        e = self._find(self.users, USERS_NS, "s:users/s:user", "name", name)
        if e is None:
            raise GeoServerError("PUT", USERS_FILE, 404, f"user {name} isn't in GeoServer's default user/group service")
        self._set_properties(e, USERS_NS, want)
        self._write(USERS_FILE, self.users)
