"""What only standard GeoServer can set, and how plan leaves it out on GeoServer Cloud.

A few of GeoServer's settings don't survive GeoServer Cloud's split into services (the
reasons are in CLOUD_CANT). On GeoServer Cloud, plan shows each one the files set as a `!`
line saying why, and leaves it out; on standard GeoServer it is applied like anything else.
configops tells the two apart by the version GeoServer reports (GeoServer.cloud).

Three of them are settings of their own, planned here:
  global.cors                   CORS: an entry in the global settings' metadata, read when GeoServer starts
  global.contentSecurityPolicy  the Content-Security-Policy header: security/csp.xml, read at once
  global.diskQuota              GeoWebCache's disk quota: its REST API changes it in memory only, so
                                apply also writes gwc/geowebcache-diskquota.xml, which GeoServer reads when it starts
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from .geoserver import GeoServerError

# (kind, path in the spec, why GeoServer Cloud can't set it)
CLOUD_CANT = [
    ("LayerGroup", ("styles",), "its catalog database doesn't keep a group style's layers, and the group breaks after a restart"),
    ("Settings", ("global", "cors"), "its gateway answers CORS requests: set the gateway's CORS instead (README)"),
    ("Settings", ("global", "contentSecurityPolicy"), "its services don't send the header"),
    ("Settings", ("global", "diskQuota"), "it turns GeoWebCache's disk quota off"),
]
CLOUD_STORE_TYPES = {"oracle": "its images don't have Oracle's JDBC driver"}
LABELS = {"LayerGroup": "group", "Layer": "layer", "Settings": "settings", "Store": "store"}


def only_standard(r) -> list[tuple]:
    """(the path in the spec, why) for each thing in a resource only standard GeoServer can set; () for an Oracle store."""
    found = []
    for kind, path, why in CLOUD_CANT:
        parent = r.spec if r.kind == kind else None
        for step in path[:-1]:
            parent = parent.get(step) if isinstance(parent, dict) else None
        if isinstance(parent, dict) and path[-1] in parent:
            found.append((path, why))
    if r.kind == "Store" and r.spec.get("type") in CLOUD_STORE_TYPES:
        found.append(((), CLOUD_STORE_TYPES[r.spec["type"]]))
    return found


def leave_out_on_cloud(planner) -> None:
    """On GeoServer Cloud, take out of the files what it can't set, with a `!` line for each.
    GeoServer is asked which it is only when the files set any of it."""
    found = [(r, path, why) for r in planner.files.resources for path, why in only_standard(r)]
    if not found or getattr(planner.gs, "cloud", False) is not True:
        return
    for r, path, why in found:
        if path:
            parent = r.spec
            for step in path[:-1]:
                parent = parent[step]
            del parent[path[-1]]
            planner.add("!", LABELS[r.kind], r.name, note=f"GeoServer Cloud can't set {'.'.join(path)}: {why}")
            continue
        planner.files.resources.remove(r)
        planner.add("!", "store", r.name, note=f"GeoServer Cloud can't have {r.spec['type']} stores: {why}")
        for layer in planner.files.of_kind("Layer"):
            if layer.spec.get("store") == r.name:
                planner.files.resources.remove(layer)
                planner.unpublished.add(layer.name)
                planner.add("!", "layer", layer.name, note=f"its store {r.name} can't be made on GeoServer Cloud")
    planner.layers = planner.files.named("Layer")
    planner.groups = planner.files.named("LayerGroup")


def plan(planner, spec: dict) -> None:
    """Settings `global.cors`, `global.contentSecurityPolicy` and `global.diskQuota`, those it sets."""
    if "cors" in spec:
        plan_cors(planner, spec["cors"])
    if "contentSecurityPolicy" in spec:
        plan_csp(planner, spec["contentSecurityPolicy"])
    if "diskQuota" in spec:
        plan_disk_quota(planner, spec["diskQuota"])


def as_list(value) -> list:
    return value if isinstance(value, list) else [] if value in (None, "") else [value]


# --- CORS: an object in the global settings' metadata map

CORS_KEY, CORS_CLASS = "corsConfigurationSettings", "org.geoserver.security.cors.CORSConfiguration"
CORS_DEFAULTS = {"enabled": False, "allowedOriginPatterns": ["*"], "allowedMethods": [], "allowedHeaders": [], "maxAge": 3600,
                 "supportsCredentials": False}


def entries(settings: dict) -> list:
    found = ((settings.get("metadata") or {}).get("map") or {}).get("entry") or []
    return found if isinstance(found, list) else [found]


def live_cors(settings: dict) -> dict:
    """The CORS settings in the files' form: lists for origins, methods and headers."""
    entry = next((e for e in entries(settings) if isinstance(e, dict) and e.get("string") == CORS_KEY), None)
    value = (entry or {}).get(CORS_CLASS) or {}
    out = dict(CORS_DEFAULTS)
    for key in out:
        if key in value:
            v = value[key]
            if key == "allowedOriginPatterns":
                v = [o.strip() for o in str(v).split(",") if o.strip()]
            elif key in ("allowedMethods", "allowedHeaders"):
                v = as_list((v or {}).get("string") if isinstance(v, dict) else v)
            out[key] = v
    return out


