"""`configops import`: write the files that describe a live GeoServer workspace.

The way in for an existing install. Only what differs from the spec's
defaults is written (a title equal to the name, GeoServer's own keywords,
the default style for the geometry all drop out), so `configops plan` on the
result shows nothing to do. Workspace styles are saved under styles/.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

import yaml

from . import assets, details, secrets, stores
from .geoserver import GROUP_MODES, GeoServer, GeoServerError, MissingCredentials, for_project
from .loader import default_table, load
from .plan import CACHE_OPTIONS, DEFAULT_FORMATS, DEFAULT_GRIDSETS, SOURCE_ATTRIBUTION, PlanError, as_list, names_of
from .keywords import to_entries
from .stores import DATA
from .validate import BUILT_IN_STYLES

API = "configops/v1alpha1"
RESOURCE_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SECRET = "geoserver-admin"  # the GeoServer secret a new project.yaml names
STYLE_SUFFIX = {"sld": ".sld", "css": ".css", "mbstyle": ".mbstyle"}
MODES = {v: k for k, v in GROUP_MODES.items()}
GEOMETRY_STYLE = {"Point": "point", "MultiPoint": "point", "LineString": "line", "MultiLineString": "line",
                  "Polygon": "polygon", "MultiPolygon": "polygon"}


FILE_DATASTORES = {"GeoPackage": ("geopackage", "database"), "Shapefile": ("shapefile", "url"),
                   "Directory of spatial files (shapefiles)": ("shapefiles", "url"), "FlatGeobuf": ("flatgeobuf", "url"),
                   "PMTiles": ("pmtiles", "pmtiles"), "GeoParquet": ("geoparquet", "uri"), "Properties": ("property", "directory"),
                   "CSV": ("csv", "file"), "Application Schema DataAccess": ("appschema", "url")}
FILE_COVERAGES = {"GeoTIFF": "geotiff", "WorldImage": "worldimage", "ArcGrid": "arcgrid", "ImageMosaic": "imagemosaic",
                  "ImagePyramid": "imagepyramid"}
CSV_PARAMS = ("strategy", "latField", "lngField", "wktField", "quoteAllFields", "writeprj")


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


ATTRIBUTE_DEFAULTS = {"nillable": True, "minOccurs": 0, "maxOccurs": 1}


def attribute_entry(a: dict):
    """A live attribute as the files write it: its name, or an object for a described or worked-out one."""
    from .plan import from_international

    name = a["name"]
    entry: dict = {"name": name}
    if a.get("source") not in (None, "", name, f'"{name}"'):
        entry["source"] = a["source"]
        if a.get("binding"):
            entry["binding"] = a["binding"]
    description = from_international(a.get("description"))
    if description:
        entry["description"] = description
    if len(entry) > 1:
        entry.update({k: a[k] for k, v in ATTRIBUTE_DEFAULTS.items() if a.get(k) is not None and a[k] != v})
    return entry if len(entry) > 1 else name


def cache_setting(tile_layer: dict | None, filters: dict | None = None):
    if not tile_layer or not tile_layer.get("enabled", True):
        return None
    gridsets = sorted(g["gridSetName"] for g in as_list(tile_layer.get("gridSubsets")))
    formats = sorted(as_list(tile_layer.get("mimeFormats")))
    options = {key: tile_layer[gwc] for key, gwc in CACHE_OPTIONS.items()
               if tile_layer.get(gwc) is not None and tile_layer.get(gwc) != GWC_DEFAULTS[gwc]}
    filters = dict(filters or {})
    styles = filters.get("STYLES")
    if styles is not None and "values" not in styles:
        del filters["STYLES"]  # GeoWebCache's own: any of the layer's styles
    for f in filters.values():
        f.pop("default", None) if f["type"] == "styles" else None
        if f.get("default") == "":
            del f["default"]
    if filters:
        options["parameterFilters"] = filters
    if gridsets == sorted(DEFAULT_GRIDSETS) and formats == sorted(DEFAULT_FORMATS) and not options:
        return True
    return {"gridsets": gridsets, "formats": formats, **options}


DEFAULTS_KEY = "x-defaults"  # on an imported resource only while updating files; never written


def resource(kind: str, name: str, spec: dict) -> dict:
    for plain, translated in (("title", "internationalTitle"), ("abstract", "internationalAbstract")):
        if spec.get(plain) and spec[plain] in (spec.get(translated) or {}).values():
            del spec[plain]  # GeoServer fills an empty title or abstract with a translation by itself
    return {"apiVersion": API, "kind": kind, "metadata": {"name": name}, "spec": spec}


class Importer:
    def __init__(self, gs: GeoServer, ws: str, project_ws: str | None, live_schema: str, dataset_tables: dict,
                 attributions: dict | None = None, project_crs: str | None = None, full: bool = False, columns=None):
        self.gs, self.ws, self.project_ws, self.live_schema = gs, ws, project_ws, live_schema
        self.project_crs = project_crs  # a declared CRS that's the project's is left out when the native one is unknown
        self.full = full  # write a layer's style even when it's the default for its geometry (updating files that name it)
        self.dataset_tables = dataset_tables  # table -> (dataset, key), from the project's Datasets
        self.attributions = attributions or {}  # dataset -> the attribution its source gives by itself
        self.columns = columns  # (schema, table) -> the table's columns, less its key; None without the database
        self.notes: list[str] = []
        self.names: dict[str, str] = {}  # GeoServer name -> resource name, for group members
        self.styles: dict[str, tuple[str, str]] = {}  # style name -> (suffix, text)
        self.store_resources: dict[str, dict] = {}  # GeoServer store name -> Store resource, for stores that aren't the JNDI PostGIS
        self.secrets: list[str] = []  # Secrets the imported stores name
        self.global_styles: dict[str, tuple[str, str]] = {}  # global style name -> (suffix, text)
        self.legends: dict[str, dict] = {}  # workspace style name -> its legend graphic

    def cache(self, name: str):
        tile_layer = self.gs.tile_layer(name)
        setting = cache_setting(tile_layer, self.gs.parameter_filters(name) if tile_layer else None)
        extras = self.gs.tile_layer_extras(name) if tile_layer else {}
        if setting and extras:
            setting = {"gridsets": sorted(DEFAULT_GRIDSETS), "formats": sorted(DEFAULT_FORMATS)} if setting is True else setting
            setting.update(extras)
        return setting

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
            elif entries.get("dbtype") in ("postgis", "sqlserver"):
                connection = {k: entries[k] for k in ("host", "port", "database", "schema") if entries.get(k)}
                secret = f"{resource_name(name)[0]}-credentials"
                self.secrets.append(secret)
                self.store_resource(store, name, {"type": entries["dbtype"], "connection": connection, "credentials": {"secret": secret}})
            elif entries.get("WFSDataStoreFactory:GET_CAPABILITIES_URL"):
                self.store_resource(store, name, {"type": "wfs", "url": entries["WFSDataStoreFactory:GET_CAPABILITIES_URL"]})
            elif store.get("type") in FILE_DATASTORES:
                kind, param = FILE_DATASTORES[store["type"]]
                where = entries.get(param) or ""
                path = data_path(where)
                if path:
                    spec = {"type": kind, "path": path}
                    if kind == "csv":
                        spec["connection"] = {k: entries[k] for k in CSV_PARAMS if entries.get(k)}
                    self.store_resource(store, name, spec)
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
                if store.get("user"):
                    secret = f"{resource_name(name)[0]}-credentials"
                    self.secrets.append(secret)
                    spec["credentials"] = {"secret": secret}
                for field, default in (("maxConnections", 6), ("readTimeout", 60), ("connectTimeout", 30)):
                    if store.get(field) not in (None, default):
                        spec[field] = store[field]
                if not stores.live_store(store, spec["type"])["connectionPooling"]:
                    spec["connectionPooling"] = False
                for field, kind_name in (("headerValue", "header"), ("authKey", "authKey")):
                    if store.get(field):  # may be a key or token: from a Secret, as logins are
                        secret = f"{resource_name(name)[0]}-{kind_name.lower()}"
                        self.secrets.append(secret)
                        ref = {"secret": secret, "key": "value"}
                        spec[kind_name] = {"name": store.get("headerName") or "", **ref} if kind_name == "header" else ref
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
        if live.get("disableOnConnFailure"):
            spec["disableOnConnectionFailure"] = True
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
        view = stores.live_view(item)
        if view:
            defaults = {"envelope": "intersection", "resolution": "best", "resolutionIndex": -1, "fillMissingBands": False}
            spec["view"] = {k: v for k, v in view.items() if defaults.get(k, object()) != v}
        else:
            spec["resource"] = item.get("nativeCoverageName") or item.get("nativeName")
        if item.get("title") and item["title"] != gs_name:
            spec["title"] = item["title"]
        if item.get("abstract"):
            spec["abstract"] = item["abstract"]
        keywords = names_of((item.get("keywords") or {}).get("string"))
        if keywords and not (len(keywords) == 2 and "features" in keywords):  # GeoServer's own: features and the name
            spec["keywords"] = to_entries(keywords)
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
        cache = self.cache(f"{self.ws}:{gs_name}")
        if cache:
            spec["cache"] = cache
        spec.update(details.to_spec(item, layer))
        if "forcedRemoteStyle" in item:  # a cascaded WMS layer
            remote = {k: v for k, v in stores.live_remote(item).items()
                      if v not in (None, "", [], {}, False) and not (k == "format" and v == "image/png")}
            if remote:
                spec["remote"] = remote
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
                spec["keywords"] = to_entries(keywords)
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
            if title and title != self.attributions.get(linked[0] if linked else None):
                spec["attribution"] = title
            cache = self.cache(f"{self.ws}:{gs_name}")
            if cache:
                spec["cache"] = cache
            live_attrs = [a for a in as_list((ft.get("attributes") or {}).get("attribute"))
                          if not str(a.get("binding", "")).startswith("org.locationtech.jts.geom.")]
            published = [a["name"] for a in live_attrs]
            entries = [attribute_entry(a) for a in live_attrs]
            full = self.columns(schema, table) if self.columns else None
            customised = any(isinstance(e, dict) for e in entries)
            if customised or (full is not None and published not in full):  # GeoServer lists every attribute unless the layer narrows them
                spec["attributes"] = entries
            if ft.get("projectionPolicy") == "FORCE_DECLARED" and ft.get("srs") and ft["srs"] != (native_code(ft) or self.project_crs):
                spec["crs"] = ft["srs"]
            spec.update(details.to_spec(ft, layer))
            doc = resource("Layer", name, spec)
            if self.full:  # what leaving a field out means here, for comparing with files that spell it out
                doc[DEFAULTS_KEY] = {"style": default_style(ft), "crs": ft.get("srs") if native_code(ft) is None else native_code(ft)}
            out.append(doc)
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
            if names_of((g.get("keywords") or {}).get("string")):
                spec["keywords"] = to_entries(names_of(g["keywords"]["string"]))
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
            cache = self.cache(f"{self.ws}:{gs_name}")
            if cache:
                spec["cache"] = cache
            for flag in ("enabled", "advertised"):
                if g.get(flag) is False:
                    spec[flag] = False
            as_resource = details.to_spec(g, {})
            spec.update({k: as_resource[k] for k in ("metadataLinks", *details.TRANSLATED) if k in as_resource})
            as_layer = details.to_spec({}, g)
            spec.update({k: as_layer[k] for k in ("attribution", "authorityURLs", "identifiers") if k in as_layer})
            if g.get("queryDisabled"):
                spec["queryable"] = False
            root = (g.get("rootLayer") or {}).get("name")
            if root:
                short = root.split(":", 1)[-1]
                if short in self.names:
                    spec["rootLayer"] = self.names[short]
                    style = self.style((g.get("rootLayerStyle") or {}).get("name"))
                    root_default = ((self.gs.layer(self.ws, short) or {}).get("layer", {}).get("defaultStyle") or {}).get("name")
                    if style and style != self.style(root_default):
                        spec["rootStyle"] = style
                else:
                    self.notes.append(f"group {gs_name}: root layer {root} isn't in {self.ws}'s imported layers; left out")
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
            from .tiles import live as live_gridset

            extra = live_gridset(g)
            if extra["description"]:
                spec["description"] = extra["description"]
            if extra["levelNames"] and extra["levelNames"] != [f"{name}:{i}" for i in range(len(g["resolutions"]))]:
                spec["levelNames"] = extra["levelNames"]
            if extra["pixelSize"] != 2.8e-4:
                spec["pixelSize"] = extra["pixelSize"]
            if extra["yAxisFirst"]:
                spec["yAxisFirst"] = True
            out.append(resource("Gridset", meta, spec))
        return out

    def blob_stores(self, resources: list[dict]) -> list[dict]:
        """BlobStore resources for the blob stores the imported layers' tiles are kept in."""
        from .tiles import FIELDS, KEYS, live_blob_store

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
                if live.get("layout") == "tms":
                    spec["layout"] = "tms"
            elif live["type"] in KEYS:
                kind = live["type"]
                spec["type"] = kind
                spec.update({k: live[k] for k in FIELDS[kind] if k in live and k != "access"})
                if kind == "s3":
                    spec = {k: v for k, v in spec.items() if (k, v) not in (("maxConnections", 50), ("gzip", True))}
                    if live.get("access") and live["access"] != "PRIVATE":
                        spec["access"] = live["access"].lower().replace("_", "-")
                if kind == "gcs" and not live.get("defaultCredentials"):
                    spec.pop("defaultCredentials", None)
                proxy = {k[6:]: v for k, v in live.items() if k.startswith("proxy.") and k not in ("proxy.username", "proxy.password")}
                if "proxy.username" in live:
                    proxy["credentials"] = {"secret": f"{meta}-proxy"}
                    self.notes.append(f"blob store {name}: put its proxy login in Secret {meta}-proxy (keys: username, password)")
                if proxy:
                    spec["proxy"] = proxy
                if any(k in live for k in KEYS[kind]):
                    secret = f"{meta}-credentials"
                    spec["credentials"] = {"secret": secret}
                    self.notes.append(f"blob store {name}: put its keys in Secret {secret} (keys: {', '.join(KEYS[kind])}), "
                                      "and add it to configops.secrets")
            else:
                self.notes.append(f"blob store {name}: its type isn't one the files have; left out")
                continue
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


