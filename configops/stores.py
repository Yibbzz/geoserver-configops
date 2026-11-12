"""Store resources, and the layers published from them.

A Store is data GeoServer reads where it is, rather than a table configops loads:
a Cloud-Optimised GeoTIFF over HTTP or object storage, a remote WMS, WMTS or WFS
to cascade, a PostGIS database reached with a password, or files on the shared
data volume (GeoPackage, Shapefile, FlatGeobuf, PMTiles, GeoTIFF, image mosaics). A Layer with `store:`
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
    # files on the shared data volume (values-data-volume.yaml), by path under it
    "geopackage": ("data", "GeoPackage"),
    "shapefile": ("data", "Shapefile"),
    "shapefiles": ("data", "Directory of spatial files (shapefiles)"),
    "flatgeobuf": ("data", "FlatGeobuf"),
    "pmtiles": ("data", "PMTiles"),
    "geotiff": ("coverage", "GeoTIFF"),
    "worldimage": ("coverage", "WorldImage"),
    "arcgrid": ("coverage", "ArcGrid"),
    "imagemosaic": ("coverage", "ImageMosaic"),
    "imagepyramid": ("coverage", "ImagePyramid"),
    "geoparquet": ("data", "GeoParquet"),
    "property": ("data", "Properties"),
    "csv": ("data", "CSV"),
    "appschema": ("data", "Application Schema DataAccess"),  # needs values-app-schema.yaml
    "sqlserver": ("data", "Microsoft SQL Server"),
}
DATA = "/data"  # where values-data-volume.yaml mounts the shared volume
FILE_PARAMS = {"geopackage": "database", "shapefile": "url", "shapefiles": "url", "flatgeobuf": "url", "pmtiles": "pmtiles",
               "geoparquet": "uri", "property": "directory", "csv": "file", "appschema": "url"}
DBTYPES = {"postgis": "postgis", "geopackage": "geopkg", "geoparquet": "geoparquet", "appschema": "app-schema", "sqlserver": "sqlserver"}
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
SECRET_PARAMS = {"postgis": ("user", "passwd"), "sqlserver": ("user", "passwd"), "wfs": (f"{WFS}USERNAME", f"{WFS}PASSWORD")}


def location(spec: dict) -> str:
    """Where a file store's data is: file:/data/<path>, or its url."""
    if spec.get("path"):
        return f"file:{DATA}/{spec['path'].strip('/')}"
    return spec["url"]


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


def secret_or_text(value, store: Resource, root) -> str:
    """`value`, `{ value }`, or `{ secret, key }` from a Secret."""
    if isinstance(value, str):
        return value
    if "value" in value:
        return value["value"]
    found = secrets.lookup(value["secret"], value["key"], root)
    if found is None:
        raise MissingCredentials(f"store {store.name}: no value in secret {value['secret']}, key {value['key']}")
    return found