def plan_cors(planner, wanted: dict) -> None:
    gs = planner.gs
    live = live_cors(((gs.get("settings") or {}).get("global") or {}).get("settings") or {})
    changed = [(f"global.cors.{k}", live[k], v) for k, v in wanted.items() if live[k] != v]
    if not changed:
        return

    def run():
        current = (gs.get("settings") or {}).get("global") or {}
        settings = current.get("settings") or {}
        value = {**live_cors(settings), **wanted}
        body = {"enabled": value["enabled"], "allowedOriginPatterns": ",".join(value["allowedOriginPatterns"]),
                "allowedMethods": {"string": value["allowedMethods"]}, "allowedHeaders": {"string": value["allowedHeaders"]},
                "maxAge": value["maxAge"], "supportsCredentials": value["supportsCredentials"]}
        kept = [e for e in entries(settings) if not (isinstance(e, dict) and e.get("string") == CORS_KEY)]
        settings["metadata"] = {"map": {"entry": [*kept, {"string": CORS_KEY, CORS_CLASS: body}]}}
        current.pop("updateSequence", None)
        gs.request("PUT", "settings", {"global": current})
        print("  note: GeoServer reads its CORS settings when it starts: restart it to use them")
    planner.add("~", "settings", "global (CORS)", changed, run=run)


# --- Content Security Policy: security/csp.xml

CSP_FILE = "resource/security/csp.xml"
CSP_FIELDS = ("enabled", "reportOnly", "allowOverride", "injectProxyBase", "remoteResources", "formAction", "frameAncestors")


def text_of(value) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def typed(text: str | None):
    return text == "true" if text in ("true", "false") else (text or "")


def csp_policies(policies: list) -> list:
    """Policies as compared: every field there, with GeoServer's defaults."""
    return [{"name": p["name"], "description": p.get("description", ""), "enabled": p.get("enabled", True),
             "rules": [{"name": r["name"], "description": r.get("description", ""), "enabled": r.get("enabled", True),
                        "filter": r.get("filter", ""), "directives": r.get("directives", "")} for r in p.get("rules") or []]}
            for p in policies]


def live_csp(xml: str) -> dict:
    root = ET.fromstring(xml)
    out = {k: typed(root.findtext(k)) for k in CSP_FIELDS}
    out["policies"] = csp_policies([
        {"name": p.findtext("name") or "", "description": p.findtext("description") or "", "enabled": typed(p.findtext("enabled")),
         "rules": [{k: typed(r.findtext(k)) if k == "enabled" else r.findtext(k) or ""
                    for k in ("name", "description", "enabled", "filter", "directives")} for r in p.findall("rule")]}
        for p in root.findall("policy")])
    return out


def set_csp(xml: str, wanted: dict) -> str:
    root = ET.fromstring(xml)
    for key in CSP_FIELDS:
        if key in wanted:
            element = root.find(key)
            if element is None:
                element = ET.SubElement(root, key)
            element.text = text_of(wanted[key])
    if "policies" in wanted:
        for p in root.findall("policy"):
            root.remove(p)
        for p in csp_policies(wanted["policies"]):
            policy = ET.SubElement(root, "policy")
            for key in ("name", "description", "enabled"):
                ET.SubElement(policy, key).text = text_of(p[key])
            for r in p["rules"]:
                rule = ET.SubElement(policy, "rule")
                for key in ("name", "description", "enabled", "filter", "directives"):
                    ET.SubElement(rule, key).text = text_of(r[key])
    return ET.tostring(root, encoding="unicode")


