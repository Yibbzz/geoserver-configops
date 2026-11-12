"""`configops plan` and `configops apply`: make GeoServer match the files.

`plan` works out every change: what the files describe, compared with live
GeoServer (and PostGIS, for whether a layer's table exists yet). Only
fields the files set, or whose defaults the spec defines, are compared, so
a plan right after `apply` is empty. `apply` makes the same changes, in
dependency order. Each Change carries the call that makes it.

Everything in a workspace the project names belongs to it: what the files
don't mention there is shown as unmanaged, and removed only with --prune.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import psycopg2

from . import assets, auth, details, discover, ingest, remote, security, server, stores, sync, tiles, urlchecks
from .geoserver import GROUP_MODES, GeoServer, GeoServerError, MissingCredentials, for_project
from .loader import Project, Resource, default_table, load
from .sources import SourceError
from .validate import BUILT_IN_STYLES, STYLE_SUFFIXES, geoserver_name, members, validate

# Apply order: what a change needs is made first; deletions come last, outermost first
ORDER = ["workspace", "settings", "service", "security", "security settings", "url check", "role", "user group", "user", "raster", "store", "file", "style", "blobstore", "gridset", "table", "layer", "template", "group", "rule"]
DELETE_ORDER = ["url check", "rule", "user", "user group", "role", "group", "template", "layer", "style", "file", "store"]

# Every kind of store a workspace can hold: (REST collection, JSON key)
STORE_COLLECTIONS = [("datastores", "dataStore"), ("coveragestores", "coverageStore"), ("wmsstores", "wmsStore"), ("wmtsstores", "wmtsStore")]

# `cache: true`: what GeoServer itself would cache
DEFAULT_GRIDSETS = ["EPSG:4326", "EPSG:900913"]
DEFAULT_FORMATS = ["image/png"]

# Attribution a source's licence asks for, when a layer's file gives none
SOURCE_ATTRIBUTION = {"os-downloads": "Contains OS data © Crown copyright and database right {year}"}


@dataclass
class Change:
    action: str  # + add, ~ change, - delete, ? unmanaged (left alone), ! can't be done
    kind: str  # workspace, settings, service, role, user group, user, store, style, table, layer, group, rule
    target: str
    fields: list[tuple[str, object, object]] = field(default_factory=list)  # (field, live, wanted)
    note: str = ""
    run: Callable[[], None] | None = None


def as_list(value) -> list:
    """GeoServer gives one item as an object and several as a list; "" for none."""
    if value in (None, ""):
        return []
    return value if isinstance(value, list) else [value]


def names_of(items) -> list[str]:
    return [i.get("name") if isinstance(i, dict) else i for i in as_list(items)]


# Tile cache options the files may set, beyond gridsets and formats: spec name -> GeoWebCache's
TILE_EXTRAS = ("levels", "expireByZoom", "warningSkips")  # read from the tile layer's XML
CACHE_OPTIONS = {"metaTiles": "metaWidthHeight", "gutter": "gutter", "expireCache": "expireCache", "expireClients": "expireClients",
                 "blobStore": "blobStoreId"}


def wanted_filters(filters: dict) -> dict:
    """Parameter filters as compared: numbers as floats, and the STYLES one GeoWebCache adds by itself."""
    out = {"STYLES": {"type": "styles"}}
    for key, f in filters.items():
        f = dict(f)
        if f["type"] in ("numbers", "integers"):
            f["values"] = [float(v) if f["type"] == "numbers" else int(v) for v in f.get("values", [])]
            if "threshold" in f:  # GeoWebCache wants a whole number for an integers filter
                f["threshold"] = float(f["threshold"]) if f["type"] == "numbers" else int(f["threshold"])
        if "default" in f:
            f["default"] = str(f["default"])
        out[key] = f
    return out


def wanted_cache(setting) -> dict | bool:
    """The spec's `cache` as {gridsets, formats, and any options it sets}, or False for no cache."""
    if not setting:
        return False
    if setting is True:
        setting = {}
    out = {"gridsets": sorted(setting.get("gridsets", DEFAULT_GRIDSETS)), "formats": sorted(setting.get("formats", DEFAULT_FORMATS))}
    out.update({k: setting[k] for k in CACHE_OPTIONS if k in setting})
    if "levels" in setting:
        out["levels"] = {g: {k: [float(x) for x in v] if k == "bounds" else [int(x) for x in v] for k, v in lv.items()}
                         for g, lv in setting["levels"].items()}
    if "expireByZoom" in setting:
        out["expireByZoom"] = {int(z): int(s) for z, s in setting["expireByZoom"].items()}
    if "warningSkips" in setting:
        out["warningSkips"] = sorted(setting["warningSkips"])
    if "parameterFilters" in setting:
        out["parameterFilters"] = wanted_filters(setting["parameterFilters"])
    return out