def wanted_store(store: Resource, root, spec: dict | None = None) -> tuple[dict, dict]:
    """(the body GeoServer takes, the fields compared: name -> value)."""
    spec = spec or store.spec
    kind = spec["type"]
    fam, gs_type = TYPES[kind]
    body: dict = {"name": geoserver_name(store), "type": gs_type, "enabled": spec.get("enabled", True)}
    compared: dict = {"enabled": body["enabled"]}
    if "description" in spec:
        body["description"] = compared["description"] = spec["description"]
    if "disableOnConnectionFailure" in spec:
        body["disableOnConnFailure"] = compared["disableOnConnFailure"] = spec["disableOnConnectionFailure"]
    user, password = credentials(store, root)
    if fam == "coverage":
        body["url"] = compared["url"] = location(spec)
        if kind == "cog":
            reader = COG_READERS.get(urlparse(spec["url"]).scheme, "HTTP")
            body["metadata"] = {"entry": {"@key": "CogSettings.Key", "cogSettings": {"rangeReaderSettings": reader, "useCachingStream": False}}}
            compared["cog"] = reader
    elif fam in ("wms", "wmts"):
        body["capabilitiesURL"] = compared["capabilitiesURL"] = spec["url"]
        if user is not None:
            body["user"], body["password"] = user, password  # GeoServer's name: "username" is ignored
            compared["user"] = user
        for key in ("maxConnections", "readTimeout", "connectTimeout"):
            if key in spec:
                body[key] = compared[key] = spec[key]
        if "connectionPooling" in spec:
            body["metadata"] = {"entry": [{"@key": "useConnectionPooling", "$": str(spec["connectionPooling"]).lower()}]}
            compared["connectionPooling"] = spec["connectionPooling"]
        if "header" in spec:
            header = spec["header"]
            body["headerName"] = compared["headerName"] = header["name"]
            body["headerValue"] = compared["headerValue"] = secret_or_text(header, store, root)
        if "authKey" in spec:
            body["authKey"] = compared["authKey"] = secret_or_text(spec["authKey"], store, root)
    else:
        params = {str(k): str(v) for k, v in (spec.get("connection") or {}).items()}
        if kind == "wfs":
            params[f"{WFS}GET_CAPABILITIES_URL"] = spec["url"]
        if kind in DBTYPES:
            params.setdefault("dbtype", DBTYPES[kind])
        if kind in FILE_PARAMS:
            where = location(spec)
            params[FILE_PARAMS[kind]] = where.removeprefix("file:") if kind == "geopackage" else where
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
    out = {"enabled": live.get("enabled", True), "description": live.get("description"),
           "disableOnConnFailure": live.get("disableOnConnFailure", False)}
    if fam == "coverage":
        out["url"] = live.get("url")
        entries = (live.get("metadata") or {}).get("entry") or []
        for e in entries if isinstance(entries, list) else [entries]:
            if isinstance(e, dict) and e.get("@key") == "CogSettings.Key":
                out["cog"] = (e.get("cogSettings") or {}).get("rangeReaderSettings")
    elif fam in ("wms", "wmts"):
        out["capabilitiesURL"] = live.get("capabilitiesURL")
        out["user"] = live.get("user")
        for key in ("maxConnections", "readTimeout", "connectTimeout", "headerName", "headerValue", "authKey"):
            out[key] = live.get(key)
        entries = (live.get("metadata") or {}).get("entry") or []
        pooling = [e.get("$") for e in (entries if isinstance(entries, list) else [entries]) if isinstance(e, dict) and e.get("@key") == "useConnectionPooling"]
        out["connectionPooling"] = pooling[0] != "false" if pooling else True
    else:
        entries = (live.get("connectionParameters") or {}).get("entry") or []
        for e in entries if isinstance(entries, list) else [entries]:
            out[f"connection.{e['@key']}"] = e.get("$")
    return out


def plan(planner) -> None:
    """Stores, then the layers published from them."""
    gs, add = planner.gs, planner.add
    stores = planner.files.named("Store")
    datasets = planner.files.named("Dataset")
    for store in stores.values():
        ws = planner.workspace_of(store)
        path, collection = store_path(ws, store)
        spec = dict(store.spec)
        if spec.get("dataset"):  # a raster Dataset's file: loaded first if it never has been
            dataset = datasets[spec["dataset"]]
            spec["path"] = dataset.spec["raster"]["path"]
            first_raster(planner, dataset)
        body, wanted = wanted_store(store, planner.root, spec)
        body["workspace"] = {"name": ws}  # coverage stores won't be made without it
        key = family(store)["key"]
        target = f"{ws}:{geoserver_name(store)}"
        live = (gs.get(path) or {}).get(key)
        def run(path=path, collection=collection, key=key, body=body, spec=spec):
            if spec["type"] == "appschema":
                schema_cache(spec["path"])
            gs.upsert(path, collection, {key: body})
        if live is None:
            where = spec.get("url") or (f"{DATA}/{spec['path'].strip('/')}" if spec.get("path") else "") \
                or spec.get("connection", {}).get("host", "")
            add("+", "store", target, note=f"{store.spec['type']}: {where}", run=run)
            continue
        current = live_store(live, store.spec["type"])
        changed = [(k, current.get(k), v) for k, v in wanted.items() if current.get(k) != v]
        if changed:
            add("~", "store", target, changed, run=run)

    for layer in planner.layers.values():
        if "store" in layer.spec:
            plan_layer(planner, layer, stores[layer.spec["store"]])