def plan_csp(planner, wanted: dict) -> None:
    gs = planner.gs
    r = gs.request("GET", CSP_FILE, ok=(200, 404))
    xml = r.text if r.status_code == 200 else "<config/>"
    live = live_csp(xml)
    compared = {**wanted, **({"policies": csp_policies(wanted["policies"])} if "policies" in wanted else {})}
    changed = [(f"global.contentSecurityPolicy.{k}", live.get(k), v) for k, v in compared.items() if live.get(k) != v]
    if not changed:
        return

    def run():
        now = gs.request("GET", CSP_FILE, ok=(200, 404))
        gs.request("PUT", CSP_FILE, set_csp(now.text if now.status_code == 200 else "<config/>", wanted),
                   content_type="application/xml")
    planner.add("~", "settings", "global (content security policy)", changed, run=run)


# --- GeoWebCache's disk quota

QUOTA_FILE = "resource/gwc/geowebcache-diskquota.xml"
QUOTA_FIELDS = ("enabled", "cacheCleanUpFrequency", "cacheCleanUpUnits", "maxConcurrentCleanUps", "globalExpirationPolicyName")
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}


def size(value) -> int:
    """Bytes from a number of bytes or a size such as '2 GiB'."""
    if isinstance(value, int):
        return value
    m = re.fullmatch(r"\s*([0-9.]+)\s*([KMGT]iB|B)\s*", str(value))
    if not m:
        raise ValueError(f"not a size: {value!r} (bytes, or a number with B, KiB, MiB, GiB or TiB)")
    return int(float(m.group(1)) * UNITS[m.group(2)])


def bytes_of(element) -> int | None:
    if element is None:
        return None
    if element.findtext("bytes") is not None:
        return int(element.findtext("bytes"))
    return size(f"{element.findtext('value')} {element.findtext('units')}")


def live_quota(rest_xml: str, file_xml: str | None) -> dict:
    root = ET.fromstring(rest_xml)
    out: dict = {k: typed(root.findtext(k)) for k in QUOTA_FIELDS}
    for k in ("cacheCleanUpFrequency", "maxConcurrentCleanUps"):
        out[k] = int(out[k]) if out[k] != "" else None
    out["globalQuota"] = bytes_of(root.find("globalQuota"))
    out["layerQuotas"] = sorted(
        ({"layer": q.findtext("layer"), "quota": bytes_of(q.find("quota")), "expirationPolicyName": q.findtext("expirationPolicyName")}
         for q in root.findall("layerQuotas/*")), key=lambda q: q["layer"] or "")
    store = root.findtext("quotaStore")
    if file_xml:
        store = ET.fromstring(file_xml).findtext("quotaStore") or store
    out["quotaStore"] = store
    return out


def wanted_quota(spec: dict) -> dict:
    out = {k: v for k, v in spec.items() if k not in ("globalQuota", "layerQuotas")}
    if "globalQuota" in spec:
        out["globalQuota"] = size(spec["globalQuota"])
    if "layerQuotas" in spec:
        out["layerQuotas"] = sorted(({"layer": q["layer"], "quota": size(q["quota"]),
                                      "expirationPolicyName": q.get("expirationPolicyName", "LFU")} for q in spec["layerQuotas"]),
                                    key=lambda q: q["layer"])
    return out


def quota_xml(value: dict, with_store: bool) -> str:
    """The disk quota as GeoWebCache reads it (its REST API and its file): sizes as a value and units."""
    quota = lambda b: f"<value>{int(b)}</value><units>B</units>"
    body = "".join(f"<{k}>{escape(text_of(value[k]))}</{k}>" for k in QUOTA_FIELDS if value.get(k) not in (None, ""))
    if value.get("globalQuota") is not None:
        body += f"<globalQuota>{quota(value['globalQuota'])}</globalQuota>"
    if with_store and value.get("quotaStore"):
        body += f"<quotaStore>{escape(value['quotaStore'])}</quotaStore>"
    body += "<layerQuotas>" + "".join(
        f"<LayerQuota><layer>{escape(q['layer'])}</layer><expirationPolicyName>{escape(q['expirationPolicyName'])}</expirationPolicyName>"
        f"<quota>{quota(q['quota'])}</quota></LayerQuota>" for q in value.get("layerQuotas") or []) + "</layerQuotas>"
    return f"<gwcQuotaConfiguration>{body}</gwcQuotaConfiguration>"


