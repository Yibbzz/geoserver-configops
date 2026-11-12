"""`configops import --settings` and `--security`: write the files for GeoServer's settings,
services and security, so `plan` on them has nothing to do.

Only what differs from a fresh GeoServer Cloud is written. Fields outside the spec's
schema aren't imported. Passwords can't be read back, so each imported user's comes
from a Secret the import names, and it lists the keys to fill in.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .importer import resource, resource_name
from .security import ACCESS, BUILT_IN_ROLES, BUILT_IN_USERS, DEFAULT_RULES, read_rest_rules, roles_list
from .server import SERVICES, URL_CHECKS_FILE, service_view, unwrap
from .validate import schema

USERS_SECRET = "geoserver-users"  # the Secret imported users' passwords come from
SECRET_KEY = re.compile(r"[^-._a-zA-Z0-9]+")

# A fresh GeoServer Cloud (3.0), read from its REST API: fields equal to these aren't written
DEFAULTS = {
    "settings": {"useHeadersProxyURL": False, "numDecimals": 4, "charset": "UTF-8", "verbose": True, "verboseExceptions": False,
                 "localWorkspaceIncludesPrefix": False, "showCreatedTimeColumnsInAdminList": False,
                 "showModifiedTimeColumnsInAdminList": False, "showModifiedUserAdminList": False},
    "global": {"globalServices": True, "resourceErrorHandling": "SKIP_MISCONFIGURED_LAYERS", "webUIMode": "DEFAULT",
               "xmlPostRequestLogBufferSize": 1024, "featureTypeCacheSize": 0, "trailingSlashMatch": True,
               "allowStoredQueriesPerWorkspace": True},
    "catalogMode": "hide",
    "jai": {"allowInterpolation": False, "recycling": False, "tilePriority": 5, "tileThreads": 7, "memoryCapacity": 0.5,
            "memoryThreshold": 0.75, "imageIOCache": False, "pngEncoderType": "PNGJ"},
    "coverageAccess": {"maxPoolSize": 5, "corePoolSize": 5, "keepAliveTime": 30000, "queueType": "UNBOUNDED",
                       "imageIOCacheThreshold": 10240, "granuleCacheMaxSizeMB": 128, "granuleCacheThresholdKB": 1024},
    "tileCaching": {"directWMSIntegrationEnabled": "false", "requireTiledParameter": "true", "WMSCEnabled": "true",
                    "TMSEnabled": "true", "securityEnabled": "false", "cacheLayersByDefault": "true", "cacheNonDefaultStyles": "true",
                    "metaTilingX": "4", "metaTilingY": "4", "gutter": "0", "defaultCachingGridSetIds": ["EPSG:4326", "EPSG:900913"],
                    "defaultCoverageCacheFormats": ["image/jpeg"], "defaultVectorCacheFormats": ["image/png"],
                    "defaultOtherCacheFormats": ["image/png", "image/jpeg"]},
    "service": {"enabled": True, "citeCompliant": False, "schemaBaseURL": "http://schemas.opengis.net", "verbose": False},
    "wms": {"bboxForEachCRS": False, "interpolation": "Nearest", "watermark": {"enabled": False, "position": "BOT_RIGHT", "transparency": 100},
            "maxBuffer": 0, "maxRequestMemory": 0, "maxRenderingTime": 0, "maxRenderingErrors": 0, "maxRequestedDimensionValues": 100,
            "getFeatureInfoMimeTypeCheckingEnabled": False, "getMapMimeTypeCheckingEnabled": False, "dynamicStylingDisabled": False,
            "featuresReprojectionDisabled": False, "defaultGroupStyleEnabled": True, "transformFeatureInfoDisabled": False,
            "autoEscapeTemplateValues": False, "cacheConfiguration": {"enabled": False, "maxEntries": 1000, "maxEntrySize": 51200},
            "remoteStyleMaxRequestTime": 60000, "remoteStyleTimeout": 30000},
    "wfs": {"serviceLevel": "COMPLETE", "maxFeatures": 1000000, "featureBounding": True, "canonicalSchemaLocation": False,
            "encodeFeatureMember": False, "hitsIgnoreMaxFeatures": False, "includeWFSRequestDumpFile": True,
            "disableStoredQueriesManagement": False, "allowGlobalQueries": True, "simpleConversionEnabled": False,
            "getFeatureOutputTypeCheckingEnabled": False, "metadata": {"maxNumberOfFeaturesForPreview": 50},
            "gml": {"1.0": {"srsNameStyle": "XML", "overrideGMLAttributes": True}, "1.1": {"srsNameStyle": "URN", "overrideGMLAttributes": False},
                    "2.0": {"srsNameStyle": "URN2", "overrideGMLAttributes": False}}},
    "wcs": {"gmlPrefixing": False, "latLon": False, "subsamplingEnabled": True, "maxInputMemory": -1, "maxOutputMemory": -1,
            "defaultDeflateCompressionLevel": 9, "maxRequestedDimensionValues": 100, "overviewPolicy": "IGNORE"},
    "wmts": {},
}
DEFAULT_RULE_ROLES = {"*.*.r": ["*"], "*.*.w": ["ADMIN", "GROUP_ADMIN"], "wfs.CreateStoredQuery": ["ADMIN", "GROUP_ADMIN"],
                      "wfs.DropStoredQuery": ["ADMIN", "GROUP_ADMIN"], "/**:GET,HEAD": ["ADMIN"], "/**:POST,DELETE,PUT": ["ADMIN"]}


def differing(live: dict, props: dict, defaults: dict) -> dict:
    """The schema's fields (`props`) whose live value is set and isn't the default, in the spec's form."""
    out = {}
    for name, prop in props.items():
        if name in ("geoserver", "workspace", "global", "contact", "catalogMode", "masterPassword", "tileCaching", "service", "security"):
            continue
        value = unwrap(live.get("abstrct" if name == "abstract" else name))
        if value in (None, "", []):
            continue
        if prop.get("type") == "object" and isinstance(value, dict):
            known = prop.get("properties")  # none: any key (gml's versions)
            nested = {k: v for k, v in value.items() if (known is None or k in known) and v not in (None, "")}
            wanted = {k: v for k, v in nested.items() if (defaults.get(name) or {}).get(k) != v}
            if wanted:
                out[name] = wanted
        elif defaults.get(name) != value:
            out[name] = value
    return out


