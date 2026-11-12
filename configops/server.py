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

import xml.etree.ElementTree as ET

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
    if isinstance(value, dict) and "string" in value and all(k == "string" or k.startswith("@") for k in value):  # "@class" for a set
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


# --- service fields GeoServer keeps in shapes of its own: the spec's form <-> GeoServer's

SPECIAL = ("metadata", "gml", "disabledVersions", "authorityURLs", "identifiers", "metadataLink")
LINK = {"url": "content", "type": "type", "standard": "metadataType"}  # as a Layer's metadataLinks: spec name -> GeoServer's
GML_VERSIONS = {"1.0": "V_10", "1.1": "V_11", "2.0": "V_20"}  # spec name -> GeoServer's


def as_list(value) -> list:
    return [] if value in (None, "") else value if isinstance(value, list) else [value]


def service_view(live: dict) -> dict:
    """A service's special fields in the spec's form."""
    entries = as_list((live.get("metadata") or {}).get("entry"))
    meta = {}
    for e in entries:
        if not isinstance(e, dict) or "@key" not in e:
            continue
        key = e["@key"]
        meta[key] = e[key] if key in e else typed(str(e.get("$", "")))
    link = live.get("metadataLink") or {}
    versions = {v: k for k, v in GML_VERSIONS.items()}
    gml = {versions.get(e.get("version"), e.get("version")): e.get("gml") or {}
           for e in as_list((live.get("gml") or {}).get("entry"))}
    return {
        "metadata": meta,
        "gml": gml,
        "disabledVersions": [v["version"] for v in as_list((live.get("disabledVersions") or {}).get("org.geotools.util.Version"))],
        "authorityURLs": as_list((live.get("authorityURLs") or {}).get("AuthorityURL")),
        "identifiers": as_list((live.get("identifiers") or {}).get("Identifier")),
        "metadataLink": {k: link[g] for k, g in LINK.items() if link.get(g)},
    }


def compare_special(view: dict, wanted: dict) -> list[tuple[str, object, object]]:
    out = []
    for key, value in wanted.items():
        here = view.get(key)
        if key in ("metadata", "gml"):  # only the entries (and their fields) the file sets
            for k, v in value.items():
                h = (here or {}).get(k)
                if isinstance(v, dict):
                    out += [(f"{key}.{k}.{f}", (h or {}).get(f), x) for f, x in v.items() if (h or {}).get(f) != x]
                elif h != v:
                    out.append((f"{key}.{k}", h, v))
        elif key == "metadataLink":
            out += [(f"{key}.{k}", here.get(k), v) for k, v in value.items() if here.get(k) != v]
        elif key == "disabledVersions" and sorted(here or []) != sorted(value):
            out.append((key, here, value))
        elif key in ("authorityURLs", "identifiers") and (here or []) != value:
            out.append((key, here, value))
    return out


def special_body(current: dict, wanted: dict) -> dict:
    """The special fields to PUT: metadata and GML merged into what GeoServer has (a PUT replaces them whole)."""
    view = service_view(current)
    out = {}
    if "metadata" in wanted:
        meta = view["metadata"]
        for k, v in wanted["metadata"].items():
            meta[k] = {**(meta.get(k) or {}), **v} if isinstance(v, dict) else v
        out["metadata"] = {"entry": [{"@key": k, k: v} if isinstance(v, dict) else
                                     {"@key": k, "$": str(v).lower() if isinstance(v, bool) else str(v)} for k, v in meta.items()]}
    if "gml" in wanted:
        gml = view["gml"]
        for k, v in wanted["gml"].items():
            gml[k] = {**(gml.get(k) or {}), **v}
        out["gml"] = {"entry": [{"version": GML_VERSIONS.get(k, k), "gml": v} for k, v in gml.items()]}
    if "disabledVersions" in wanted:
        out["disabledVersions"] = {"org.geotools.util.Version": [{"version": v} for v in wanted["disabledVersions"]]}
    if "authorityURLs" in wanted:
        out["authorityURLs"] = {"AuthorityURL": wanted["authorityURLs"]}
    if "identifiers" in wanted:
        out["identifiers"] = {"Identifier": wanted["identifiers"]}
    if "metadataLink" in wanted:
        out["metadataLink"] = {**(current.get("metadataLink") or {}), **{LINK[k]: v for k, v in wanted["metadataLink"].items()}}
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
        spec = dict(r.spec)
        name, ws = spec.pop("service", None) or r.name, spec.pop("workspace", None)
        special = {k: spec.pop(k) for k in list(spec) if k in SPECIAL}
        path = f"services/{name}/workspaces/{ws}/settings" if ws else f"services/{name}/settings"
        target = f"{name} ({ws})" if ws else name
        found = gs.get(path)
        live = (found or {}).get(name) or {}
        wanted = to_geoserver(spec)
        changed = compare(live, wanted) + compare_special(service_view(live), special)
        if found is not None and not changed:
            continue

        def run(name=name, ws=ws, path=path, wanted=wanted, special=special):
            current = (gs.get(path) or {}).get(name)
            if current is None:  # a workspace's own: starts from the global settings, as the web UI does
                current = {k: v for k, v in ((gs.get(f"services/{name}/settings") or {}).get(name) or {}).items()
                           if k not in ("id", "workspace")}
            body = {**merge(current, wanted), **special_body(current, special)}
            if ws:
                body["workspace"] = {"name": ws}
            gs.request("PUT", path, {name: body})
        if found is None:
            planner.add("+", "service", target, note="this workspace's own settings", run=run)
        else:
            planner.add("~", "service", target, changed, run=run)


