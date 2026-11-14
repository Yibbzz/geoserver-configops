"""Authentication as code: PasswordPolicy, AuthFilter, AuthProvider, UserGroupService and
RoleService resources, and the security settings (`Settings` `global.security`): the active
role service, the authentication providers in order, filter chains, brute force prevention.

GeoServer Cloud has no REST endpoints for most of these, but GeoServer keeps each one as
an XML file (security/<folder>/<name>/config.xml, and security/config.xml), which /rest/resource
reads and writes. GeoServer reads them again whenever its own security API saves the
configuration, so after writing, apply saves an authentication provider unchanged.

The files use GeoServer's own field names (`config:`), because each kind of filter or
service has its own. Only the fields a file sets are compared. No object is ever removed; `filterChains`
is the whole list of chains, so a chain GeoServer has that the files leave out goes (plan names it).
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET

from .geoserver import GeoServerError

FOLDERS = {"PasswordPolicy": "pwpolicy", "AuthFilter": "filter", "AuthProvider": "auth",
           "UserGroupService": "usergroup", "RoleService": "role", "MasterPasswordProvider": "masterpw"}
KINDS = tuple(FOLDERS)
POLICY = {"uppercase": "uppercaseRequired", "lowercase": "lowercaseRequired", "digit": "digitRequired",
          "minLength": "minLength", "maxLength": "maxLength"}
SECURITY_FILE = "resource/security/config.xml"
# Filters GeoServer has by itself, which a filter chain may name without an AuthFilter
BUILT_IN_FILTERS = {"anonymous", "basic", "form", "rememberme", "gateway-shared-auth", "interceptor", "restInterceptor",
                    "exception", "contextAsc", "contextNoAsc", "formLogout", "roleFilter", "sslFilter"}
CHAIN_ATTRS = {"class": "class", "interceptor": "interceptorName", "exceptionTranslation": "exceptionTranslationName",
               "roleFilter": "roleFilterName", "disabled": "disabled", "allowSessionCreation": "allowSessionCreation",
               "ssl": "ssl", "matchHTTPMethod": "matchHTTPMethod"}
SETTINGS = {"roleService": "roleServiceName", "encryptUrlParams": "encryptingUrlParams",
            "passwordEncryption": "configPasswordEncrypterName"}


# --- XML <-> plain values

def text(value) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def to_value(e: ET.Element):
    """An element as the files write it: text, a list for <string>s, {class, value}, or a dict."""
    children = list(e)
    if e.get("class") and not children:
        return {"class": e.get("class"), "value": e.text or ""}
    if children and all(c.tag == "string" for c in children):
        return [c.text or "" for c in children]
    if children:
        return {c.tag: to_value(c) for c in children}
    return e.text or ""


def set_value(parent: ET.Element, key: str, value) -> None:
    e = parent.find(key)
    if e is None:
        e = ET.SubElement(parent, key)
    if isinstance(value, dict) and set(value) != {"class", "value"} and len(e):  # merged: the fields left out stay
        for k, v in value.items():
            set_value(e, k, v)
        return
    for child in list(e):
        e.remove(child)
    e.attrib.pop("class", None)
    e.text = None
    if isinstance(value, dict) and set(value) == {"class", "value"}:
        e.set("class", value["class"])
        e.text = text(value["value"])
    elif isinstance(value, dict):
        for k, v in value.items():
            set_value(e, k, v)
    elif isinstance(value, list):
        for v in value:
            ET.SubElement(e, "string").text = text(v)
    else:
        e.text = text(value)


def same(live, wanted) -> bool:
    if isinstance(wanted, dict) and set(wanted) != {"class", "value"}:
        return isinstance(live, dict) and all(same(live.get(k), v) for k, v in wanted.items())
    if isinstance(wanted, dict):
        return isinstance(live, dict) and live.get("class") == wanted["class"] and live.get("value") == text(wanted["value"])
    if isinstance(wanted, list):
        return isinstance(live, list) and live == [text(v) for v in wanted]
    return live == text(wanted)


# --- the objects

def wanted_fields(r) -> dict:
    spec = r.spec
    if r.kind == "PasswordPolicy":
        fields = {POLICY[k]: spec[k] for k in POLICY if k in spec}
        return {"className": "org.geoserver.security.validation.PasswordValidatorImpl", **fields}
    fields = dict(spec.get("config") or {})
    if spec.get("className"):
        fields["className"] = spec["className"]
    return fields


def root_tag(r) -> str:
    return "org.geoserver.security.validation.PasswordPolicyConfig" if r.kind == "PasswordPolicy" else r.spec["class"]


def reload_security(gs, seconds: float = 30, every: float = 2) -> None:
    """Have GeoServer read its security files again: save its default authentication provider as it is.
    Right after a master password change GeoServer may still be reloading by itself, and answers
    500 "Cannot save security configuration" until it's done: try again for a while."""
    url = f"{gs.rest}/security/authproviders/default"
    deadline = time.monotonic() + seconds
    while True:
        body = gs.session.get(url, headers={"Accept": "application/xml"}, timeout=60).text
        try:
            gs.request("PUT", "security/authproviders/default", body, content_type="application/xml")
            return
        except GeoServerError as e:
            if e.status != 500 or time.monotonic() + every > deadline:
                raise
        time.sleep(every)