def live_cache(tile_layer: dict | None, wanted: dict | bool = False, filters: dict | None = None) -> dict | bool:
    """A live tile layer in wanted_cache's form, with the options `wanted` sets (and its parameter filters, given)."""
    if not tile_layer or not tile_layer.get("enabled", True):
        return False
    out = {"gridsets": sorted(g["gridSetName"] for g in as_list(tile_layer.get("gridSubsets"))),
           "formats": sorted(as_list(tile_layer.get("mimeFormats")))}
    for key, gwc in CACHE_OPTIONS.items():
        if isinstance(wanted, dict) and key in wanted:
            out[key] = tile_layer.get(gwc)
    if isinstance(wanted, dict) and "parameterFilters" in wanted:
        out["parameterFilters"] = compared_filters(filters or {}, wanted["parameterFilters"])
    return out


def compared_filters(live: dict, wanted: dict) -> dict:
    """The live filters with only the fields the files set (a STYLES filter's default is the layer's style)."""
    out = {}
    for key, f in live.items():
        want = wanted.get(key, {})
        out[key] = {k: v for k, v in f.items() if k == "type" or k in want or k == "values"}
    return out


def cache_name(gs: GeoServer, name: str, wanted: dict | bool) -> dict | bool:
    """The live cache of `name`, in the form `wanted` is compared in."""
    filters = gs.parameter_filters(name) if isinstance(wanted, dict) and "parameterFilters" in wanted else None
    out = live_cache(gs.tile_layer(name), wanted, filters)
    if isinstance(out, dict) and isinstance(wanted, dict) and any(k in wanted for k in TILE_EXTRAS):
        extras = gs.tile_layer_extras(name)
        for key in TILE_EXTRAS:
            if key in wanted:
                live = extras.get(key, {} if key != "warningSkips" else [])
                out[key] = {g: {k: v for k, v in live.get(g, {}).items() if k in wanted[key].get(g, {})} for g in wanted[key]} \
                    if key == "levels" else live
    return out


def set_cache(gs: GeoServer, name: str, wanted: dict | bool, seed: dict | None = None) -> None:
    """Cache `name` as wanted; no cache drops the one GeoServer makes for every new layer. Then seed, if asked."""
    if wanted:
        options = {k: wanted[k] for k in CACHE_OPTIONS if k in wanted}
        if "parameterFilters" in wanted:
            options["parameterFilters"] = wanted["parameterFilters"]
        options.update({k: wanted[k] for k in TILE_EXTRAS if k in wanted})
        gs.put_tile_layer(name, wanted["gridsets"], wanted["formats"], options)
        if seed:
            seed_cache(gs, name, wanted, seed)
    else:
        gs.delete_tile_layer(name)


def seed_of(spec: dict) -> dict | None:
    cache = spec.get("cache")
    return cache.get("seed") if isinstance(cache, dict) else None


def seed_cache(gs: GeoServer, name: str, cache: dict, seed: dict) -> None:
    """Draw the tiles `seed` asks for, in each of its gridsets and formats (in the background)."""
    for gridset in seed.get("gridsets") or cache["gridsets"]:
        for fmt in seed.get("formats") or cache["formats"]:
            gs.seed(name, gridset, fmt, seed["zoom"], seed.get("bounds"), seed.get("threads", 1))


def is_geometry(attribute: dict) -> bool:
    return str(attribute.get("binding", "")).startswith("org.locationtech.jts.geom.")


def virtual_table(name: str, sql: dict) -> dict:
    """A Layer's sql: as GeoServer's virtual table."""
    view = {"name": name, "sql": sql["query"].strip(), "escapeSql": False}
    if sql.get("key"):
        view["keyColumn"] = sql["key"]
    geometry = sql.get("geometry")
    if geometry:
        view["geometry"] = {"name": geometry["column"], "type": geometry.get("type", "Geometry"),
                            "srid": int(str(geometry.get("crs", "EPSG:4326")).split(":")[-1])}
    return view


def sql_fields(view: dict) -> dict:
    """What's compared of a virtual table: GeoServer adds a newline to the query."""
    geometry = view.get("geometry") or {}
    return {"query": " ".join(str(view.get("sql", "")).split()), "key": view.get("keyColumn"),
            "geometry": [geometry.get("name"), geometry.get("type"), geometry.get("srid")] if geometry else None}