def plan_settings(planner, gs, r: Resource) -> None:
    spec = dict(r.spec)
    ws = spec.pop("workspace", None)
    contact = to_geoserver(spec.pop("contact", None) or {})
    top_spec = dict(spec.pop("global", None) or {})
    catalog_mode = top_spec.pop("catalogMode", None)
    tile_caching = top_spec.pop("tileCaching", None)
    security_settings = top_spec.pop("security", None)
    if security_settings:
        from . import auth

        auth.plan_settings(planner, security_settings)
    master = top_spec.pop("masterPassword", None)
    logging = top_spec.pop("requestLogging", None)
    url_checks = top_spec.pop("urlChecks", None)
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
    wanted_meta = logging_entries(logging) if logging else {}
    live_meta = metadata(live)
    changed += [("global.requestLogging." + LOGGING_NAMES[k], typed(live_meta.get(k, LOGGING_DEFAULTS[k])), typed(v))
                for k, v in wanted_meta.items() if live_meta.get(k, LOGGING_DEFAULTS[k]) != v]
    checks_xml = None
    if url_checks is not None:
        checks_xml = gs.request("GET", URL_CHECKS_FILE, ok=(200, 404))
        checks_xml = checks_xml.text if checks_xml.status_code == 200 else "<checks/>"
        live_on = (ET.fromstring(checks_xml).findtext("enabled") or "true") == "true"
        if live_on != url_checks:
            changed.append(("global.urlChecks", live_on, url_checks))
        else:
            checks_xml = None
    if catalog_mode:
        live_mode = ((gs.get("security/acl/catalog") or {}).get("mode") or "").lower()
        if live_mode != catalog_mode:
            changed.append(("global.catalogMode", live_mode, catalog_mode))
    gwc_xml = None
    if tile_caching:
        live_gwc = gs.request("GET", GWC_FILE).text
        gwc_changes = compare_gwc(live_gwc, tile_caching)
        changed += [("global.tileCaching." + f, a, b) for f, a, b in gwc_changes]
        if gwc_changes:
            gwc_xml = set_gwc(live_gwc, tile_caching)
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
        if gwc_xml:
            gs.request("PUT", GWC_FILE, gwc_xml, content_type="application/xml")
            print("  note: GeoServer reads the tile caching defaults when its services start: "
                  "kubectl rollout restart the GeoServer deployments to use them now")
        current = (gs.get(path) or {}).get(key) or {}
        if ws:
            body = merge(current, {**settings, "contact": contact, "workspace": {"name": ws}})
        else:
            body = merge(current, {**top, "settings": merge(current.get("settings") or {}, settings)})
            body["settings"]["contact"] = merge(body["settings"].get("contact") or {}, contact)
            body.pop("updateSequence", None)  # GeoServer's own counter
            if wanted_meta:
                entries = {**metadata(current), **wanted_meta}
                body["metadata"] = {"entry": [{"@key": k, "$": v} for k, v in entries.items()]}
        gs.request("PUT", path, {key: body})
        if checks_xml:
            root = ET.fromstring(checks_xml)
            switch = root.find("enabled")
            if switch is None:
                switch = ET.SubElement(root, "enabled")
            switch.text = str(url_checks).lower()
            gs.request("PUT", URL_CHECKS_FILE, ET.tostring(root, encoding="unicode"), content_type="application/xml")
        if catalog_mode:
            gs.request("PUT", "security/acl/catalog", {"mode": catalog_mode.upper()})
        if new_master:
            # GeoServer Cloud sometimes answers 422 though it made the change: what counts is what it reads back
            old = (gs.get("security/masterpw") or {}).get("oldMasterPassword")
            gs.request("PUT", "security/masterpw", {"oldMasterPassword": old, "newMasterPassword": new_master}, ok=(200, 201, 422))
            if (gs.get("security/masterpw") or {}).get("oldMasterPassword") != new_master:
                raise GeoServerError("PUT", "security/masterpw", 422,
                                     "GeoServer refused the new master password (its policy wants at least 8 characters)")
            # GeoServer Cloud tells its other services before it has swapped the keystore in. If one of them
            # swaps it first, the REST service keeps the old keystore in memory and can't read any store
            # password until it reads its security files again
            from . import auth

            auth.reload_security(gs)
        if not ws and contact:  # the contact has its own endpoint, which is what the services read
            current_contact = (gs.get("settings/contact") or {}).get("contact") or {}
            gs.request("PUT", "settings/contact", {"contact": merge(current_contact, contact)})

    if exists:
        planner.add("~", "settings", target, changed, run=run)
    else:
        planner.add("+", "settings", target, note="this workspace's own settings", run=run)