def schema_cache(path: str) -> None:
    """App-schema downloads the schemas a mapping names into an app-schema-cache folder beside it, if there is one:
    GeoServer Cloud has no data directory for its own. Made when the data volume is mounted here."""
    from .raster import data_root

    folder = data_root() / path
    if folder.parent.is_dir():
        (folder.parent / "app-schema-cache").mkdir(exist_ok=True)


def first_raster(planner, dataset: Resource) -> None:
    """A raster Dataset that has never loaded is loaded before the store on its file is made."""
    from . import ingest

    path = dataset.spec["raster"]["path"]
    last = ingest.last_load(planner.conn, dataset.name) if planner.conn is not None else None
    if (last is None or last.tables != [path]) and dataset.name not in planner.synced:
        planner.synced.add(dataset.name)
        planner.add("+", "raster", f"{DATA}/{path}", note=f"first sync of dataset {dataset.name}",
                    run=lambda d=dataset.name: planner.first_sync(d))


def available(gs, ws: str, store: Resource) -> list[str]:
    fam = family(store)
    path, _ = store_path(ws, store)
    data = gs.get(f"{path}/{fam['items']}", params={"list": fam["list"]}) or {}
    items = (data.get("list") or {}).get("string") or []
    return items if isinstance(items, list) else [items]


# A cascaded WMS layer's options: spec name -> GeoServer's
REMOTE = {"style": "forcedRemoteStyle", "styles": "selectedRemoteStyles", "format": "preferredFormat", "formats": "selectedRemoteFormats",
          "minScale": "minScale", "maxScale": "maxScale", "respectBounds": "metadataBBoxRespected", "vendorParameters": "vendorParameters"}


def remote_options(spec: dict) -> dict:
    out = {}
    for key, value in spec.items():
        if key in ("styles", "formats"):
            value = sorted(value)
        elif key == "vendorParameters":
            value = {str(k): str(v).lower() if isinstance(v, bool) else str(v) for k, v in value.items()}
        out[key] = value
    return out


def live_remote(item: dict) -> dict:
    out = {}
    for key, field in REMOTE.items():
        value = item.get(field)
        if key in ("styles", "formats"):
            value = sorted(as_list((value or {}).get("string")))
        elif key == "vendorParameters":
            value = {e["@key"]: str(e.get("$", "")) for e in as_list((value or {}).get("entry")) if isinstance(e, dict)}
        elif key == "respectBounds":
            value = bool(value)
        out[key] = value
    return out


def remote_body(remote: dict) -> dict:
    out = {}
    for key, value in remote.items():
        if key in ("styles", "formats"):
            value = {"string": value}
        elif key == "vendorParameters":
            value = {"entry": [{"@key": k, "$": v} for k, v in value.items()]}
        out[REMOTE[key]] = value
    return out


def as_list(value) -> list:
    return [] if value in (None, "") else value if isinstance(value, list) else [value]


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
    remote = remote_options(spec.get("remote") or {})
    wanted_item += [(f"remote.{k}", v) for k, v in remote.items()]
    wanted_layer, settings = planner.layer_settings(layer, (item_path, fam["item"]))

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
        item.update(remote_body(remote))
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
               "resource": live_item.get(fam["native"]) or live_item.get("nativeName"),
               **{f"remote.{k}": v for k, v in live_remote(live_item).items()}, **planner.live_layer_settings(layer, live_layer, live_item)}
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
