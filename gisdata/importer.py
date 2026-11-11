"""`gisdata import`: write the files that describe a live GeoServer workspace.

The way in for an existing install. Only what differs from the spec's
defaults is written (a title equal to the name, GeoServer's own keywords,
the default style for the geometry all drop out), so `gisdata plan` on the
result shows nothing to do. Workspace styles are saved under styles/.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from . import details, secrets
from .geoserver import GROUP_MODES, GeoServer, GeoServerError, MissingCredentials, for_project
from .loader import default_table, load
from .plan import CACHE_OPTIONS, DEFAULT_FORMATS, DEFAULT_GRIDSETS, PlanError, as_list, names_of
from .stores import DATA
from .validate import BUILT_IN_STYLES

API = "gisdata/v1alpha1"
RESOURCE_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SECRET = "geoserver-admin"  # the GeoServer secret a new project.yaml names
STYLE_SUFFIX = {"sld": ".sld", "css": ".css", "mbstyle": ".mbstyle"}
MODES = {v: k for k, v in GROUP_MODES.items()}
GEOMETRY_STYLE = {"Point": "point", "MultiPoint": "point", "LineString": "line", "MultiLineString": "line",
                  "Polygon": "polygon", "MultiPolygon": "polygon"}


FILE_DATASTORES = {"GeoPackage": ("geopackage", "database"), "Shapefile": ("shapefile", "url"),
                   "Directory of spatial files (shapefiles)": ("shapefiles", "url"), "FlatGeobuf": ("flatgeobuf", "url"),
                   "PMTiles": ("pmtiles", "pmtiles")}
FILE_COVERAGES = {"GeoTIFF": "geotiff", "WorldImage": "worldimage", "ArcGrid": "arcgrid", "ImageMosaic": "imagemosaic"}


def data_path(where: str) -> str | None:
    """A file store's path under the shared data volume, or None if it isn't there."""
    path = where.removeprefix("file:").removeprefix("//")
    return path[len(DATA) + 1:].strip("/") or None if path.startswith(f"{DATA}/") else None


def resource_name(gs_name: str) -> tuple[str, str | None]:
    """(metadata.name, spec.name or None): the GeoServer name as dashes, spelt out when that doesn't round trip."""
    candidate = re.sub(r"[^a-z0-9]+", "-", gs_name.lower()).strip("-")[:63] or "unnamed"
    if RESOURCE_NAME.match(candidate) and candidate.replace("-", "_") == gs_name:
        return candidate, None
    return candidate, gs_name


def default_style(ft: dict) -> str:
    """The style GeoServer gives a new layer, from its geometry."""
    for a in as_list((ft.get("attributes") or {}).get("attribute")):
        binding = a.get("binding", "")
        if binding.startswith("org.locationtech.jts.geom."):
            return GEOMETRY_STYLE.get(binding.rsplit(".", 1)[1], "generic")
    return "generic"


# GeoWebCache's own gridsets; any other a layer is cached in is written as a Gridset
BUILT_IN_GRIDSETS = re.compile(r"^(EPSG:(4326|900913|3857|4258)|GlobalCRS84(Pixel|Scale|Geometric)|GoogleCRS84Quad|"
                               r"WebMercatorQuad|WorldCRS84Quad|UPS(Arctic|Antarctic)WGS84Quad(x2)?|UTM\d\dWGS84Quad(x2)?)$")
GWC_DEFAULTS = {"metaWidthHeight": [4, 4], "gutter": 0, "expireCache": 0, "expireClients": 0, "blobStoreId": None}


def cache_setting(tile_layer: dict | None):
    if not tile_layer or not tile_layer.get("enabled", True):
        return None
    gridsets = sorted(g["gridSetName"] for g in as_list(tile_layer.get("gridSubsets")))
    formats = sorted(as_list(tile_layer.get("mimeFormats")))
    options = {key: tile_layer[gwc] for key, gwc in CACHE_OPTIONS.items()
               if tile_layer.get(gwc) is not None and tile_layer.get(gwc) != GWC_DEFAULTS[gwc]}
    if gridsets == sorted(DEFAULT_GRIDSETS) and formats == sorted(DEFAULT_FORMATS) and not options:
        return True
    return {"gridsets": gridsets, "formats": formats, **options}