def settings_resources(gs) -> tuple[list[dict], list[str]]:
    """The global Settings, each workspace's own Settings, and the four Services, where they differ from a fresh GeoServer."""
    defs = schema()["$defs"]
    props = defs["settings"]["properties"]
    found, notes = [], []

    live = (gs.get("settings") or {}).get("global") or {}
    spec = {}
    contact = {k: v for k, v in ((gs.get("settings/contact") or {}).get("contact") or {}).items()
               if k in props["contact"]["properties"] and v not in (None, "")}
    if contact:
        spec["contact"] = contact
    spec.update(differing(live.get("settings") or {}, props, DEFAULTS["settings"]))
    top = differing(live, props["global"]["properties"], DEFAULTS["global"])
    mode = ((gs.get("security/acl/catalog") or {}).get("mode") or "").lower()
    if mode and mode != DEFAULTS["catalogMode"]:
        top["catalogMode"] = mode
    caching = tile_caching(gs, props["global"]["properties"]["tileCaching"]["properties"])
    if caching:
        top["tileCaching"] = caching
    logging = request_logging(live)
    if logging:
        top["requestLogging"] = logging
    checks = gs.request("GET", URL_CHECKS_FILE, ok=(200, 404))
    if checks.status_code == 200 and ET.fromstring(checks.text).findtext("enabled") == "false":
        top["urlChecks"] = False
    security = security_settings(gs)
    if security:
        top["security"] = security
    if top:
        spec["global"] = top
    if spec:
        found.append(resource("Settings", "global", spec))

    for ws in gs.names("workspaces", "workspace"):
        own = (gs.get(f"workspaces/{ws}/settings") or {}).get("settings") or {}
        if "workspace" not in own:
            continue  # no settings of its own
        spec = {"workspace": ws}
        contact = {k: v for k, v in (own.get("contact") or {}).items() if k in props["contact"]["properties"] and v not in (None, "")}
        if contact:
            spec["contact"] = contact
        spec.update(differing(own, props, DEFAULTS["settings"]))
        found.append(resource("Settings", f"{resource_name(ws)[0]}-settings", spec))
        notes.append(f"workspace {ws} has settings of its own: written as Settings {resource_name(ws)[0]}-settings")

    workspaces = gs.names("workspaces", "workspace")
    for name in SERVICES:
        props = defs[f"service_{name}"]["properties"]
        live = (gs.get(f"services/{name}/settings") or {}).get(name) or {}
        live = {**live, **service_view(live)}
        spec = differing(live, props, {**DEFAULTS["service"], **DEFAULTS[name]})
        if spec:
            found.append(resource("Service", name, spec))
        for ws in workspaces:  # a workspace's own settings: what differs from the global ones, which they start from
            own = (gs.get(f"services/{name}/workspaces/{ws}/settings") or {}).get(name)
            if own is None:
                continue
            own = {**own, **service_view(own)}
            spec = {"service": name, "workspace": ws, **differing(own, props, {k: unwrap(v) for k, v in live.items()})}
            found.append(resource("Service", f"{resource_name(ws)[0]}-{name}", spec))
    return found, notes