def plan(planner) -> None:
    gs, add = planner.gs, planner.add
    for r in [r for kind in KINDS for r in planner.files.of_kind(kind)]:
        name = r.spec.get("name") or r.name
        path = f"resource/security/{FOLDERS[r.kind]}/{name}/config.xml"
        wanted = wanted_fields(r)
        response = gs.request("GET", path, ok=(200, 404))
        target = f"{r.kind} {name}"
        live_xml = response.text if response.status_code == 200 and response.text.lstrip().startswith("<") else None
        if live_xml is None:
            root = ET.Element(root_tag(r))
            ET.SubElement(root, "name").text = name
            changed = None
        else:
            root = ET.fromstring(live_xml)
            live = {c.tag: to_value(c) for c in root}
            changed = [(k, live.get(k), v) for k, v in wanted.items() if not same(live.get(k), v)]
            if not changed:
                continue
        for k, v in wanted.items():
            set_value(root, k, v)
        xml = ET.tostring(root, encoding="unicode")

        def run(path=path, xml=xml):
            gs.request("PUT", path, xml, content_type="application/xml")
            reload_security(gs)
        if changed is None:
            add("+", "security", target.lower(), note=r.spec.get("className") or r.kind, run=run)
        else:
            add("~", "security", target.lower(), changed, run=run)


# --- the security settings: security/config.xml

def chain_of(e: ET.Element) -> dict:
    out = {"name": e.get("name"), "paths": [p for p in (e.get("path") or "").split(",") if p],
           "filters": [f.text for f in e.findall("filter")]}
    for key, attr in CHAIN_ATTRS.items():
        if e.get(attr) is not None:
            out[key] = e.get(attr) == "true" if e.get(attr) in ("true", "false") else e.get(attr)
    if e.get("httpMethods"):
        out["httpMethods"] = e.get("httpMethods").split(",")
    return out


def chain_element(chain: dict) -> ET.Element:
    e = ET.Element("filters", {"name": chain["name"]})
    for key, attr in CHAIN_ATTRS.items():
        if key in chain:
            e.set(attr, text(chain[key]))
    e.set("path", ",".join(chain["paths"]))
    if chain.get("httpMethods"):
        e.set("httpMethods", ",".join(chain["httpMethods"]))
    for f in chain.get("filters", []):
        ET.SubElement(e, "filter").text = f
    return e


def live_security(xml: str) -> dict:
    root = ET.fromstring(xml)
    out = {key: (root.findtext(tag) or "") for key, tag in SETTINGS.items()}
    out["encryptUrlParams"] = out["encryptUrlParams"] == "true"
    out["authProviders"] = [s.text for s in root.findall("authProviderNames/string")]
    out["filterChains"] = [chain_of(e) for e in root.findall("filterChain/filters")]
    brute = root.find("bruteForcePrevention")
    out["bruteForcePrevention"] = to_value(brute) if brute is not None else {}
    remember = root.find("rememberMeService")
    out["rememberMe"] = to_value(remember) if remember is not None else {}
    return out


def compare_security(xml: str, wanted: dict) -> list[tuple[str, object, object]]:
    live = live_security(xml)
    out = []
    for key, value in wanted.items():
        here = live.get(key)
        if key == "filterChains":
            names, wanted_names = [c["name"] for c in here], [c["name"] for c in value]
            if names != wanted_names:
                out.append((key, names, wanted_names))
            gone = [n for n in names if n not in wanted_names]
            if gone:  # said outright: the names above are easy to read past
                out.append((f"{key} (removed)", gone, []))
            by_name = {c["name"]: c for c in here}
            for want in value:  # a chain both have, changed inside
                have = by_name.get(want["name"])
                compared = {k: have.get(k) for k in want} if have else None
                if compared is not None and compared != want:
                    out.append((f"{key}.{want['name']}", compared, want))
        elif key in ("bruteForcePrevention", "rememberMe"):
            if not same(here, value):
                out.append((key, here, value))
        elif here != value:
            out.append((key, here, value))
    return out


def set_security(xml: str, wanted: dict) -> str:
    root = ET.fromstring(xml)
    for key, tag in SETTINGS.items():
        if key in wanted:
            set_value(root, tag, wanted[key])
    if "authProviders" in wanted:
        set_value(root, "authProviderNames", wanted["authProviders"])
    if "bruteForcePrevention" in wanted:
        set_value(root, "bruteForcePrevention", wanted["bruteForcePrevention"])
    if "rememberMe" in wanted:
        set_value(root, "rememberMeService", wanted["rememberMe"])
    if "filterChains" in wanted:
        chains = root.find("filterChain")
        live = {e.get("name"): e for e in chains.findall("filters")} if chains is not None else {}
        if chains is None:
            chains = ET.SubElement(root, "filterChain")
        for e in list(chains):
            chains.remove(e)
        for chain in wanted["filterChains"]:
            old = live.get(chain["name"])
            merged = {**(chain_of(old) if old is not None else {}), **chain}  # attributes the files leave out stay
            chains.append(chain_element(merged))
    return ET.tostring(root, encoding="unicode")


def plan_settings(planner, wanted: dict) -> None:
    """`Settings` `global.security`, as one change to security/config.xml."""
    gs = planner.gs
    xml = gs.request("GET", SECURITY_FILE).text
    changed = compare_security(xml, wanted)
    if not changed:
        return

    def run():
        current = gs.request("GET", SECURITY_FILE).text
        gs.request("PUT", SECURITY_FILE, set_security(current, wanted), content_type="application/xml")
        try:
            reload_security(gs)
        except GeoServerError:
            gs.request("PUT", SECURITY_FILE, current, content_type="application/xml")  # put it back, so GeoServer still starts
            raise
    planner.add("~", "security settings", "global", [("global.security." + k, a, b) for k, a, b in changed], run=run)