def resource(kind: str, name: str, spec: dict) -> dict:
    return {"apiVersion": API, "kind": kind, "metadata": {"name": name}, "spec": spec}


class Importer:
    def __init__(self, gs: GeoServer, ws: str, project_ws: str | None, live_schema: str, dataset_tables: dict):
        self.gs, self.ws, self.project_ws, self.live_schema = gs, ws, project_ws, live_schema
        self.dataset_tables = dataset_tables  # table -> (dataset, key), from the project's Datasets
        self.notes: list[str] = []
        self.names: dict[str, str] = {}  # GeoServer name -> resource name, for group members
        self.styles: dict[str, tuple[str, str]] = {}  # style name -> (suffix, text)
        self.store_resources: dict[str, dict] = {}  # GeoServer store name -> Store resource, for stores that aren't the JNDI PostGIS
        self.secrets: list[str] = []  # Secrets the imported stores name
        self.global_styles: dict[str, tuple[str, str]] = {}  # global style name -> (suffix, text)
        self.legends: dict[str, dict] = {}  # workspace style name -> its legend graphic

    def style(self, ref: str | None) -> str | None:
        """A live style reference as the files write it, or None if it can't be."""
        if not ref:
            return None
        if ":" in ref:
            ws, name = ref.split(":", 1)
            if ws == self.ws:
                return name
            self.notes.append(f"style {ref} is in another workspace; left out")
            return None
        if ref in BUILT_IN_STYLES:
            return ref
        if not RESOURCE_NAME.match(ref):
            self.notes.append(f"global style {ref}: the files can only name it if it's lowercase letters, digits and dashes; left out")
            return None
        if ref not in self.global_styles:
            info = (self.gs.style(ref) or {}).get("style") or {}
            suffix = STYLE_SUFFIX.get(info.get("format", "sld"))
            if suffix is None:
                self.notes.append(f"global style {ref}: format {info.get('format')} isn't supported; left out")
                return None
            self.global_styles[ref] = (suffix, self.gs.style_body(ref))
        return ref

    def stores(self) -> dict[str, dict]:
        """The PostGIS stores on the JNDI datasource (by name: their parameters); other stores become Store resources."""
        found = {}
        for name in self.gs.names(f"workspaces/{self.ws}/datastores", "dataStore"):
            store = self.gs.datastore(self.ws, name)["dataStore"]
            entries = {e["@key"]: e.get("$") for e in as_list((store.get("connectionParameters") or {}).get("entry"))}
            if entries.get("jndiReferenceName"):
                found[name] = entries
                if entries.get("schema") and name != entries["schema"]:
                    self.notes.append(f"store {name}: apply moves its layers to a store named after their schema ({entries['schema']})")
            elif entries.get("dbtype") == "postgis":
                connection = {k: entries[k] for k in ("host", "port", "database", "schema") if entries.get(k)}
                secret = f"{resource_name(name)[0]}-credentials"
                self.secrets.append(secret)
                self.store_resource(store, name, {"type": "postgis", "connection": connection, "credentials": {"secret": secret}})
            elif entries.get("WFSDataStoreFactory:GET_CAPABILITIES_URL"):
                self.store_resource(store, name, {"type": "wfs", "url": entries["WFSDataStoreFactory:GET_CAPABILITIES_URL"]})
            elif store.get("type") in FILE_DATASTORES:
                kind, param = FILE_DATASTORES[store["type"]]
                where = entries.get(param) or ""
                path = data_path(where)
                if path:
                    self.store_resource(store, name, {"type": kind, "path": path})
                elif kind == "pmtiles" and where and not where.startswith("file:"):
                    self.store_resource(store, name, {"type": kind, "url": where})
                else:
                    self.notes.append(f"store {name}: {where} isn't on the shared data volume ({DATA}); left out, with its layers")
            else:
                self.notes.append(f"store {name} ({store.get('type')}) isn't a kind the files have yet; left out, with its layers")
        for name in self.gs.names(f"workspaces/{self.ws}/coveragestores", "coverageStore"):
            store = self.gs.get(f"workspaces/{self.ws}/coveragestores/{name}")["coverageStore"]
            cog = any(isinstance(e, dict) and e.get("@key") == "CogSettings.Key" for e in as_list((store.get("metadata") or {}).get("entry")))
            path = data_path(store.get("url") or "")
            if cog:
                self.store_resource(store, name, {"type": "cog", "url": store.get("url")})
            elif store.get("type") in FILE_COVERAGES and path:
                self.store_resource(store, name, {"type": FILE_COVERAGES[store["type"]], "path": path})
            else:
                self.notes.append(f"store {name} ({store.get('type')}) isn't a kind the files have yet; left out, with its layers")
        for kind, key in (("wmsstores", "wmsStore"), ("wmtsstores", "wmtsStore")):
            for name in self.gs.names(f"workspaces/{self.ws}/{kind}", key):
                store = self.gs.get(f"workspaces/{self.ws}/{kind}/{name}")[key]
                spec = {"type": kind[:-6], "url": store.get("capabilitiesURL")}
                if store.get("username"):
                    secret = f"{resource_name(name)[0]}-credentials"
                    self.secrets.append(secret)
                    spec["credentials"] = {"secret": secret}
                self.store_resource(store, name, spec)
        return found

    def store_resource(self, live: dict, gs_name: str, spec: dict) -> None:
        name, explicit = resource_name(gs_name)
        if explicit:
            spec["name"] = explicit
        if self.ws != self.project_ws:
            spec["workspace"] = self.ws
        if live.get("description"):
            spec["description"] = live["description"]
        if live.get("enabled") is False:
            spec["enabled"] = False
        self.store_resources[gs_name] = resource("Store", name, spec)

    def store_of_layer(self, gs_name: str, href: str) -> tuple[str | None, dict]:
        """(the store a layer is published from, its resource), for layers on Store resources."""
        for kind, item in (("datastores", "featuretypes"), ("coveragestores", "coverages"), ("wmsstores", "wmslayers")):
            if f"/{kind}/" in href:
                store = href.split(f"/{kind}/", 1)[1].split("/", 1)[0]
                if store in self.store_resources:
                    return store, next(iter(self.gs.get(f"workspaces/{self.ws}/{kind}/{store}/{item}/{gs_name}").values()))
                return None, {}
        for store, r in self.store_resources.items():  # a WMTS layer's resource has no link
            if r["spec"]["type"] == "wmts":
                found = self.gs.get(f"workspaces/{self.ws}/wmtsstores/{store}/layers/{gs_name}")
                if found:
                    return store, next(iter(found.values()))
        return None, {}

    def sql_layer(self, gs_name: str, layer: dict, ft: dict, schema: str, view: dict) -> dict:
        """A SQL view, as a Layer with sql:."""
        name, explicit = resource_name(gs_name)
        self.names[gs_name] = name
        spec: dict = {"name": explicit} if explicit else {}
        if self.ws != self.project_ws:
            spec["workspace"] = self.ws
        sql: dict = {"schema": schema, "query": str(view.get("sql", "")).strip()}
        if view.get("keyColumn"):
            sql["key"] = view["keyColumn"]
        geometry = view.get("geometry")
        if geometry:
            sql["geometry"] = {"column": geometry["name"], "type": geometry.get("type", "Geometry"), "crs": f"EPSG:{geometry.get('srid', 4326)}"}
        spec["sql"] = sql
        if ft.get("title") and ft["title"] != gs_name:
            spec["title"] = ft["title"]
        if ft.get("abstract"):
            spec["abstract"] = ft["abstract"]
        style = self.style((layer.get("defaultStyle") or {}).get("name"))
        if style and style not in BUILT_IN_STYLES:
            spec["style"] = style
        spec.update(details.to_spec(ft, layer))
        return resource("Layer", name, spec)

    def store_layer(self, gs_name: str, layer: dict, store: str, item: dict) -> dict:
        name, explicit = resource_name(gs_name)
        self.names[gs_name] = name
        spec: dict = {}
        if explicit:
            spec["name"] = explicit
        if self.ws != self.project_ws:
            spec["workspace"] = self.ws
        spec["store"] = self.store_resources[store]["metadata"]["name"]
        spec["resource"] = item.get("nativeCoverageName") or item.get("nativeName")
        if item.get("title") and item["title"] != gs_name:
            spec["title"] = item["title"]
        if item.get("abstract"):
            spec["abstract"] = item["abstract"]
        keywords = names_of((item.get("keywords") or {}).get("string"))
        if keywords and not (len(keywords) == 2 and "features" in keywords):  # GeoServer's own: features and the name
            spec["keywords"] = keywords
        style = self.style((layer.get("defaultStyle") or {}).get("name"))
        if style and style not in BUILT_IN_STYLES:
            spec["style"] = style
        extra = [s for s in (self.style(n) for n in names_of((layer.get("styles") or {}).get("style"))) if s]
        if extra:
            spec["styles"] = extra
        for flag, live in (("enabled", item.get("enabled", True)), ("advertised", item.get("advertised", True)),
                           ("queryable", layer.get("queryable", True))):
            if live is False:
                spec[flag] = False
        title = (layer.get("attribution") or {}).get("title")
        if title:
            spec["attribution"] = title
        cache = cache_setting(self.gs.tile_layer(f"{self.ws}:{gs_name}"))
        if cache:
            spec["cache"] = cache
        spec.update(details.to_spec(item, layer))
        return resource("Layer", name, spec)

    def layers(self, stores: dict[str, dict]) -> list[dict]:
        out = []
        for gs_name in self.gs.names(f"workspaces/{self.ws}/layers", "layer"):
            layer = self.gs.layer(self.ws, gs_name)["layer"]
            href = (layer.get("resource") or {}).get("href", "")
            on_store, item = self.store_of_layer(gs_name, href)
            if on_store:
                out.append(self.store_layer(gs_name, layer, on_store, item))
                continue
            if "/datastores/" not in href:
                self.notes.append(f"layer {gs_name} is on a store the files don't have yet; left out")
                continue
            store = href.split("/datastores/", 1)[1].split("/", 1)[0]
            if store not in stores:
                continue  # its store was left out, with a note
            ft = self.gs.featuretype(self.ws, store, gs_name)["featureType"]
            schema, table = stores.get(store, {}).get("schema") or "public", ft["nativeName"]
            view = next((e.get("virtualTable") for e in as_list((ft.get("metadata") or {}).get("entry"))
                         if isinstance(e, dict) and e.get("@key") == "JDBC_VIRTUAL_TABLE"), None)
            if view:
                out.append(self.sql_layer(gs_name, layer, ft, schema, view))
                continue
            if not TABLE_NAME.match(table) or not TABLE_NAME.match(schema):
                self.notes.append(f"layer {gs_name}: table {schema}.{table} isn't a name the files allow (lowercase); left out")
                continue
            name, explicit = resource_name(gs_name)
            self.names[gs_name] = name
            spec: dict = {}
            if explicit:
                spec["name"] = explicit
            if self.ws != self.project_ws:
                spec["workspace"] = self.ws
            linked = self.dataset_tables.get(table) if schema == self.live_schema else None
            if linked:
                spec["dataset"], spec["table"] = linked
            else:
                spec["table"] = {"schema": schema, "name": table}
            if ft.get("title") and ft["title"] != gs_name:
                spec["title"] = ft["title"]
            if ft.get("abstract"):
                spec["abstract"] = ft["abstract"]
            keywords = names_of((ft.get("keywords") or {}).get("string"))
            if keywords and sorted(keywords) != sorted(["features", table]):
                spec["keywords"] = keywords
            style = self.style((layer.get("defaultStyle") or {}).get("name"))
            if style and style != default_style(ft):
                spec["style"] = style
            extra = [s for s in (self.style(n) for n in names_of((layer.get("styles") or {}).get("style"))) if s]
            if extra:
                spec["styles"] = extra
            for flag, live in (("enabled", ft.get("enabled", True)), ("advertised", ft.get("advertised", True)),
                               ("queryable", layer.get("queryable", True))):
                if live is False:
                    spec[flag] = False
            title = (layer.get("attribution") or {}).get("title")
            if title:
                spec["attribution"] = title
            cache = cache_setting(self.gs.tile_layer(f"{self.ws}:{gs_name}"))
            if cache:
                spec["cache"] = cache
            native = (ft.get("nativeCRS") or {}).get("$") if isinstance(ft.get("nativeCRS"), dict) else None
            if ft.get("projectionPolicy") == "FORCE_DECLARED" and ft.get("srs") and ft["srs"] not in (native or ""):
                spec["crs"] = ft["srs"]
            spec.update(details.to_spec(ft, layer))
            out.append(resource("Layer", name, spec))
        return out

    def groups(self) -> list[dict]:
        found = []
        names = self.gs.names(f"workspaces/{self.ws}/layergroups", "layerGroup")
        for gs_name in names:
            self.names[gs_name] = resource_name(gs_name)[0]
        for gs_name in names:
            g = self.gs.layergroup(gs_name, self.ws)["layerGroup"]
            name, explicit = resource_name(gs_name)
            spec: dict = {}
            if explicit:
                spec["name"] = explicit
            if self.ws != self.project_ws:
                spec["workspace"] = self.ws
            if g.get("title") and g["title"] != gs_name:
                spec["title"] = g["title"]
            if g.get("abstractTxt"):
                spec["abstract"] = g["abstractTxt"]
            mode = MODES.get(g.get("mode", "SINGLE"))
            if mode is None:
                self.notes.append(f"group {gs_name}: mode {g.get('mode')} has no equivalent in the files; left out")
                continue
            if mode != "single":
                spec["mode"] = mode
            styles = [s.get("name") if isinstance(s, dict) else None for s in as_list((g.get("styles") or {}).get("style"))]
            members = []
            for i, member in enumerate(names_of((g.get("publishables") or {}).get("published"))):
                ws, _, short = member.rpartition(":")
                if ws not in ("", self.ws) or short not in self.names:
                    self.notes.append(f"group {gs_name}: member {member} isn't in {self.ws}'s imported layers; left out")
                    continue
                style = self.style(styles[i]) if i < len(styles) else None
                members.append({"layer": self.names[short], "style": style} if style else self.names[short])
            if not members:
                self.notes.append(f"group {gs_name}: no members left; left out")
                continue
            spec["layers"] = members
            cache = cache_setting(self.gs.tile_layer(f"{self.ws}:{gs_name}"))
            if cache:
                spec["cache"] = cache
            for flag in ("enabled", "advertised"):
                if g.get(flag) is False:
                    spec[flag] = False
            links = details.to_spec(g, {}).get("metadataLinks")
            if links:
                spec["metadataLinks"] = links
            found.append(resource("LayerGroup", name, spec))
        return found

    def gridsets(self, resources: list[dict]) -> list[dict]:
        """Gridset resources for the gridsets the imported layers are cached in that aren't GeoWebCache's own."""
        names = {g for r in resources if isinstance(r["spec"].get("cache"), dict) for g in r["spec"]["cache"]["gridsets"]}
        out = []
        for name in sorted(n for n in names if not BUILT_IN_GRIDSETS.match(n)):
            g = self.gs.gridset(name)
            if g is None:
                continue
            meta = resource_name(name)[0]
            spec = {"name": name} if meta != name else {}  # gridsets are named as written, dashes and all
            spec.update({"crs": f"EPSG:{g['srs']['number']}", "extent": g["extent"]["coords"], "resolutions": g["resolutions"]})
            if g.get("tileWidth", 256) != 256:
                spec["tileSize"] = g["tileWidth"]
            if g.get("alignTopLeft"):
                spec["alignTopLeft"] = True
            if float(g.get("metersPerUnit") or 1) != 1:
                spec["metersPerUnit"] = g["metersPerUnit"]
            out.append(resource("Gridset", meta, spec))
        return out

    def blob_stores(self, resources: list[dict]) -> list[dict]:
        """BlobStore resources for the blob stores the imported layers' tiles are kept in."""
        from .tiles import live_blob_store

        names = sorted({r["spec"]["cache"]["blobStore"] for r in resources
                        if isinstance(r["spec"].get("cache"), dict) and r["spec"]["cache"].get("blobStore")})
        out = []
        for name in names:
            response = self.gs.gwc_request("GET", f"blobstores/{name}", accept="application/xml")
            if response.status_code != 200:
                continue
            live = live_blob_store(response.text)
            meta = resource_name(name)[0]
            spec: dict = {"name": name} if meta != name else {}  # blob stores are named as written
            if "path" in live:
                path = data_path(live["path"])
                if not path:
                    self.notes.append(f"blob store {name}: {live['path']} isn't on the shared data volume; left out")
                    continue
                spec.update({"type": "file", "path": path})
            else:
                spec["type"] = "s3"
                spec.update({k: live[k] for k in ("bucket", "prefix", "endpoint", "region", "maxConnections", "gzip") if k in live})
                if live.get("access") and live["access"] != "PRIVATE":
                    spec["access"] = live["access"].lower().replace("_", "-")
                if live.get("accessKey"):
                    secret = f"{meta}-credentials"
                    spec["credentials"] = {"secret": secret}
                    self.notes.append(f"blob store {name}: put its keys in Secret {secret} (keys: accessKey, secretKey), "
                                      "and add it to gisdata.secrets")
            if live.get("default"):
                spec["default"] = True
            if not live.get("enabled", True):
                spec["enabled"] = False
            out.append(resource("BlobStore", meta, spec))
        return out

    def workspace(self) -> list[dict]:
        """A Workspace resource, when its namespace, isolation or being the default isn't GeoServer's default."""
        spec: dict = {}
        ns = (self.gs.get(f"namespaces/{self.ws}") or {}).get("namespace") or {}
        if ns.get("uri") and ns["uri"] != f"http://{self.ws}":
            spec["namespace"] = ns["uri"]
        if ((self.gs.get(f"workspaces/{self.ws}") or {}).get("workspace") or {}).get("isolated"):
            spec["isolated"] = True
        if ((self.gs.get("workspaces/default") or {}).get("workspace") or {}).get("name") == self.ws:
            spec["default"] = True
        if not spec:
            return []
        name, explicit = resource_name(self.ws)
        if explicit:
            spec["name"] = explicit
        return [resource("Workspace", name, spec)]

    def workspace_styles(self) -> None:
        for name in self.gs.names(f"workspaces/{self.ws}/styles", "style"):
            info = self.gs.style(name, self.ws)["style"]
            suffix = STYLE_SUFFIX.get(info.get("format", "sld"))
            if suffix is None:
                self.notes.append(f"style {name}: format {info.get('format')} isn't supported; left out")
                continue
            self.styles[name] = (suffix, self.gs.style_body(name, self.ws))
            legend = info.get("legend") or {}
            if legend.get("onlineResource"):
                self.legends[name] = {"url": legend["onlineResource"], "width": legend.get("width", 20),
                                      "height": legend.get("height", 20), "format": legend.get("format", "image/png")}