def update_files(target: Path, existing, docs: dict[Path, list[dict]], files: dict[Path, str], ws: str | None,
                 settings: bool, security: bool, prune: bool, notes: list[str], found: dict[str, bytes]) -> int:
    """Import into an existing project: change only what's different (configops/update.py)."""
    from .update import merge

    result = merge(target, existing, docs, ws, settings, security, prune)
    style_files = {r.name: target / r.spec["file"] for r in existing.of_kind("Style") if r.spec.get("file")}
    for path, text in files.items():  # style files: rewritten where the project keeps them, if GeoServer's differ
        name = path.stem
        where = style_files.get(name, path)
        current = where.read_text(encoding="utf-8") if where.exists() else None
        if current is None or current.replace("\r\n", "\n").strip() != text.replace("\r\n", "\n").strip():
            where.parent.mkdir(parents=True, exist_ok=True)
            where.write_text(text, encoding="utf-8")
            result.styles.append(str(where.relative_to(target)))
    lines = [("changed", result.changed), ("added", result.added), ("removed", result.removed), ("style files written", result.styles),
             ("images and templates written", assets.write(target, found))]
    said = [f"{len(items)} {what}: {', '.join(items)}" for what, items in lines if items]
    print("Updated the files: " + "; ".join(said) if said else "No changes: the files already match GeoServer.")
    for item in result.missing:
        print(f"  not in GeoServer: {item} (plan would make it again; --prune removes it from the files)")
    for note in dict.fromkeys(notes):
        print(f"  note: {note}")
    print(f"Next: git diff, then configops plan -C {target}")
    return 0