def diff(pairs: list[tuple[str, object, object]]) -> list[tuple[str, object, object]]:
    """The pairs that differ; where both sides are objects (a cache), only the fields inside that differ, as a.b."""
    out = []
    for name, live, wanted in pairs:
        if live == wanted:
            continue
        if isinstance(live, dict) and isinstance(wanted, dict):
            out += diff([(f"{name}.{k}", live.get(k), wanted.get(k)) for k in dict.fromkeys([*live, *wanted])])
        else:
            out.append((name, live, wanted))
    return out


class Planner:
    def __init__(self, files: Project, root: Path, gs: GeoServer, conn, db: ingest.Database, prune: bool = False):
        self.files, self.root, self.gs, self.conn, self.db, self.prune = files, root, gs, conn, db, prune
        self.project = files.of_kind("Project")[0]
        self.ws = self.project.spec["workspace"]
        self.jndi = self.project.spec["database"]["jndi"]
        self.datasets = files.named("Dataset")
        self.layers = files.named("Layer")
        self.groups = files.named("LayerGroup")
        self.styles = self.style_files()
        self.changes: list[Change] = []
        self.synced: set[str] = set()
        self.unpublished: set[str] = set()  # layers and groups that can't be published yet ("!")

    # --- what the files say

    def workspace_of(self, r: Resource) -> str:
        return r.spec.get("workspace") or self.ws

    def style_files(self) -> dict[str, tuple[Path, str]]:
        """Style name -> (file, workspace): files in styles/, overridden by Style resources."""
        found = {}
        styles_dir = self.root / "styles"
        if styles_dir.is_dir():
            for f in sorted(styles_dir.iterdir()):
                if f.is_file() and f.suffix in STYLE_SUFFIXES:
                    found[f.stem] = (f, self.ws)
        for name, s in self.files.named("Style").items():
            found[name] = (self.root / s.spec["file"], None if s.spec.get("global") else self.workspace_of(s))
        return found

    def style_ref(self, name: str | None) -> str | None:
        """A style as a layer refers to it: workspace styles qualified, GeoServer's built-in ones not."""
        if name is None or name in BUILT_IN_STYLES and name not in self.styles:
            return name
        ws = self.styles[name][1]
        return f"{ws}:{name}" if ws else name  # a global style is named without a workspace

    def table_of(self, layer: Resource) -> tuple[str, str, str | None]:
        """(schema, table, dataset) a layer publishes; a SQL view is named after the layer."""
        if "sql" in layer.spec:
            return layer.spec["sql"]["schema"], geoserver_name(layer), None
        table = layer.spec["table"]
        if isinstance(table, dict):
            return table["schema"], table["name"], None
        dataset = self.datasets[layer.spec["dataset"]]
        options = (dataset.spec.get("tables") or {}).get(table) or {}
        return self.db.live, options.get("name") or default_table(dataset.name, table), dataset.name

    def attribution(self, layer: Resource) -> str | None:
        """The layer's own attribution, else what its dataset's source asks for."""
        if "attribution" in layer.spec:
            text = layer.spec["attribution"]
            return None if isinstance(text, dict) else text  # an attribution with a link or logo: details.py
        dataset = self.datasets.get(layer.spec.get("dataset") or "")
        source = self.files.named("Source").get(dataset.spec["source"]) if dataset else None
        text = SOURCE_ATTRIBUTION.get(source.spec.get("type")) if source else None
        return text.format(year=date.today().year) if text else None

    def published(self, name: str) -> tuple[str, str]:
        """("layer" or "layerGroup", ws:name) for a group member."""
        r = self.layers.get(name) or self.groups[name]
        return ("layer" if name in self.layers else "layerGroup"), f"{self.workspace_of(r)}:{geoserver_name(r)}"

    # --- comparing

    def add(self, *args, **kwargs) -> None:
        self.changes.append(Change(*args, **kwargs))

    def plan(self) -> list[Change]:
        workspaces = {geoserver_name(w): w for w in self.files.of_kind("Workspace")}
        managed = sorted({self.ws} | set(workspaces) | {self.workspace_of(r) for r in [*self.layers.values(), *self.groups.values()]}
                         | {self.workspace_of(s) for s in self.files.named("Style").values() if not s.spec.get("global")})
        for ws in managed:
            if self.gs.workspace(ws) is None:
                self.add("+", "workspace", ws, run=lambda ws=ws: self.gs.upsert_workspace(ws))
        for ws, w in workspaces.items():
            self.plan_workspace(ws, w)
        server.plan(self)
        auth.plan(self)
        self.plan_stores()
        stores.plan(self)
        self.plan_styles()
        self.plan_layers()
        self.plan_groups()
        security.plan(self)
        urlchecks.plan(self)
        tiles.plan(self)
        assets.plan(self, managed)
        self.plan_unmanaged(managed)
        return self.changes

    def wanted_stores(self) -> dict[tuple[str, str], None]:
        """(workspace, schema) of every store the layers need; a store per schema, named after it."""
        return {(self.workspace_of(layer), self.table_of(layer)[0]): None for layer in self.layers.values() if "store" not in layer.spec}

    def plan_stores(self) -> None:
        for ws, schema in self.wanted_stores():
            live = self.gs.datastore(ws, schema)
            run = (lambda ws=ws, schema=schema: self.gs.upsert_jndi_datastore(ws, schema, self.jndi, schema))
            if live is None:
                self.add("+", "store", f"{ws}:{schema}", note=f"PostGIS through {self.jndi}", run=run)
                continue
            entries = {e["@key"]: e.get("$") for e in as_list((live["dataStore"].get("connectionParameters") or {}).get("entry"))}
            changed = diff([("jndiReferenceName", entries.get("jndiReferenceName"), self.jndi), ("schema", entries.get("schema"), schema)])
            if changed:
                self.add("~", "store", f"{ws}:{schema}", changed, run=run)

    def plan_styles(self) -> None:
        resources = self.files.named("Style")
        for name, (path, ws) in self.styles.items():
            text = path.read_text(encoding="utf-8")
            live = self.gs.style_body(name, ws)
            legend = (resources[name].spec.get("legend") if name in resources else None)
            wanted_legend = {"onlineResource": legend["url"], "width": legend.get("width", 20), "height": legend.get("height", 20),
                             "format": legend.get("format", "image/png")} if legend else None

            def run(name=name, text=text, suffix=path.suffix, ws=ws, wanted_legend=wanted_legend):
                self.gs.upsert_style(name, text, suffix, ws)
                if wanted_legend:
                    self.gs.request("PUT", self.gs.style_path(name, ws)[0], {"style": {"legend": wanted_legend}})
            if live is not None and wanted_legend:
                have = ((self.gs.style(name, ws) or {}).get("style") or {}).get("legend") or {}
                have = {k: have.get(k) for k in wanted_legend}
                if have != wanted_legend:
                    self.add("~", "style", f"{ws}:{name}" if ws else f"{name} (global)", [("legend", have, wanted_legend)], run=run)
                    continue
            target = f"{ws}:{name}" if ws else f"{name} (global)"
            if live is None:
                self.add("+", "style", target, note=str(path.relative_to(self.root)), run=run)
            elif live.replace("\r\n", "\n").strip() != text.replace("\r\n", "\n").strip():
                self.add("~", "style", target, note=f"{path.relative_to(self.root)} differs from GeoServer's copy", run=run)

    def layer_settings(self, layer: Resource, resource: tuple[str, str] | None = None) -> tuple[list[tuple[str, object]], Callable[[], None]]:
        """
        (the layer's publishing settings as (field, wanted), the call that sets them): styles,
        queryable, attribution, cache, and the details (configops/details.py). `resource` is the
        REST path and JSON key of the layer's feature type or coverage.
        """
        spec, ws, name = layer.spec, self.workspace_of(layer), geoserver_name(layer)
        resource_wanted, layer_wanted = details.wanted_resource(spec), details.wanted_layer(spec)
        attribution = self.attribution(layer)
        cache = wanted_cache(spec.get("cache"))
        wanted = [("queryable", spec.get("queryable", True)), ("cache", cache)]
        if attribution is not None:
            wanted.append(("attribution", attribution))
        if spec.get("style"):
            wanted.append(("style", self.style_ref(spec["style"])))
        if "styles" in spec:
            wanted.append(("styles", [self.style_ref(s) for s in spec["styles"]]))
        wanted += list(resource_wanted.items()) + list(layer_wanted.items())

        def run():
            self.gs.update_layer(ws, name, style=self.style_ref(spec.get("style")),
                                 styles=[self.style_ref(s) for s in spec["styles"]] if "styles" in spec else None,
                                 queryable=spec.get("queryable", True), attribution=attribution)
            if resource_wanted and resource:
                path, key = resource
                live = (self.gs.get(path) or {}).get(key) or {}
                self.gs.request("PUT", path, {key: details.patch_resource(live, resource_wanted)})
            if layer_wanted:
                live = (self.gs.layer(ws, name) or {}).get("layer") or {}
                self.gs.request("PUT", f"workspaces/{ws}/layers/{name}", {"layer": details.patch_layer(live, layer_wanted)})
            set_cache(self.gs, f"{ws}:{name}", cache, seed_of(spec))
        return wanted, run

    def live_layer_settings(self, layer: Resource, live_layer: dict, live_resource: dict | None = None) -> dict:
        live = live_layer["layer"]
        current = {"queryable": live.get("queryable", True), "style": (live.get("defaultStyle") or {}).get("name"),
                   "styles": names_of((live.get("styles") or {}).get("style")),
                   "attribution": (live.get("attribution") or {}).get("title"),
                   "cache": cache_name(self.gs, f"{self.workspace_of(layer)}:{geoserver_name(layer)}",
                                       wanted_cache(layer.spec.get("cache")))}
        current.update(details.live_resource(live_resource or {}, details.wanted_resource(layer.spec)))
        current.update(details.live_layer(live, details.wanted_layer(layer.spec)))
        return current

    def plan_layers(self) -> None:
        for layer in self.layers.values():
            if "store" in layer.spec:
                continue  # stores.plan publishes it
            ws, name, spec = self.workspace_of(layer), geoserver_name(layer), layer.spec
            schema, table, dataset = self.table_of(layer)
            target = f"{ws}:{name}"
            view = virtual_table(name, spec["sql"]) if "sql" in spec else None
            if view is None and not ingest.tables_exist(self.conn, schema, [table]):
                if dataset is None:
                    self.add("!", "layer", target, note=f"table {schema}.{table} doesn't exist; configops only publishes it")
                    self.unpublished.add(layer.name)
                    continue
                if dataset not in self.synced:
                    self.synced.add(dataset)
                    self.add("+", "table", f"{schema}.{table}", note=f"first sync of dataset {dataset}",
                             run=lambda d=dataset: self.first_sync(d))

            title = spec.get("title") or name
            wanted_ft = [("title", title), ("enabled", spec.get("enabled", True)), ("advertised", spec.get("advertised", True)),
                         ("nativeName", table)]
            if "abstract" in spec:
                wanted_ft.append(("abstract", spec["abstract"]))
            if "crs" in spec:
                wanted_ft.append(("crs", spec["crs"]))
            if view:
                wanted_ft.append(("sql", sql_fields(view)))
            if "attributes" in spec:
                wanted_ft.append(("attributes", list(spec["attributes"])))
            ft_path = f"workspaces/{ws}/datastores/{schema}/featuretypes/{name}"
            wanted_layer, settings = self.layer_settings(layer, (ft_path, "featureType"))

            def run(ws=ws, name=name, schema=schema, table=table, spec=spec, title=title, settings=settings, view=view,
                    ft_path=ft_path, old_store=None):
                if old_store and old_store != schema:
                    self.gs.delete_featuretype(ws, old_store, name)
                extra = {"metadata": {"entry": {"@key": "JDBC_VIRTUAL_TABLE", "virtualTable": view}}} if view else None
                self.gs.upsert_featuretype(ws, schema, name, table, title=title, abstract=spec.get("abstract"),
                                           keywords=spec.get("keywords"), enabled=spec.get("enabled", True),
                                           advertised=spec.get("advertised", True), srs=spec.get("crs"), extra=extra)
                if "attributes" in spec:
                    self.set_attributes(ft_path, spec["attributes"])
                settings()

            live_layer = self.gs.layer(ws, name)
            if live_layer is None:
                self.add("+", "layer", target, note=f"{schema}.{table}", run=run)
                continue
            store = self.store_of(live_layer)
            ft = (self.gs.featuretype(ws, store, name) or {}).get("featureType", {}) if store else {}
            live_view = next((e.get("virtualTable") for e in as_list((ft.get("metadata") or {}).get("entry"))
                              if isinstance(e, dict) and e.get("@key") == "JDBC_VIRTUAL_TABLE"), None)
            current = {
                "title": ft.get("title"), "enabled": ft.get("enabled", True), "advertised": ft.get("advertised", True),
                "nativeName": ft.get("nativeName"), "abstract": ft.get("abstract"),
                "crs": ft.get("srs") if ft.get("projectionPolicy") == "FORCE_DECLARED" else None,
                "sql": sql_fields(live_view) if live_view else None,
                "attributes": [a["name"] for a in as_list((ft.get("attributes") or {}).get("attribute")) if not is_geometry(a)],
                **self.live_layer_settings(layer, live_layer, ft),
            }
            changed = diff([(k, current[k], v) for k, v in wanted_ft + wanted_layer])
            if "keywords" in spec and names_of((ft.get("keywords") or {}).get("string")) != spec["keywords"]:
                changed.append(("keywords", names_of((ft.get("keywords") or {}).get("string")), spec["keywords"]))
            if store != schema:
                changed.append(("store", store, schema))
            if changed:
                self.add("~", "layer", target, changed, run=lambda run=run, store=store: run(old_store=store))

    def set_attributes(self, path: str, names: list[str]) -> None:
        """Publish only these attributes, and the geometry: clearing the list makes GeoServer list them all again."""
        self.gs.request("PUT", path, {"featureType": {"attributes": {"attribute": []}}})
        every = as_list(((self.gs.get(path) or {}).get("featureType") or {}).get("attributes", {}).get("attribute"))
        kept = [a for a in every if is_geometry(a) or a["name"] in names]
        kept.sort(key=lambda a: names.index(a["name"]) if a["name"] in names else -1)  # geometry first, then the files' order
        self.gs.request("PUT", path, {"featureType": {"attributes": {"attribute": kept}}})

    @staticmethod
    def store_of(live_layer: dict) -> str | None:
        """The store a live layer's feature type is in, from its link."""
        href = live_layer["layer"].get("resource", {}).get("href", "")
        return href.split("/datastores/", 1)[1].split("/", 1)[0] if "/datastores/" in href else None

    def delete_store(self, ws: str, name: str) -> None:
        """Remove a store of any kind, with what's published from it."""
        for kind, key in STORE_COLLECTIONS:
            if name in self.gs.names(f"workspaces/{ws}/{kind}", key):
                self.gs.delete(f"workspaces/{ws}/{kind}/{name}", {"recurse": "true"})
                return

    def delete_layer(self, ws: str, name: str) -> None:
        """Remove a layer and its feature type (GeoServer drops it from groups too)."""
        live = self.gs.layer(ws, name)
        store = self.store_of(live) if live else None
        if store:
            self.gs.delete_featuretype(ws, store, name)
        else:
            self.gs.delete(f"workspaces/{ws}/layers/{name}", {"recurse": "true"})

    def plan_workspace(self, ws: str, w: Resource) -> None:
        """A Workspace's namespace URI, isolation, and whether it's the default."""
        spec = w.spec
        live_ws = (self.gs.get(f"workspaces/{ws}") or {}).get("workspace") or {}
        live_ns = (self.gs.get(f"namespaces/{ws}") or {}).get("namespace") or {}
        default = ((self.gs.get("workspaces/default") or {}).get("workspace") or {}).get("name")
        current = {"namespace": live_ns.get("uri"), "isolated": live_ws.get("isolated", False), "default": default == ws}
        wanted = {k: spec[k] for k in ("namespace", "isolated", "default") if k in spec}
        changed = [(k, current[k], v) for k, v in wanted.items() if current[k] != v]
        if not changed:
            return

        def run():
            if "namespace" in wanted:
                self.gs.request("PUT", f"namespaces/{ws}", {"namespace": {"prefix": ws, "uri": wanted["namespace"]}})
            if "isolated" in wanted:
                self.gs.request("PUT", f"workspaces/{ws}", {"workspace": {"name": ws, "isolated": wanted["isolated"]}})
            if wanted.get("default"):
                self.gs.request("PUT", "workspaces/default", {"workspace": {"name": ws}})
        self.add("~", "workspace", ws, changed, run=run)

    def plan_groups(self) -> None:
        for name in self.group_order():
            group = self.groups[name]
            ws, gs_name, spec = self.workspace_of(group), geoserver_name(group), group.spec
            # Members that can't be published yet are left out: GeoServer drops them anyway, and
            # asking for them would show the group as changed on every plan.
            entries = [(self.published(member), entry.get("style") if isinstance(entry, dict) else None)
                       for (_, member), entry in zip(members(group), spec["layers"]) if member not in self.unpublished]
            if not entries:
                self.add("!", "group", f"{ws}:{gs_name}", note="none of its layers can be published yet")
                self.unpublished.add(name)
                continue
            cache = wanted_cache(spec.get("cache"))
            wanted = [("title", spec.get("title") or gs_name), ("mode", GROUP_MODES[spec.get("mode", "single")]),
                      ("layers", [n for (_, n), _ in entries]), ("styles", [self.style_ref(s) for _, s in entries]),
                      ("cache", cache)]
            if "abstract" in spec:
                wanted.append(("abstract", spec["abstract"]))
            if "keywords" in spec:
                wanted.append(("keywords", list(spec["keywords"])))
            root = None
            if spec.get("rootLayer") and spec["rootLayer"] not in self.unpublished:
                root = (self.published(spec["rootLayer"])[1], self.style_ref(spec.get("rootStyle")))
                wanted.append(("rootLayer", root[0]))
                if root[1]:
                    wanted.append(("rootStyle", root[1]))
            extra = {"enabled": spec.get("enabled", True), "advertised": spec.get("advertised", True)}
            links = details.wanted_resource({k: spec[k] for k in ("metadataLinks", *details.TRANSLATED) if k in spec})
            layer_fields = {k: spec[k] for k in ("attribution", "authorityURLs", "identifiers") if k in spec}
            if isinstance(layer_fields.get("attribution"), str):
                layer_fields["attribution"] = {"title": layer_fields["attribution"]}
            like_layer = details.wanted_layer(layer_fields)
            if "queryable" in spec:
                extra["queryDisabled"] = not spec["queryable"]
            wanted += list(extra.items()) + list(links.items()) + list(like_layer.items())

            def run(gs_name=gs_name, ws=ws, spec=spec, entries=entries, cache=cache, extra=extra, links=links, root=root,
                    like_layer=like_layer):
                if root and not root[1]:  # GeoServer wants a root style: the root layer's own
                    root_ws, root_name = root[0].split(":", 1)
                    root = (root[0], ((self.gs.layer(root_ws, root_name) or {}).get("layer", {}).get("defaultStyle") or {}).get("name"))
                self.gs.upsert_layergroup(gs_name, [(kind, n, self.style_ref(style)) for (kind, n), style in entries], ws=ws,
                                          title=spec.get("title"), abstract=spec.get("abstract"), mode=spec.get("mode", "single"),
                                          root=root, keywords=spec.get("keywords"))
                if extra or links or like_layer:
                    self.gs.request("PUT", f"workspaces/{ws}/layergroups/{gs_name}",
                                    {"layerGroup": {**extra, **details.patch_resource({}, links), **details.patch_layer({}, like_layer)}})
                set_cache(self.gs, f"{ws}:{gs_name}", cache, seed_of(spec))

            live = self.gs.layergroup(gs_name, ws)
            if live is None:
                self.add("+", "group", f"{ws}:{gs_name}", note=f"{len(entries)} members", run=run)
                continue
            g = live["layerGroup"]
            styles = [s.get("name") if isinstance(s, dict) and s.get("name") else None for s in as_list((g.get("styles") or {}).get("style"))]
            current = {"title": g.get("title"), "mode": g.get("mode"), "abstract": g.get("abstractTxt"),
                       "keywords": names_of((g.get("keywords") or {}).get("string")),
                       "rootLayer": (g.get("rootLayer") or {}).get("name"), "rootStyle": (g.get("rootLayerStyle") or {}).get("name"),
                       "layers": names_of((g.get("publishables") or {}).get("published")),
                       "styles": styles + [None] * (len(entries) - len(styles)),
                       "cache": cache_name(self.gs, f"{ws}:{gs_name}", cache),
                       "enabled": g.get("enabled", True), "advertised": g.get("advertised", True),
                       "queryDisabled": g.get("queryDisabled", False),
                       **details.live_resource(g, links), **details.live_layer(g, like_layer)}
            changed = diff([(k, current[k], v) for k, v in wanted])
            if changed:
                self.add("~", "group", f"{ws}:{gs_name}", changed, run=run)

    def group_order(self) -> list[str]:
        """Groups after the groups they hold (validate rules out loops)."""
        done: list[str] = []

        def visit(name: str) -> None:
            if name in done:
                return
            for _, member in members(self.groups[name]):
                if member in self.groups:
                    visit(member)
            done.append(name)

        for name in sorted(self.groups):
            visit(name)
        return done

    def plan_unmanaged(self, managed: list[str]) -> None:
        wanted = {
            "group": {f"{self.workspace_of(g)}:{geoserver_name(g)}" for g in self.groups.values()},
            "layer": {f"{self.workspace_of(layer)}:{geoserver_name(layer)}" for layer in self.layers.values()},
            "style": {f"{ws}:{name}" for name, (_, ws) in self.styles.items() if ws},
            "store": {f"{ws}:{schema}" for ws, schema in self.wanted_stores()}
                     | {f"{self.workspace_of(s)}:{geoserver_name(s)}" for s in self.files.of_kind("Store")},
        }
        for ws in managed:
            if self.gs.workspace(ws) is None:
                continue
            live = {
                "group": self.gs.names(f"workspaces/{ws}/layergroups", "layerGroup"),
                "layer": self.gs.names(f"workspaces/{ws}/layers", "layer"),
                "style": self.gs.names(f"workspaces/{ws}/styles", "style"),
                "store": [n for kind, key in STORE_COLLECTIONS for n in self.gs.names(f"workspaces/{ws}/{kind}", key)],
            }
            delete = {
                "group": lambda ws, n: self.gs.delete_layergroup(n, ws),
                "layer": self.delete_layer,
                "style": lambda ws, n: self.gs.delete_style(n, ws),
                "store": self.delete_store,
            }
            for kind in live:  # outermost first, as DELETE_ORDER
                for n in live[kind]:
                    if f"{ws}:{n}" in wanted[kind]:
                        continue
                    if self.prune:
                        self.add("-", kind, f"{ws}:{n}", note="not in the files", run=lambda k=kind, ws=ws, n=n, delete=delete: delete[k](ws, n))
                    else:
                        self.add("?", kind, f"{ws}:{n}", note="unmanaged; removed with --prune")

    # --- applying

    def first_sync(self, dataset: str) -> None:
        ingest.bootstrap(self.conn)  # the ingest log, on a database nothing has loaded into yet
        message, _ = sync.sync_dataset(self.conn, self.db, self.root, self.project, self.datasets[dataset], force=False)
        print("    " + message.replace("\n", "\n    "), flush=True)


