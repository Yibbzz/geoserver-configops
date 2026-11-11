"""Store resources, and the layers published from them.

A Store is data GeoServer reads where it is, rather than a table gisdata loads:
a Cloud-Optimised GeoTIFF over HTTP or object storage, a remote WMS, WMTS or WFS
to cascade, or a PostGIS database reached with a password. A Layer with `store:`
publishes one thing from it: a coverage, a remote layer or a feature type.

Passwords come from the Store's credentials Secret (keys username and password).
GeoServer keeps them encrypted, so they're set but never compared.
"""

from __future__ import annotations

from urllib.parse import urlparse

from . import secrets
from .geoserver import MissingCredentials, q
from .loader import Resource
from .validate import geoserver_name

# Store type -> (family, GeoServer's type)
TYPES = {
    "cog": ("coverage", "GeoTIFF"),
    "wms": ("wms", "WMS"),
    "wmts": ("wmts", "WMTS"),
    "wfs": ("data", "Web Feature Server (NG)"),
    "postgis": ("data", "PostGIS"),
}
FAMILIES = {
    "coverage": {"stores": "coveragestores", "key": "coverageStore", "items": "coverages", "item": "coverage",
                 "native": "nativeCoverageName", "list": "all"},
    "data": {"stores": "datastores", "key": "dataStore", "items": "featuretypes", "item": "featureType",
             "native": "nativeName", "list": "available"},
    "wms": {"stores": "wmsstores", "key": "wmsStore", "items": "wmslayers", "item": "wmsLayer",
            "native": "nativeName", "list": "available"},
    "wmts": {"stores": "wmtsstores", "key": "wmtsStore", "items": "layers", "item": "wmtsLayer",
             "native": "nativeName", "list": "available"},
}
COG_READERS = {"http": "HTTP", "https": "HTTP", "s3": "S3", "gs": "GS", "az": "AZURE", "azure": "AZURE"}
WFS = "WFSDataStoreFactory:"
SECRET_PARAMS = {"postgis": ("user", "passwd"), "wfs": (f"{WFS}USERNAME", f"{WFS}PASSWORD")}


def family(store: Resource) -> dict:
    return FAMILIES[TYPES[store.spec["type"]][0]]


def store_path(ws: str, store: Resource) -> tuple[str, str]:
    """(REST path of the store, of its collection)."""
    base = f"workspaces/{q(ws)}/{family(store)['stores']}"
    return f"{base}/{q(geoserver_name(store))}", base


def credentials(store: Resource, root) -> tuple[str | None, str | None]:
    ref = (store.spec.get("credentials") or {}).get("secret")
    if not ref:
        return None, None
    user, password = secrets.lookup(ref, "username", root), secrets.lookup(ref, "password", root)
    if user is None or password is None:
        raise MissingCredentials(f"store {store.name}: secret {ref} needs keys username and password")
    return user, password


def wanted_store(store: Resource, root) -> tuple[dict, dict]:
    """(the body GeoServer takes, the fields compared: name -> value)."""
    spec, kind = store.spec, store.spec["type"]
    fam, gs_type = TYPES[kind]
    body: dict = {"name": geoserver_name(store), "type": gs_type, "enabled": spec.get("enabled", True)}
    compared: dict = {"enabled": body["enabled"]}
    if "description" in spec:
        body["description"] = compared["description"] = spec["description"]
    user, password = credentials(store, root)
    if fam == "coverage":
        body["url"] = compared["url"] = spec["url"]
        reader = COG_READERS.get(urlparse(spec["url"]).scheme, "HTTP")
        body["metadata"] = {"entry": {"@key": "CogSettings.Key", "cogSettings": {"rangeReaderSettings": reader, "useCachingStream": False}}}
        compared["cog"] = reader
    elif fam in ("wms", "wmts"):
        body["capabilitiesURL"] = compared["capabilitiesURL"] = spec["url"]
        if user is not None:
            body["username"], body["password"] = user, password
            compared["username"] = user
    else:
        params = {str(k): str(v) for k, v in (spec.get("connection") or {}).items()}
        if kind == "wfs":
            params[f"{WFS}GET_CAPABILITIES_URL"] = spec["url"]
        if kind == "postgis":
            params.setdefault("dbtype", "postgis")
        compared.update({f"connection.{k}": v for k, v in params.items()})
        if user is not None:
            user_key, password_key = SECRET_PARAMS[kind]
            params[user_key], params[password_key] = user, password
            compared[f"connection.{user_key}"] = user
        body["connectionParameters"] = {"entry": [{"@key": k, "$": v} for k, v in params.items()]}
    return body, compared


def live_store(live: dict, kind: str) -> dict:
    """A live store's compared fields, in wanted_store's names."""
    fam = TYPES[kind][0]
    out = {"enabled": live.get("enabled", True), "description": live.get("description")}
    if fam == "coverage":
        out["url"] = live.get("url")
        entries = (live.get("metadata") or {}).get("entry") or []
        for e in entries if isinstance(entries, list) else [entries]:
            if isinstance(e, dict) and e.get("@key") == "CogSettings.Key":
                out["cog"] = (e.get("cogSettings") or {}).get("rangeReaderSettings")
    elif fam in ("wms", "wmts"):
        out["capabilitiesURL"] = live.get("capabilitiesURL")
        out["username"] = live.get("username")
    else:
        entries = (live.get("connectionParameters") or {}).get("entry") or []
        for e in entries if isinstance(entries, list) else [entries]:
            out[f"connection.{e['@key']}"] = e.get("$")
    return out


