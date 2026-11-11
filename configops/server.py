"""Settings and Service resources: GeoServer's settings, contact details and OGC services.

Each is one object GeoServer already has, so plan only ever changes it (or, for a
workspace's own settings, adds it). The files use GeoServer's field names, with
two friendlier forms: `abstract` (GeoServer spells it `abstrct`) and plain lists
(GeoServer wraps them as {"string": [...]}). `geoserver:` passes any other field
through as it is. Only the fields a file sets are compared.

GeoServer replaces what a PUT leaves out of its settings, so apply always reads
the whole object, merges the files' fields in, and writes it back.
"""

from __future__ import annotations

from . import secrets
from .geoserver import GeoServerError, MissingCredentials
from .loader import Resource

SERVICES = ("wms", "wfs", "wcs", "wmts")
RENAMES = {"abstract": "abstrct"}  # spec name -> GeoServer's
RAW = "geoserver"  # fields passed through as GeoServer names them


def to_geoserver(spec: dict) -> dict:
    """A spec's fields in GeoServer's shape: renamed, lists wrapped, `geoserver:` merged in."""
    out: dict = {}
    for key, value in spec.items():
        if key == RAW:
            continue
        if isinstance(value, dict):
            value = to_geoserver(value)
        elif isinstance(value, list):
            value = {"string": value}
        out[RENAMES.get(key, key)] = value
    return merge(out, spec.get(RAW) or {})


def merge(live: dict, wanted: dict) -> dict:
    """`live` with `wanted`'s fields set, nested objects merged rather than replaced."""
    out = dict(live)
    for key, value in wanted.items():
        out[key] = merge(out.get(key) or {}, value) if isinstance(value, dict) and "string" not in value else value
    return out


def unwrap(value):
    """GeoServer's {"string": x} as a list (one item comes back as a plain string)."""
    if isinstance(value, dict) and set(value) == {"string"}:
        inner = value["string"]
        return inner if isinstance(inner, list) else [inner]
    return value


def compare(live: dict, wanted: dict, path: str = "") -> list[tuple[str, object, object]]:
    """(field, live, wanted) for each field `wanted` sets that differs; nested fields as a.b."""
    out = []
    for key, value in wanted.items():
        name = path + next((k for k, v in RENAMES.items() if v == key), key)
        here = (live or {}).get(key)
        if isinstance(value, dict) and "string" not in value:
            out += compare(here or {}, value, name + ".")
            continue
        here, value = unwrap(here), unwrap(value)
        if value == [] and here is None:
            continue  # GeoServer drops an empty list
        if here != value:
            out.append((name, here, value))
    return out


def settings_paths(ws: str | None) -> tuple[str, str]:
    """(REST path, JSON key) of the global settings or one workspace's."""
    return (f"workspaces/{ws}/settings", "settings") if ws else ("settings", "global")


def plan(planner) -> None:
    """Settings and Service changes, added to the planner's list."""
    gs = planner.gs
    for r in planner.files.of_kind("Settings"):
        plan_settings(planner, gs, r)
    for r in planner.files.of_kind("Service"):
        name = r.name
        live = (gs.get(f"services/{name}/settings") or {}).get(name) or {}
        wanted = to_geoserver(r.spec)
        changed = compare(live, wanted)
        if changed:
            def run(name=name, wanted=wanted):
                current = (gs.get(f"services/{name}/settings") or {}).get(name) or {}
                gs.request("PUT", f"services/{name}/settings", {name: merge(current, wanted)})
            planner.add("~", "service", name, changed, run=run)


def plan_settings(planner, gs, r: Resource) -> None:
    spec = dict(r.spec)
    ws = spec.pop("workspace", None)
    contact = to_geoserver(spec.pop("contact", None) or {})
    top_spec = dict(spec.pop("global", None) or {})
    catalog_mode = top_spec.pop("catalogMode", None)
    master = top_spec.pop("masterPassword", None)
    top = to_geoserver(top_spec)
    settings = to_geoserver(spec)
    path, key = settings_paths(ws)
    live = (gs.get(path) or {}).get(key) or {}
    target = f"{ws} (workspace)" if ws else "global"

    if ws:
        live_settings = live
        exists = "workspace" in live  # GeoServer shows defaults for a workspace without settings of its own
    else:
        live_settings = live.get("settings") or {}
        exists = True
    live_contact = live_settings.get("contact") or {}
    changed = [("contact." + f, a, b) for f, a, b in compare(live_contact, contact)]
    changed += compare(live_settings, settings)
    changed += [("global." + f, a, b) for f, a, b in compare(live, top)]
    if catalog_mode:
        live_mode = ((gs.get("security/acl/catalog") or {}).get("mode") or "").lower()
        if live_mode != catalog_mode:
            changed.append(("global.catalogMode", live_mode, catalog_mode))
    new_master = None
    if master:
        new_master = secrets.lookup(master["secret"], master["key"], planner.root)
        if new_master is None:
            raise MissingCredentials(f"Settings {r.name}: no master password in secret {master['secret']}, key {master['key']}")
        if (gs.get("security/masterpw") or {}).get("oldMasterPassword") != new_master:
            changed.append(("global.masterPassword", "(differs)", f"(from secret {master['secret']}, key {master['key']})"))
        else:
            new_master = None
    if exists and not changed:
        return

    def run():
        current = (gs.get(path) or {}).get(key) or {}
        if ws:
            body = merge(current, {**settings, "contact": contact, "workspace": {"name": ws}})
        else:
            body = merge(current, {**top, "settings": merge(current.get("settings") or {}, settings)})
            body["settings"]["contact"] = merge(body["settings"].get("contact") or {}, contact)
            body.pop("updateSequence", None)  # GeoServer's own counter
        gs.request("PUT", path, {key: body})
        if catalog_mode:
            gs.request("PUT", "security/acl/catalog", {"mode": catalog_mode.upper()})
        if new_master:
            # GeoServer Cloud sometimes answers 422 though it made the change: what counts is what it reads back
            old = (gs.get("security/masterpw") or {}).get("oldMasterPassword")
            gs.request("PUT", "security/masterpw", {"oldMasterPassword": old, "newMasterPassword": new_master}, ok=(200, 201, 422))
            if (gs.get("security/masterpw") or {}).get("oldMasterPassword") != new_master:
                raise GeoServerError("PUT", "security/masterpw", 422,
                                     "GeoServer refused the new master password (its policy wants at least 8 characters)")
        if not ws and contact:  # the contact has its own endpoint, which is what the services read
            current_contact = (gs.get("settings/contact") or {}).get("contact") or {}
            gs.request("PUT", "settings/contact", {"contact": merge(current_contact, contact)})

    if exists:
        planner.add("~", "settings", target, changed, run=run)
    else:
        planner.add("+", "settings", target, note="this workspace's own settings", run=run)