def ordered(changes: list[Change]) -> list[Change]:
    def key(c: Change):
        if c.action == "-":
            return (1, DELETE_ORDER.index(c.kind))
        return (0, ORDER.index(c.kind) if c.kind in ORDER else len(ORDER))
    return sorted(changes, key=key)


def show(value) -> str:
    if isinstance(value, dict) and set(value) == {"gridsets", "formats"}:
        return f"cached ({', '.join(value['gridsets'])}; {', '.join(value['formats'])})"
    if value is False:
        return "False"
    return '"' + value + '"' if isinstance(value, str) else str(value)


def print_plan(changes: list[Change]) -> None:
    for c in changes:
        note = f"   ({c.note})" if c.note else ""
        print(f"  {c.action} {c.kind} {c.target}{note}")
        for name, live, wanted in c.fields:
            print(f"      {name}: {show(live)} -> {show(wanted)}")
    count = {a: sum(1 for c in changes if c.action == a) for a in "+~-?!"}
    summary = f"Plan: {count['+']} to add, {count['~']} to change, {count['-']} to delete, {count['?']} unmanaged."
    if count["!"]:
        summary += f" {count['!']} can't be done."
    print(("\n" if changes else "") + (summary if changes else "No changes: GeoServer matches the files."))


class PlanError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def connect(root: Path, geoserver_url: str | None, wait: float = 0):
    """
    (files, GeoServer, database connection, Database) for a valid project.
    With `wait`, the database and GeoServer get that many seconds to come up
    (a Job started alongside a fresh install starts before either).
    """
    files = load(root)
    errors = [p for p in validate(files) if p.severity == "error"]
    if errors:
        for p in errors[:10]:
            print(f"{p.file}:{p.line}: {p.resource + ': ' if p.resource else ''}{p.message}", file=sys.stderr)
        raise PlanError(f"the files have {len(errors)} error(s); `configops validate` lists them")
    project = files.of_kind("Project")[0]
    try:
        gs = for_project(project, root, geoserver_url)
    except MissingCredentials as e:
        raise PlanError(str(e)) from None
    db = sync.database(project, root)
    deadline = time.monotonic() + wait
    if wait:
        print(f"Waiting up to {wait:.0f}s for the database at {db.host}:{db.port} and GeoServer at {gs.url}...", flush=True)
    while True:
        try:
            conn = db.connect()
            break
        except psycopg2.OperationalError as e:
            reason = str(e).strip().splitlines()[0]
            if "password authentication failed" in reason or time.monotonic() + 5 > deadline:
                raise PlanError(f"can't connect to the database at {db.host}:{db.port}: {reason}", code=1) from None
            time.sleep(5)
    if wait:
        try:
            gs.wait(max(deadline - time.monotonic(), 0))
        except GeoServerError as e:
            conn.close()
            raise PlanError(str(e), code=1) from None
    return files, gs, conn, db