def plan(planner) -> None:
    """Stores, then the layers published from them."""
    gs, add = planner.gs, planner.add
    stores = planner.files.named("Store")
    for store in stores.values():
        ws = planner.workspace_of(store)
        path, collection = store_path(ws, store)
        body, wanted = wanted_store(store, planner.root)
        body["workspace"] = {"name": ws}  # coverage stores won't be made without it
        key = family(store)["key"]
        target = f"{ws}:{geoserver_name(store)}"
        live = (gs.get(path) or {}).get(key)
        run = lambda path=path, collection=collection, key=key, body=body: gs.upsert(path, collection, {key: body})
        if live is None:
            add("+", "store", target, note=f"{store.spec['type']}: {store.spec.get('url') or store.spec.get('connection', {}).get('host', '')}", run=run)
            continue
        current = live_store(live, store.spec["type"])
        changed = [(k, current.get(k), v) for k, v in wanted.items() if current.get(k) != v]
        if changed:
            add("~", "store", target, changed, run=run)

    for layer in planner.layers.values():
        if "store" in layer.spec:
            plan_layer(planner, layer, stores[layer.spec["store"]])


def available(gs, ws: str, store: Resource) -> list[str]:
    fam = family(store)
    path, _ = store_path(ws, store)
    data = gs.get(f"{path}/{fam['items']}", params={"list": fam["list"]}) or {}
    items = (data.get("list") or {}).get("string") or []
    return items if isinstance(items, list) else [items]


def plan_layer(planner, layer: Resource, store: Resource) -> None:
    gs = planner.gs
    spec, fam = layer.spec, family(store)
    ws, name = planner.workspace_of(layer), geoserver_name(layer)
    store_ws = planner.workspace_of(store)
    target = f"{ws}:{name}"
    path, _ = store_path(store_ws, store)
    item_path = f"{path}/{fam['items']}/{q(name)}"
    title = spec.get("title") or name
    native = spec.get("resource")

    live_layer = gs.layer(ws, name)
    live_item = ((gs.get(item_path) or {}).get(fam["item"]) or {}) if live_layer else {}
    if native is None:
        native = live_item.get(fam["native"]) or live_item.get("nativeName")
    if native is None:
        if gs.get(path) is None:
            options = []  # the store is made first, in this same apply
        else:
            options = available(gs, store_ws, store)
            if len(options) != 1:
                which = ", ".join(options[:10]) or "nothing"
                planner.add("!", "layer", target, note=f"store {store.name} offers {which}: say which with spec.resource")
                planner.unpublished.add(layer.name)
                return
        native = options[0] if options else None

    wanted_item = [("title", title), ("enabled", spec.get("enabled", True)), ("advertised", spec.get("advertised", True))]
    if "abstract" in spec:
        wanted_item.append(("abstract", spec["abstract"]))
    if spec.get("resource"):
        wanted_item.append(("resource", spec["resource"]))
    wanted_layer, settings = planner.layer_settings(layer)

    def run(old_store=None):
        if old_store is not None:
            gs.delete(f"workspaces/{q(ws)}/layers/{q(name)}", {"recurse": "true"})
        item = {"name": name, "title": title, "enabled": spec.get("enabled", True), "advertised": spec.get("advertised", True)}
        if "abstract" in spec:
            item["abstract"] = spec["abstract"]
        if "keywords" in spec:
            item["keywords"] = {"string": spec["keywords"]}
        resource = native or available(gs, store_ws, store)[0]
        item[fam["native"]] = resource
        if fam["native"] != "nativeName":
            item["nativeName"] = resource
        gs.upsert(item_path, f"{path}/{fam['items']}", {fam["item"]: item})
        settings()

    if live_layer is None:
        planner.add("+", "layer", target, note=f"{store.spec['type']} store {store.name}" + (f": {native}" if native else ""), run=run)
        return
    href = live_layer["layer"].get("resource", {}).get("href", "")
    if f"/{fam['stores']}/" in href:
        live_store_name = href.split(f"/{fam['stores']}/", 1)[1].split("/", 1)[0]
    else:  # GeoServer gives a WMTS layer no link: it's in this store if the store has it
        live_store_name = geoserver_name(store) if live_item else None
    current = {"title": live_item.get("title"), "enabled": live_item.get("enabled", True),
               "advertised": live_item.get("advertised", True), "abstract": live_item.get("abstract"),
               "resource": live_item.get(fam["native"]) or live_item.get("nativeName"), **planner.live_layer_settings(layer, live_layer)}
    changed = [(k, current[k], v) for k, v in wanted_item + wanted_layer if current[k] != v]
    live_keywords = (live_item.get("keywords") or {}).get("string")
    live_keywords = live_keywords if isinstance(live_keywords, list) else ([live_keywords] if live_keywords else [])
    if "keywords" in spec and live_keywords != spec["keywords"]:
        changed.append(("keywords", live_keywords, spec["keywords"]))
    if live_store_name != geoserver_name(store):
        changed.append(("store", live_store_name, geoserver_name(store)))
    if changed:
        old = live_store_name if live_store_name != geoserver_name(store) else None
        planner.add("~", "layer", target, changed, run=lambda: run(old_store=old))