def request_logging(live: dict) -> dict:
    """The request logging switches that differ from a fresh GeoServer's."""
    from .server import LOGGING_DEFAULTS, LOGGING_NAMES, metadata, typed

    return {LOGGING_NAMES[k]: typed(v) for k, v in metadata(live).items() if k in LOGGING_NAMES and v != LOGGING_DEFAULTS[k]}


def tile_caching(gs, props: dict) -> dict:
    """The tile caching defaults that differ from a fresh GeoServer's."""
    from .server import GWC_FILE, gwc_values

    r = gs.request("GET", GWC_FILE, ok=(200, 404))
    if r.status_code != 200:
        return {}
    out = {}
    for key, value in gwc_values(r.text).items():
        if key not in props or value == DEFAULTS["tileCaching"].get(key) or (value == "false" and key not in DEFAULTS["tileCaching"]):
            continue
        kind = props[key].get("type")
        out[key] = value == "true" if kind == "boolean" else int(value) if kind == "integer" else value
    return out


def names_of(data: dict | None, key: str) -> list:
    items = (data or {}).get(key) or []
    return items if isinstance(items, list) else [items]


def security_resources(gs) -> tuple[list[dict], list[str], list[str]]:
    """(resources, notes, Secret keys to fill in) for the roles, groups, users and access rules that aren't GeoServer's own."""
    found, notes, keys = [], [], []
    taken: set[tuple[str, str]] = set()

    def unique(kind: str, name: str) -> str:
        out, n = name, 2
        while (kind, out) in taken:
            out, n = f"{name[:60]}-{n}", n + 1
        taken.add((kind, out))
        return out

    roles = sorted(set(names_of(gs.get("security/roles"), "roles")) - BUILT_IN_ROLES)
    for role in roles:
        meta = unique("Role", resource_name(role.lower())[0])
        spec = {} if meta.upper().replace("-", "_") == role else {"name": role}
        found.append(resource("Role", meta, spec))

    for group in sorted(names_of(gs.get("security/usergroup/groups"), "groups")):
        meta, spelt = resource_name(group)
        meta = unique("Group", meta)
        spec = {"name": group} if (spelt or meta != group) else {}
        group_roles = sorted(names_of(gs.get(f"security/roles/group/{group}"), "roles"))
        if group_roles:
            spec["roles"] = group_roles
        found.append(resource("Group", meta, spec))

    for user in names_of(gs.get("security/usergroup/users"), "users"):
        name = user.get("userName")
        if name in BUILT_IN_USERS:
            continue
        meta, spelt = resource_name(name)
        meta = unique("User", meta)
        key = SECRET_KEY.sub("-", name)
        keys.append(key)
        spec = {"name": name} if (spelt or meta != name) else {}
        spec["password"] = {"secret": USERS_SECRET, "key": key}
        groups = sorted(names_of(gs.get(f"security/usergroup/user/{name}/groups"), "groups"))
        own = sorted(names_of(gs.get(f"security/roles/user/{name}"), "roles"))
        if groups:
            spec["groups"] = groups
        if own:
            spec["roles"] = own
        if user.get("enabled") is False:
            spec["enabled"] = False
        found.append(resource("User", meta, spec))

    access = {v: k for k, v in ACCESS.items()}
    rules: list[tuple[str, str, list[str]]] = []
    for kind in ("layers", "services"):
        rules += [(kind, key, roles_list(text)) for key, text in (gs.get(f"security/acl/{kind}") or {}).items()]
    rules += [("rest", key, roles) for key, roles in read_rest_rules(gs)]
    for kind, key, rule_roles in rules:
        if key in DEFAULT_RULES[kind] and DEFAULT_RULE_ROLES[key] == rule_roles:
            continue  # GeoServer's own, unchanged
        if kind == "layers":
            target, mode = key.rsplit(".", 1)
            ws, layer = target.split(".", 1)
            spec = {"layer": {"workspace": ws, "layer": layer, "access": access.get(mode, "read")}}
        elif kind == "services":
            service, operation = key.split(".", 1)
            spec = {"service": {"service": service, "operation": operation}}
        else:
            path, methods = key.rsplit(":", 1)
            spec = {"rest": {"path": path, "methods": methods.split(",")}}
        spec["roles"] = rule_roles
        found.append(resource("AccessRule", unique("AccessRule", resource_name(f"{kind}-{key}".lower())[0]), spec))
        missing = [r for r in rule_roles if r != "*" and r not in roles and r not in BUILT_IN_ROLES]
        if missing:
            notes.append(f"rule {kind} {key} names role(s) GeoServer doesn't have: {', '.join(missing)}; add Roles for them")
    found += auth_resources(gs, notes)
    from .urlchecks import live_checks

    for name, check in sorted(live_checks(gs).items()):
        meta = unique("URLCheck", resource_name(name)[0])
        spec = {"name": name} if meta != name else {}
        spec["regex"] = check["regex"]
        if check["description"]:
            spec["description"] = check["description"]
        if check["enabled"] is False:
            spec["enabled"] = False
        found.append(resource("URLCheck", meta, spec))
    return found, notes, keys