def run(root: Path, apply: bool = False, prune: bool = False, geoserver_url: str | None = None, wait: float = 0) -> int:
    files, gs, conn, db = connect(root, geoserver_url, wait)
    try:
        try:
            changes = ordered(Planner(files, root, gs, conn, db, prune).plan())
        except (GeoServerError, MissingCredentials) as e:
            raise PlanError(str(e), code=1) from None
        print_plan(changes)
        blocked = any(c.action == "!" for c in changes)
        todo = [c for c in changes if c.run is not None]
        if not apply:
            # 1 is kept for errors; 3 says only layers waiting for your own tables are left
            return 2 if todo else (3 if blocked else 0)
        if todo:
            print("\nApplying:")
        for c in todo:
            print(f"  {c.action} {c.kind} {c.target}", flush=True)
            try:
                c.run()
            except (GeoServerError, ingest.LoadError, SourceError, remote.RemoteError, discover.DiscoverError,
                    psycopg2.Error, OSError) as e:
                print(f"configops apply: {c.kind} {c.target} failed: {e}", file=sys.stderr)
                print("Nothing after it was applied; fix the cause and run apply again.", file=sys.stderr)
                return 1
        if todo:
            print(f"\nApplied {len(todo)} change(s).")
        if blocked:
            # Not a failure: the tables are yours to load, often after the deploy. plan exits 3 for them.
            print("Left for later: the layers marked ! wait for tables you load yourself; apply again once they exist.")
        return 0
    finally:
        conn.close()