def read_quota(gs) -> dict:
    rest = gs.gwc_request("GET", "diskquota.xml", accept="application/xml")
    if rest.status_code != 200:
        raise GeoServerError("GET", "gwc/diskquota", rest.status_code, rest.text)
    file = gs.request("GET", QUOTA_FILE, ok=(200, 404))
    return live_quota(rest.text, file.text if file.status_code == 200 else None)


def plan_disk_quota(planner, spec: dict) -> None:
    gs = planner.gs
    live, wanted = read_quota(gs), wanted_quota(spec)
    changed = [(f"global.diskQuota.{k}", live.get(k), v) for k, v in wanted.items() if live.get(k) != v]
    if not changed:
        return

    def run():
        value = {**read_quota(gs), **wanted}
        r = gs.gwc_request("PUT", "diskquota.xml", quota_xml(value, with_store=False))  # now, in memory
        if r.status_code != 200:
            raise GeoServerError("PUT", "gwc/diskquota", r.status_code, r.text)
        gs.request("PUT", QUOTA_FILE, quota_xml(value, with_store=True), content_type="application/xml")  # after a restart
        if live.get("quotaStore") != value.get("quotaStore"):
            print("  note: GeoServer opens the disk quota store when it starts: restart it to use the new one")
    planner.add("~", "settings", "global (disk quota)", changed, run=run)


# --- import: what differs from a fresh standard GeoServer

QUOTA_DEFAULTS = {"enabled": False, "cacheCleanUpFrequency": 10, "cacheCleanUpUnits": "SECONDS", "maxConcurrentCleanUps": 2,
                  "globalExpirationPolicyName": "LFU", "globalQuota": 500 * 1024 ** 2, "quotaStore": None, "layerQuotas": []}


def readable_size(n: int) -> str | int:
    for unit in ("TiB", "GiB", "MiB", "KiB"):
        if n and n % UNITS[unit] == 0:
            return f"{n // UNITS[unit]} {unit}"
    return n


def import_settings(gs) -> dict:
    """`global` fields for CORS, the Content-Security-Policy and the disk quota, where they aren't a fresh GeoServer's."""
    from pathlib import Path

    if getattr(gs, "cloud", False):
        return {}
    out: dict = {}
    cors = live_cors(((gs.get("settings") or {}).get("global") or {}).get("settings") or {})
    cors = {k: v for k, v in cors.items() if v != CORS_DEFAULTS[k]}
    if cors:
        out["cors"] = cors
    r = gs.request("GET", CSP_FILE, ok=(200, 404))
    if r.status_code == 200:
        live, default = live_csp(r.text), live_csp((Path(__file__).parent / "csp_default.xml").read_text())
        csp = {k: v for k, v in live.items() if v != default[k]}
        if "policies" in csp:
            csp["policies"] = [{**p, "rules": [{k: v for k, v in rule.items() if v not in ("", True) or k == "name"} for rule in p["rules"]]}
                               for p in csp["policies"]]
            csp["policies"] = [{k: v for k, v in p.items() if v not in ("", True) or k == "name"} for p in csp["policies"]]
        if csp:
            out["contentSecurityPolicy"] = csp
    try:
        quota = {k: v for k, v in read_quota(gs).items() if v != QUOTA_DEFAULTS[k] and v is not None}
    except GeoServerError:  # none to read: GeoServer Cloud that wouldn't say which it is
        quota = {}
    if "globalQuota" in quota:
        quota["globalQuota"] = readable_size(quota["globalQuota"])
    if "layerQuotas" in quota:
        quota["layerQuotas"] = [{**q, "quota": readable_size(q["quota"])} for q in quota["layerQuotas"]]
    if quota:
        out["diskQuota"] = quota
    return out
