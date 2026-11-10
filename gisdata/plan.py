"""`gisdata plan` and `gisdata apply`: make GeoServer match the files.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import psycopg2

from . import discover, ingest, remote, sync
from .geoserver import GROUP_MODES, GeoServer, GeoServerError, MissingCredentials, for_project
from .loader import Project, Resource, default_table, load
from .sources import SourceError
from .validate import BUILT_IN_STYLES, STYLE_SUFFIXES, geoserver_name, members, validate

# Apply order: what a change needs is made first; deletions come last, outermost first
ORDER = ["workspace", "store", "style", "table", "layer", "group"]
DELETE_ORDER = ["group", "layer", "style", "store"]

# `cache: true`: what GeoServer itself would cache
DEFAULT_GRIDSETS = ["EPSG:4326", "EPSG:900913"]
DEFAULT_FORMATS = ["image/png"]

# Attribution a source's licence asks for, when a layer's file gives none
SOURCE_ATTRIBUTION = {"os-downloads": "Contains OS data © Crown copyright and database right {year}"}


@dataclass
class Change:
    action: str  # + add, ~ change, - delete, ? unmanaged (left alone), ! can't be done
    kind: str  # workspace, store, style, table, layer, group
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


def wanted_cache(setting) -> dict | bool:
    """The spec's `cache` as {gridsets, formats}, or False for no cache."""
    if not setting:
        return False
    if setting is True:
        setting = {}
    return {"gridsets": sorted(setting.get("gridsets", DEFAULT_GRIDSETS)), "formats": sorted(setting.get("formats", DEFAULT_FORMATS))}


def live_cache(tile_layer: dict | None) -> dict | bool:
    if not tile_layer or not tile_layer.get("enabled", True):
        return False
    return {"gridsets": sorted(g["gridSetName"] for g in as_list(tile_layer.get("gridSubsets"))),
            "formats": sorted(as_list(tile_layer.get("mimeFormats")))}


def set_cache(gs: GeoServer, name: str, wanted: dict | bool) -> None:
    """Cache `name` as wanted; no cache drops the one GeoServer makes for every new layer."""
    if wanted:
        gs.put_tile_layer(name, wanted["gridsets"], wanted["formats"])
    else:
        gs.delete_tile_layer(name)