def run(target: Path, ws: str | None, geoserver_url: str | None = None, force: bool = False,
        settings: bool = False, security: bool = False, prune: bool = False) -> int:
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
        gs = GeoServer(geoserver_url, user, password, gwc_url=os.environ.get("CONFIGOPS_GWC_URL"))
        project_ws, live = ws, "data_layers"

    dataset_tables, attributions = {}, {}
    sources = existing.named("Source")
    for name, d in existing.named("Dataset").items():
        for key, options in (d.spec.get("tables") or {}).items():
            dataset_tables[(options or {}).get("name") or default_table(name, key)] = (name, key)
        source = sources.get(d.spec.get("source") or "")
        text = SOURCE_ATTRIBUTION.get(source.spec.get("type")) if source else None
        if text:
            attributions[name] = text.format(year=date.today().year)
    for layer in existing.of_kind("Layer"):  # a Dataset's tables that only its Layers name
        dataset, key = layer.spec.get("dataset"), layer.spec.get("table")
        if dataset in existing.named("Dataset") and isinstance(key, str):
            options = (existing.named("Dataset")[dataset].spec.get("tables") or {}).get(key) or {}
            dataset_tables.setdefault(options.get("name") or default_table(dataset, key), (dataset, key))

    from .import_config import USERS_SECRET, security_resources, settings_resources

    files: dict[Path, str] = {}  # style files, and a note for a kind with nothing in it
    docs: dict[Path, list[dict]] = {}  # resources, by the file a fresh import writes them to
    asset_files: dict[str, bytes] = {}  # the workspace's images and templates, by their path in the project
    report, notes = [], []
    try:
        if ws:
            if gs.workspace(ws) is None:
                raise PlanError(f"GeoServer has no workspace {ws}", code=1)
            imp = Importer(gs, ws, project_ws, live, dataset_tables, attributions,
                           project_crs=project.spec.get("crs") if project else None, full=project is not None and not force,
                           columns=table_columns(project, target) if project else None)
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
            docs[target / f"{ws}.yaml"] = (imp.workspace() + list(imp.store_resources.values()) + layers + groups + styles
                                           + imp.gridsets(layers + groups) + imp.blob_stores(layers + groups))
            for name, (suffix, text) in imp.styles.items():
                files[target / "styles" / f"{name}{suffix}"] = text if text.endswith("\n") else text + "\n"
            linked = sum(1 for r in layers if "dataset" in r["spec"])
            report.append(f"{ws}.yaml: {len(imp.store_resources)} store(s), {len(layers)} layer(s) ({linked} on a Dataset's table), "
                          f"{len(groups)} group(s); {len(imp.styles)} style(s) in styles/")
            notes += imp.notes
            asset_files, more = assets.download(gs, ws, {r["spec"].get("name") or r["metadata"]["name"].replace("-", "_"): r["metadata"]["name"]
                                                   for r in layers})
            notes += more
            if asset_files:
                report.append(f"{len(asset_files)} image(s) and template(s) in styles/ and templates/")
            for secret in imp.secrets:
                notes.append(f"store passwords can't be read from GeoServer: put the login in Secret {secret} "
                             "(keys: username, password), and add it to configops.secrets")
        if settings:
            found, more = settings_resources(gs)
            docs[target / "server.yaml"] = found
            kinds = [r["kind"] for r in found]
            report.append(f"server.yaml: {kinds.count('Settings')} Settings, {kinds.count('Service')} Service(s)")
            notes += more
        if security:
            found, more, keys = security_resources(gs)
            docs[target / "security.yaml"] = found
            kinds = [r["kind"] for r in found]
            report.append(f"security.yaml: {kinds.count('Role')} role(s), {kinds.count('Group')} group(s), "
                          f"{kinds.count('User')} user(s), {kinds.count('AccessRule')} rule(s)")
            notes += more
            if keys:
                notes.append(f"passwords can't be read from GeoServer: put each user's in Secret {USERS_SECRET} "
                             f"(keys: {', '.join(keys)}), and add it to configops.secrets; plan stops until it's there")
    except GeoServerError as e:
        raise PlanError(str(e), code=1) from None

    if project is not None and not force:
        from .update import in_scope

        default_ws = project.spec.get("workspace")
        if any(p.exists() for p in docs) or any(in_scope(r, ws, settings, security, default_ws) for r in existing.resources):
            return update_files(target, existing, docs, files, ws, settings, security, prune, notes, asset_files)

    for path, found in docs.items():
        found = [{k: v for k, v in r.items() if k != DEFAULTS_KEY} for r in found]
        files[path] = dump(found) if found else "# As a fresh GeoServer install has it\n"
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
    assets.write(target, asset_files)

    print("Wrote " + "; ".join(report))
    for note in dict.fromkeys(notes):
        print(f"  note: {note}")
    print(f"Next: configops validate {target} && configops plan -C {target}")
    return 0


