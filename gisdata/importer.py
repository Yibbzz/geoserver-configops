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

from . import secrets
from .geoserver import GROUP_MODES, GeoServer, GeoServerError, MissingCredentials, for_project
from .loader import default_table, load
from .plan import DEFAULT_FORMATS, DEFAULT_GRIDSETS, PlanError, as_list, names_of
from .validate import BUILT_IN_STYLES

API = "gisdata/v1alpha1"
RESOURCE_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SECRET = "geoserver-admin"  # the GeoServer secret a new project.yaml names
STYLE_SUFFIX = {"sld": ".sld", "css": ".css", "mbstyle": ".mbstyle"}
MODES = {v: k for k, v in GROUP_MODES.items()}
GEOMETRY_STYLE = {"Point": "point", "MultiPoint": "point", "LineString": "line", "MultiLineString": "line",
                  "Polygon": "polygon", "MultiPolygon": "polygon"}


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


def cache_setting(tile_layer: dict | None):
    if not tile_layer or not tile_layer.get("enabled", True):
        return None
    gridsets = sorted(g["gridSetName"] for g in as_list(tile_layer.get("gridSubsets")))
    formats = sorted(as_list(tile_layer.get("mimeFormats")))
    if gridsets == sorted(DEFAULT_GRIDSETS) and formats == sorted(DEFAULT_FORMATS):
        return True
    return {"gridsets": gridsets, "formats": formats}


def resource(kind: str, name: str, spec: dict) -> dict:
    return {"apiVersion": API, "kind": kind, "metadata": {"name": name}, "spec": spec}


class Importer:
    def __init__(self, gs: GeoServer, ws: str, project_ws: str | None, live_schema: str, dataset_tables: dict):
        self.gs, self.ws, self.project_ws, self.live_schema = gs, ws, project_ws, live_schema
        self.dataset_tables = dataset_tables  # table -> (dataset, key), from the project's Datasets
        self.notes: list[str] = []
        self.names: dict[str, str] = {}  # GeoServer name -> resource name, for group members
        self.styles: dict[str, tuple[str, str]] = {}  # style name -> (suffix, text)

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
        self.notes.append(f"global style {ref} isn't imported (only {self.ws}'s styles are); left out")
        return None

    def stores(self) -> dict[str, dict]:
        found = {}
        for name in self.gs.names(f"workspaces/{self.ws}/datastores", "dataStore"):
            store = self.gs.datastore(self.ws, name)["dataStore"]
            entries = {e["@key"]: e.get("$") for e in as_list((store.get("connectionParameters") or {}).get("entry"))}
            found[name] = entries
            if entries.get("schema") and name != entries["schema"]:
                self.notes.append(f"store {name}: apply moves its layers to a store named after their schema ({entries['schema']})")
            if not entries.get("jndiReferenceName"):
                self.notes.append(f"store {name} connects with a password; apply uses the project's JNDI datasource instead")
        return found

    def layers(self, stores: dict[str, dict]) -> list[dict]:
        out = []
        for gs_name in self.gs.names(f"workspaces/{self.ws}/layers", "layer"):
            layer = self.gs.layer(self.ws, gs_name)["layer"]
            href = (layer.get("resource") or {}).get("href", "")
            if "/datastores/" not in href:
                self.notes.append(f"layer {gs_name} isn't a table (a raster or cascaded layer); left out")
                continue
            store = href.split("/datastores/", 1)[1].split("/", 1)[0]
            ft = self.gs.featuretype(self.ws, store, gs_name)["featureType"]
            schema, table = stores.get(store, {}).get("schema") or "public", ft["nativeName"]
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
            if cache is True:
                spec["cache"] = True
            elif cache:
                self.notes.append(f"group {gs_name}: tile cache with its own gridsets or formats; groups only take cache: true")
                spec["cache"] = True
            found.append(resource("LayerGroup", name, spec))
        return found

    def workspace_styles(self) -> None:
        for name in self.gs.names(f"workspaces/{self.ws}/styles", "style"):
            info = self.gs.style(name, self.ws)["style"]
            suffix = STYLE_SUFFIX.get(info.get("format", "sld"))
            if suffix is None:
                self.notes.append(f"style {name}: format {info.get('format')} isn't supported; left out")
                continue
            self.styles[name] = (suffix, self.gs.style_body(name, self.ws))


def dump(resources: list[dict]) -> str:
    return "\n---\n".join(yaml.safe_dump(r, sort_keys=False, allow_unicode=True, width=120).rstrip() for r in resources) + "\n"


def run(target: Path, ws: str, geoserver_url: str | None = None, force: bool = False) -> int:
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

    try:
        if gs.workspace(ws) is None:
            raise PlanError(f"GeoServer has no workspace {ws}", code=1)
        imp = Importer(gs, ws, project_ws, live, dataset_tables)
        stores = imp.stores()
        layers = imp.layers(stores)
        groups = imp.groups()
        imp.workspace_styles()
    except GeoServerError as e:
        raise PlanError(str(e), code=1) from None

    files: dict[Path, str] = {target / f"{ws}.yaml": dump(layers + groups)}
    for name, (suffix, text) in imp.styles.items():
        files[target / "styles" / f"{name}{suffix}"] = text if text.endswith("\n") else text + "\n"
    if project is None:
        jndi = next((e["jndiReferenceName"] for e in stores.values() if e.get("jndiReferenceName")), "java:comp/env/jdbc/mypostgis")
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

    linked = sum(1 for r in layers if "dataset" in r["spec"])
    print(f"Wrote {ws}.yaml: {len(layers)} layer(s) ({linked} on a Dataset's table), {len(groups)} group(s); "
          f"{len(imp.styles)} style(s) in styles/" + ("; project.yaml" if project is None else ""))
    for note in dict.fromkeys(imp.notes):
        print(f"  note: {note}")
    print(f"Next: gisdata validate {target} && gisdata plan -C {target}")
    return 0