def security_defaults() -> dict:
    import json
    from pathlib import Path

    return json.loads((Path(__file__).parent / "security_defaults.json").read_text())


def auth_resources(gs, notes: list[str]) -> list[dict]:
    """Password policies, authentication filters and providers, user/group and role services that differ from a fresh GeoServer's."""
    import xml.etree.ElementTree as ET

    from .auth import FOLDERS, POLICY, to_value

    defaults = security_defaults()["objects"]
    out = []
    for kind, folder in FOLDERS.items():
        listing = gs.request("GET", f"resource/security/{folder}", params={"format": "xml"}, ok=(200, 404))
        if listing.status_code != 200:
            continue
        for name in [n.text for n in ET.fromstring(listing.text).findall(".//child/name")]:
            r = gs.request("GET", f"resource/security/{folder}/{name}/config.xml", ok=(200, 404))
            if r.status_code != 200 or not r.text.lstrip().startswith("<"):
                continue
            root = ET.fromstring(r.text)
            live = {c.tag: to_value(c) for c in root if c.tag not in ("id", "name")}
            fresh = defaults.get(f"{kind}/{name}")
            changed = {k: v for k, v in live.items() if fresh is None or fresh.get(k) != v}
            if not changed:
                continue
            meta = resource_name(name)[0]
            spec: dict = {"name": name} if meta != name else {}
            if kind == "PasswordPolicy":
                names = {v: k for k, v in POLICY.items()}
                for k, v in changed.items():
                    if k in names:
                        spec[names[k]] = v == "true" if v in ("true", "false") else int(v)
            else:
                if fresh is None:  # a new one: what it is, and all of its fields
                    spec["class"], spec["className"] = root.tag, live.pop("className", "")
                    changed = live
                if changed:
                    spec["config"] = {k: typed(v) for k, v in changed.items() if k != "className"}
            out.append(resource(kind, meta, spec))
            if kind != "PasswordPolicy" and fresh is None and root.tag[:1].islower():
                notes.append(f"{kind} {name}: GeoServer names its class {root.tag}; check spec.class before applying it elsewhere")
    return out


def typed(value):
    """A value from GeoServer's XML as YAML would write it: booleans and numbers as such."""
    if isinstance(value, str):
        if value in ("true", "false"):
            return value == "true"
        if value.lstrip("-").isdigit():
            return int(value)
    if isinstance(value, dict) and set(value) != {"class", "value"}:
        return {k: typed(v) for k, v in value.items()}
    return value


def security_settings(gs) -> dict:
    """The security settings (security/config.xml) that differ from a fresh GeoServer's."""
    from .auth import SECURITY_FILE, live_security

    r = gs.request("GET", SECURITY_FILE, ok=(200, 404))
    if r.status_code != 200 or not r.text.lstrip().startswith("<"):
        return {}
    live, fresh = live_security(r.text), security_defaults()["settings"]
    out = {k: v for k, v in live.items() if v != fresh.get(k)}
    if "bruteForcePrevention" in out:
        out["bruteForcePrevention"] = typed(out["bruteForcePrevention"])
    return out