# --- request logging: entries in the global settings' metadata, which GeoServer reads on every request

LOGGING_KEYS = {"enabled": "logRequestsEnabled", "headers": "logHeadersEnabled", "bodies": "logBodiesEnabled",
                "bodySize": "requestLogBufferSize"}  # spec name -> GeoServer's
LOGGING_NAMES = {v: k for k, v in LOGGING_KEYS.items()}
LOGGING_DEFAULTS = {"logRequestsEnabled": "false", "logHeadersEnabled": "false", "logBodiesEnabled": "false",
                    "requestLogBufferSize": "1024"}
URL_CHECKS_FILE = "resource/security/urlchecks.xml"  # the switch for every URL check (GeoServer's web UI: URL Checks)


def logging_entries(spec: dict) -> dict[str, str]:
    """The request logging spec as GeoServer's metadata entries (all text)."""
    return {LOGGING_KEYS[k]: str(v).lower() if isinstance(v, bool) else str(v) for k, v in spec.items()}


def metadata(settings: dict) -> dict[str, str]:
    """The global settings' metadata entries, as text; GeoServer gives a single entry as an object, not a list."""
    entries = (settings.get("metadata") or {}).get("entry") or []
    entries = entries if isinstance(entries, list) else [entries]
    return {e["@key"]: str(e.get("$", "")) for e in entries if isinstance(e, dict) and "@key" in e}


def typed(text: str):
    return text == "true" if text in ("true", "false") else int(text) if text.isdigit() else text


# --- tile caching defaults: GeoServer's gwc-gs.xml, which it reads only when it starts

GWC_FILE = "resource/gwc-gs.xml"


def gwc_text(value) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def gwc_values(xml: str) -> dict:
    """gwc-gs.xml's settings: text, or a list for the ones that hold <string>s."""
    out = {}
    for e in ET.fromstring(xml):
        items = e.findall("string")
        out[e.tag] = [i.text or "" for i in items] if items or e.tag.startswith("default") else (e.text or "")
    return out


def compare_gwc(xml: str, wanted: dict) -> list[tuple[str, object, object]]:
    live = gwc_values(xml)
    out = []
    for key, value in wanted.items():
        here = live.get(key)
        same = sorted(here or []) == sorted(value) if isinstance(value, list) else here == gwc_text(value)
        if not same:
            out.append((key, here, value))
    return out


def set_gwc(xml: str, wanted: dict) -> str:
    root = ET.fromstring(xml)
    for key, value in wanted.items():
        e = root.find(key)
        if e is None:
            e = ET.SubElement(root, key)
        for child in list(e):
            e.remove(child)
        if isinstance(value, list):
            e.text = None
            for v in value:
                ET.SubElement(e, "string").text = v
        else:
            e.text = gwc_text(value)
    return ET.tostring(root, encoding="unicode")