def native_code(ft: dict) -> str | None:
    """The feature type's native CRS as EPSG:<code>, read from its WKT (the last AUTHORITY is the CRS's own)."""
    native = ft.get("nativeCRS")
    text = native.get("$") if isinstance(native, dict) else native
    if not isinstance(text, str):
        return None
    if text.startswith("EPSG:"):
        return text
    codes = re.findall(r'AUTHORITY\["EPSG",\s*"(\d+)"\]', text)
    return f"EPSG:{codes[-1]}" if codes else None


def table_columns(project, root: Path):
    """A lookup of a table's full attribute lists, with its key and without (a store may expose keys or not),
    or None if the database can't be reached."""
    from .sync import database

    try:
        conn = database(project, root).connect()
    except Exception:  # import works without the database; it just can't tell narrowed attributes
        return None
    cache: dict = {}

    def columns(schema: str, table: str) -> list[list[str]] | None:
        if (schema, table) not in cache:
            with conn.cursor() as cur:
                cur.execute("""SELECT a.attname, i.indrelid IS NOT NULL FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
                               JOIN pg_namespace n ON n.oid = c.relnamespace
                               LEFT JOIN pg_index i ON i.indrelid = c.oid AND i.indisprimary AND a.attnum = ANY(i.indkey)
                               WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
                                 AND format_type(a.atttypid, a.atttypmod) NOT LIKE 'geometry%%'
                               ORDER BY a.attnum""", (schema, table))
                rows = cur.fetchall()
            conn.rollback()
            cache[(schema, table)] = [[name for name, _ in rows], [name for name, key in rows if not key]] if rows else None
        return cache[(schema, table)]
    return columns