class Dumper(yaml.SafeDumper):
    """Writes text with line breaks (a SQL view's query) as a | block."""


Dumper.add_representer(str, lambda d, text: d.represent_scalar("tag:yaml.org,2002:str", text, style="|" if "\n" in text else None))


def dump(resources: list[dict]) -> str:
    return "\n---\n".join(yaml.dump(r, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=120).rstrip() for r in resources) + "\n"


def run(target: Path, ws: str | None, geoserver_url: str | None = None, force: bool = False,
        settings: bool = False, security: bool = False) -> int:
    if not (ws or settings or security):
        raise PlanError("name a workspace, or --settings or --security")
    target.mkdir(parents=True, exist_ok=True)
    existing = load(target)
    projects = existing.of_kind("Project")
    project = projects[0] if projects else None
    if project:
        try:
            gs = for_project(project, target, geoserver_url)
        except MissingCredentials as e:
            raise PlanError(str(e)) from None
        project_ws = project.spec.get("workspace")
        live = ((project.spec.get("database") or {}).get("schemas") or {}).get("live", "data_layers")
    else:
        if not geoserver_url:
            raise PlanError("no Project in this directory: give GeoServer's address with --geoserver")
        if not ws:
            raise PlanError("no Project in this directory: name a workspace too, for the project.yaml the import writes")
        user, password = secrets.lookup(SECRET, "username", target), secrets.lookup(SECRET, "password", target)
        if not user or not password:
            raise PlanError(f"GeoServer's login: set ${secrets.env_name(SECRET, 'username')} and "
                            f"${secrets.env_name(SECRET, 'password')}, or add them to secrets.yaml")
        gs = GeoServer(geoserver_url, user, password, gwc_url=os.environ.get("GISDATA_GWC_URL"))
        project_ws, live = ws, "data_layers"

    dataset_tables = {}
    for name, d in existing.named("Dataset").items():
        for key, options in (d.spec.get("tables") or {}).items():
            dataset_tables[(options or {}).get("name") or default_table(name, key)] = (name, key)

    from .import_config import USERS_SECRET, security_resources, settings_resources

    files: dict[Path, str] = {}
    report, notes = [], []
    try:
        if ws:
            if gs.workspace(ws) is None:
                raise PlanError(f"GeoServer has no workspace {ws}", code=1)
            imp = Importer(gs, ws, project_ws, live, dataset_tables)
            stores = imp.stores()
            layers = imp.layers(stores)
            groups = imp.groups()
            imp.workspace_styles()
            styles = []
            for name, (suffix, text) in imp.global_styles.items():  # not in styles/ itself, which holds the workspace's
                files[target / "styles" / "global" / f"{name}{suffix}"] = text if text.endswith("\n") else text + "\n"
                styles.append(resource("Style", resource_name(name)[0], {"file": f"styles/global/{name}{suffix}", "global": True}))
            for name, legend in imp.legends.items():  # a legend graphic needs the style named, with its file
                suffix = imp.styles[name][0]
                styles.append(resource("Style", name, {"file": f"styles/{name}{suffix}", "legend": legend}))
            files[target / f"{ws}.yaml"] = dump(imp.workspace() + list(imp.store_resources.values()) + layers + groups + styles
                                                + imp.gridsets(layers + groups) + imp.blob_stores(layers + groups))
            for name, (suffix, text) in imp.styles.items():
                files[target / "styles" / f"{name}{suffix}"] = text if text.endswith("\n") else text + "\n"
            linked = sum(1 for r in layers if "dataset" in r["spec"])
            report.append(f"{ws}.yaml: {len(imp.store_resources)} store(s), {len(layers)} layer(s) ({linked} on a Dataset's table), "
                          f"{len(groups)} group(s); {len(imp.styles)} style(s) in styles/")
            notes += imp.notes
            for secret in imp.secrets:
                notes.append(f"store passwords can't be read from GeoServer: put the login in Secret {secret} "
                             "(keys: username, password), and add it to gisdata.secrets")
        if settings:
            found, more = settings_resources(gs)
            files[target / "server.yaml"] = dump(found) if found else "# GeoServer's settings and services are as a fresh install has them\n"
            kinds = [r["kind"] for r in found]
            report.append(f"server.yaml: {kinds.count('Settings')} Settings, {kinds.count('Service')} Service(s)")
            notes += more
        if security:
            found, more, keys = security_resources(gs)
            files[target / "security.yaml"] = dump(found) if found else "# GeoServer's security is as a fresh install has it\n"
            kinds = [r["kind"] for r in found]
            report.append(f"security.yaml: {kinds.count('Role')} role(s), {kinds.count('Group')} group(s), "
                          f"{kinds.count('User')} user(s), {kinds.count('AccessRule')} rule(s)")
            notes += more
            if keys:
                notes.append(f"passwords can't be read from GeoServer: put each user's in Secret {USERS_SECRET} "
                             f"(keys: {', '.join(keys)}), and add it to gisdata.secrets; plan stops until it's there")
    except GeoServerError as e:
        raise PlanError(str(e), code=1) from None

    if project is None:
        jndi = next((e["jndiReferenceName"] for e in stores.values() if e.get("jndiReferenceName")), "java:comp/env/jdbc/mypostgis")
        report.append("project.yaml")
        files[target / "project.yaml"] = dump([resource("Project", resource_name(ws)[0], {
            "geoserver": {"url": geoserver_url, "credentials": {"secret": SECRET}},
            "database": {"jndi": jndi, "connection": {"secret": "geoserver-pgconfig-secret"}},
            "workspace": ws,
        })])
    clashes = [p for p in files if p.exists()]
    if clashes and not force:
        raise PlanError(f"{', '.join(str(p) for p in clashes)} already exist; --force replaces them")
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    print("Wrote " + "; ".join(report))
    for note in dict.fromkeys(notes):
        print(f"  note: {note}")
    print(f"Next: gisdata validate {target} && gisdata plan -C {target}")
    return 0