def diff(pairs: list[tuple[str, object, object]]) -> list[tuple[str, object, object]]:
    return [(name, live, wanted) for name, live, wanted in pairs if live != wanted]


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
            found[name] = (self.root / s.spec["file"], self.workspace_of(s))
        return found

    def style_ref(self, name: str | None) -> str | None:
        """A style as a layer refers to it: workspace styles qualified, GeoServer's built-in ones not."""
        if name is None or name in BUILT_IN_STYLES and name not in self.styles:
            return name
        return f"{self.styles[name][1]}:{name}"

    def table_of(self, layer: Resource) -> tuple[str, str, str | None]:
        """(schema, table, dataset) a layer publishes."""
        table = layer.spec["table"]
        if isinstance(table, dict):
            return table["schema"], table["name"], None
        dataset = self.datasets[layer.spec["dataset"]]
        options = (dataset.spec.get("tables") or {}).get(table) or {}
        return self.db.live, options.get("name") or default_table(dataset.name, table), dataset.name

    def attribution(self, layer: Resource) -> str | None:
        """The layer's own attribution, else what its dataset's source asks for."""
        if "attribution" in layer.spec:
            return layer.spec["attribution"]
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
        managed = sorted({self.ws} | {self.workspace_of(r) for r in [*self.layers.values(), *self.groups.values(),
                                                                       *self.files.named("Style").values()]})
        for ws in managed:
            if self.gs.workspace(ws) is None:
                self.add("+", "workspace", ws, run=lambda ws=ws: self.gs.upsert_workspace(ws))
        self.plan_stores()
        self.plan_styles()
        self.plan_layers()
        self.plan_groups()
        self.plan_unmanaged(managed)
        return self.changes

    def wanted_stores(self) -> dict[tuple[str, str], None]:
        """(workspace, schema) of every store the layers need; a store per schema, named after it."""
        return {(self.workspace_of(layer), self.table_of(layer)[0]): None for layer in self.layers.values()}

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
        for name, (path, ws) in self.styles.items():
            text = path.read_text(encoding="utf-8")
            live = self.gs.style_body(name, ws)
            run = (lambda name=name, text=text, suffix=path.suffix, ws=ws: self.gs.upsert_style(name, text, suffix, ws))
            if live is None:
                self.add("+", "style", f"{ws}:{name}", note=str(path.relative_to(self.root)), run=run)
            elif live.replace("\r\n", "\n").strip() != text.replace("\r\n", "\n").strip():
                self.add("~", "style", f"{ws}:{name}", note=f"{path.relative_to(self.root)} differs from GeoServer's copy", run=run)

    def plan_layers(self) -> None:
        for layer in self.layers.values():
            ws, name, spec = self.workspace_of(layer), geoserver_name(layer), layer.spec
            schema, table, dataset = self.table_of(layer)
            target = f"{ws}:{name}"
            if not ingest.tables_exist(self.conn, schema, [table]):
                if dataset is None:
                    self.add("!", "layer", target, note=f"table {schema}.{table} doesn't exist; gisdata only publishes it")
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
            attribution = self.attribution(layer)
            cache = wanted_cache(spec.get("cache"))
            wanted_layer = [("queryable", spec.get("queryable", True)), ("cache", cache)]
            if attribution is not None:
                wanted_layer.append(("attribution", attribution))
            if spec.get("style"):
                wanted_layer.append(("style", self.style_ref(spec["style"])))
            if "styles" in spec:
                wanted_layer.append(("styles", [self.style_ref(s) for s in spec["styles"]]))

            def run(ws=ws, name=name, schema=schema, table=table, spec=spec, title=title, attribution=attribution,
                    cache=cache, old_store=None):
                if old_store and old_store != schema:
                    self.gs.delete_featuretype(ws, old_store, name)
                self.gs.upsert_featuretype(ws, schema, name, table, title=title, abstract=spec.get("abstract"),
                                           keywords=spec.get("keywords"), enabled=spec.get("enabled", True),
                                           advertised=spec.get("advertised", True))
                self.gs.update_layer(ws, name, style=self.style_ref(spec.get("style")),
                                     styles=[self.style_ref(s) for s in spec["styles"]] if "styles" in spec else None,
                                     queryable=spec.get("queryable", True), attribution=attribution)
                set_cache(self.gs, f"{ws}:{name}", cache)

            live_layer = self.gs.layer(ws, name)
            if live_layer is None:
                self.add("+", "layer", target, note=f"{schema}.{table}", run=run)
                continue
            store = self.store_of(live_layer)
            ft = (self.gs.featuretype(ws, store, name) or {}).get("featureType", {}) if store else {}
            live = live_layer["layer"]
            current = {
                "title": ft.get("title"), "enabled": ft.get("enabled", True), "advertised": ft.get("advertised", True),
                "nativeName": ft.get("nativeName"), "abstract": ft.get("abstract"),
                "queryable": live.get("queryable", True),
                "style": (live.get("defaultStyle") or {}).get("name"),
                "styles": names_of((live.get("styles") or {}).get("style")),
                "attribution": (live.get("attribution") or {}).get("title"),
                "cache": live_cache(self.gs.tile_layer(target)),
            }
            changed = diff([(k, current[k], v) for k, v in wanted_ft + wanted_layer])
            if "keywords" in spec and names_of((ft.get("keywords") or {}).get("string")) != spec["keywords"]:
                changed.append(("keywords", names_of((ft.get("keywords") or {}).get("string")), spec["keywords"]))
            if store != schema:
                changed.append(("store", store, schema))
            if changed:
                self.add("~", "layer", target, changed, run=lambda run=run, store=store: run(old_store=store))

    @staticmethod
    def store_of(live_layer: dict) -> str | None:
        """The store a live layer's feature type is in, from its link."""
        href = live_layer["layer"].get("resource", {}).get("href", "")
        return href.split("/datastores/", 1)[1].split("/", 1)[0] if "/datastores/" in href else None

    def delete_layer(self, ws: str, name: str) -> None:
        """Remove a layer and its feature type (GeoServer drops it from groups too)."""
        live = self.gs.layer(ws, name)
        store = self.store_of(live) if live else None
        if store:
            self.gs.delete_featuretype(ws, store, name)
        else:
            self.gs.delete(f"workspaces/{ws}/layers/{name}", {"recurse": "true"})

    def plan_groups(self) -> None:
        for name in self.group_order():
            group = self.groups[name]
            ws, gs_name, spec = self.workspace_of(group), geoserver_name(group), group.spec
            entries = [(self.published(member), entry.get("style") if isinstance(entry, dict) else None)
                       for (_, member), entry in zip(members(group), spec["layers"])]
            cache = wanted_cache(spec.get("cache"))
            wanted = [("title", spec.get("title") or gs_name), ("mode", GROUP_MODES[spec.get("mode", "single")]),
                      ("layers", [n for (_, n), _ in entries]), ("styles", [self.style_ref(s) for _, s in entries]),
                      ("cache", cache)]
            if "abstract" in spec:
                wanted.append(("abstract", spec["abstract"]))
            def run(gs_name=gs_name, ws=ws, spec=spec, entries=entries, cache=cache):
                self.gs.upsert_layergroup(gs_name, [(kind, n, self.style_ref(style)) for (kind, n), style in entries], ws=ws,
                                          title=spec.get("title"), abstract=spec.get("abstract"), mode=spec.get("mode", "single"))
                set_cache(self.gs, f"{ws}:{gs_name}", cache)

            live = self.gs.layergroup(gs_name, ws)
            if live is None:
                self.add("+", "group", f"{ws}:{gs_name}", note=f"{len(entries)} members", run=run)
                continue
            g = live["layerGroup"]
            styles = [s.get("name") if isinstance(s, dict) and s.get("name") else None for s in as_list((g.get("styles") or {}).get("style"))]
            current = {"title": g.get("title"), "mode": g.get("mode"), "abstract": g.get("abstractTxt"),
                       "layers": names_of((g.get("publishables") or {}).get("published")),
                       "styles": styles + [None] * (len(entries) - len(styles)),
                       "cache": live_cache(self.gs.tile_layer(f"{ws}:{gs_name}"))}
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
            "style": {f"{ws}:{name}" for name, (_, ws) in self.styles.items()},
            "store": {f"{ws}:{schema}" for ws, schema in self.wanted_stores()},
        }
        for ws in managed:
            if self.gs.workspace(ws) is None:
                continue
            live = {
                "group": self.gs.names(f"workspaces/{ws}/layergroups", "layerGroup"),
                "layer": self.gs.names(f"workspaces/{ws}/layers", "layer"),
                "style": self.gs.names(f"workspaces/{ws}/styles", "style"),
                "store": self.gs.names(f"workspaces/{ws}/datastores", "dataStore"),
            }
            delete = {
                "group": lambda ws, n: self.gs.delete_layergroup(n, ws),
                "layer": self.delete_layer,
                "style": lambda ws, n: self.gs.delete_style(n, ws),
                "store": lambda ws, n: self.gs.delete_datastore(ws, n),
            }
            for kind in DELETE_ORDER:
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


