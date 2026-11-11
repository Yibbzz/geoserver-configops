"""Security as code: Role, Group, User and AccessRule resources.

GeoServer's default user/group service and role service, and its access rules
for layers, services and the REST API. A project manages security only when it
has at least one of these resources; then users, groups, roles and rules the
files don't mention are shown as unmanaged and removed only with --prune.
GeoServer's own (the admin user, the ADMIN and GROUP_ADMIN roles, the default
rules) are never touched unless a file names them.

Passwords come from Secrets and can't be read back, so plan checks one by
logging in as the user: 401 means it differs.
"""

from __future__ import annotations

import requests

from . import secrets
from .geoserver import MissingCredentials
from .loader import Resource

BUILT_IN_USERS = {"admin"}
BUILT_IN_ROLES = {"ADMIN", "GROUP_ADMIN"}
DEFAULT_RULES = {
    "layers": {"*.*.r", "*.*.w"},
    "services": {"wfs.CreateStoredQuery", "wfs.DropStoredQuery"},
    "rest": {"/**:GET,HEAD", "/**:POST,DELETE,PUT"},
}
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
        add("+", "role", name, run=lambda n=name: gs.request("POST", f"security/roles/role/{n}"))
    for name in sorted(live_roles - wanted_roles - BUILT_IN_ROLES):
        unmanaged("role", name, lambda n=name: drop(gs, f"security/roles/role/{n}"))

    # Groups
    live_groups = set(names(gs.get("security/usergroup/groups"), "groups"))
    wanted_groups = {}
    for r in files.of_kind("Group"):
        wanted_groups[account_name(r)] = sorted(r.spec.get("roles", []))
    for name, roles in sorted(wanted_groups.items()):
        current = names(gs.get(f"security/roles/group/{name}"), "roles") if name in live_groups else []
        run = lambda n=name, want=roles, have=current, new=name not in live_groups: set_group(gs, n, want, have, new)
        if name not in live_groups:
            add("+", "user group", name, note=f"roles: {', '.join(roles) or 'none'}", run=run)
        elif current != roles:
            add("~", "user group", name, [("roles", current, roles)], run=run)
    for name in sorted(live_groups - set(wanted_groups)):
        unmanaged("user group", name, lambda n=name: drop(gs, f"security/usergroup/group/{n}"))

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
                "groups": names(gs.get(f"security/usergroup/user/{name}/groups"), "groups"),
                "roles": names(gs.get(f"security/roles/user/{name}"), "roles")}
        changed = [(k, have[k], want[k]) for k in want if have[k] != want[k]]
        new_password = None
        if want["enabled"] and not password_works(gs, name, password):
            changed.append(("password", "(differs)", f"(from secret {ref['secret']}, key {ref['key']})"))
            new_password = password
        if changed:
            add("~", "user", name, changed, run=lambda n=name, w=want, p=new_password, h=have: set_user(gs, n, w, p, h, new=False))
    for name in sorted(set(live_users) - wanted_users - BUILT_IN_USERS):
        unmanaged("user", name, lambda n=name: drop(gs, f"security/usergroup/user/{n}"))

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


def as_items(value) -> list[dict]:
    if not value:
        return []
    return value if isinstance(value, list) else [value]


def set_group(gs, name: str, want: list[str], have: list[str], new: bool) -> None:
    if new:
        gs.request("POST", f"security/usergroup/group/{name}")
    for role in sorted(set(want) - set(have)):
        gs.request("POST", f"security/roles/role/{role}/group/{name}")
    for role in sorted(set(have) - set(want)):
        gs.request("DELETE", f"security/roles/role/{role}/group/{name}")


def set_user(gs, name: str, want: dict, password: str | None, have: dict, new: bool) -> None:
    user = {"enabled": want["enabled"]}
    if password is not None:
        user["password"] = password
    if new:
        gs.request("POST", "security/usergroup/users", {"user": {"userName": name, **user}})
    else:
        gs.request("POST", f"security/usergroup/user/{name}", {"user": user})
    for group in sorted(set(want["groups"]) - set(have["groups"])):
        gs.request("POST", f"security/usergroup/user/{name}/group/{group}")
    for group in sorted(set(have["groups"]) - set(want["groups"])):
        gs.request("DELETE", f"security/usergroup/user/{name}/group/{group}")
    for role in sorted(set(want["roles"]) - set(have["roles"])):
        gs.request("POST", f"security/roles/role/{role}/user/{name}")
    for role in sorted(set(have["roles"]) - set(want["roles"])):
        gs.request("DELETE", f"security/roles/role/{role}/user/{name}")


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
    gs.request("DELETE", f"security/acl/{kind}/{key}", ok=(200, 404))


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