def connect(root: Path, geoserver_url: str | None):
    """(files, GeoServer, database connection, Database) for a valid project."""
    files = load(root)
    errors = [p for p in validate(files) if p.severity == "error"]
    if errors:
        for p in errors[:10]:
            print(f"{p.file}:{p.line}: {p.resource + ': ' if p.resource else ''}{p.message}", file=sys.stderr)
        raise PlanError(f"the files have {len(errors)} error(s); `gisdata validate` lists them")
    project = files.of_kind("Project")[0]
    try:
        gs = for_project(project, root, geoserver_url)
    except MissingCredentials as e:
        raise PlanError(str(e)) from None
    db = sync.database(project, root)
    try:
        conn = db.connect()
    except psycopg2.OperationalError as e:
        raise PlanError(f"can't connect to the database at {db.host}:{db.port}: {str(e).strip().splitlines()[0]}", code=1) from None
    return files, gs, conn, db


def run(root: Path, apply: bool = False, prune: bool = False, geoserver_url: str | None = None, wait: float = 0) -> int:
    files, gs, conn, db = connect(root, geoserver_url)
    if wait:
        print(f"Waiting up to {wait:.0f}s for GeoServer at {gs.url}...", flush=True)
        try:
            gs.wait(wait)
        except GeoServerError as e:
            conn.close()
            raise PlanError(str(e), code=1) from None
    try:
        try:
            changes = ordered(Planner(files, root, gs, conn, db, prune).plan())
        except GeoServerError as e:
            raise PlanError(str(e), code=1) from None
        print_plan(changes)
        blocked = any(c.action == "!" for c in changes)
        todo = [c for c in changes if c.run is not None]
        if not apply:
            return 1 if blocked else (2 if todo else 0)
        if todo:
            print("\nApplying:")
        for c in todo:
            print(f"  {c.action} {c.kind} {c.target}", flush=True)
            try:
                c.run()
            except (GeoServerError, ingest.LoadError, SourceError, remote.RemoteError, discover.DiscoverError,
                    psycopg2.Error, OSError) as e:
                print(f"gisdata apply: {c.kind} {c.target} failed: {e}", file=sys.stderr)
                print("Nothing after it was applied; fix the cause and run apply again.", file=sys.stderr)
                return 1
        if todo:
            print(f"\nApplied {len(todo)} change(s).")
        return 1 if blocked else 0
    finally:
        conn.close()
