"""Check a project's files against the spec, without touching the network.

Two passes: each resource against the JSON Schema (configops/schema/v1alpha1.json),
then the rules that span files: references between resources, style files,
names that would clash in GeoServer or PostGIS, and layer groups that contain
themselves.
"""

from __future__ import annotations

import difflib
import json
from collections import defaultdict
from importlib import resources as package_files
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from .loader import Problem, Project, Resource, default_table

BUILT_IN_STYLES = {"generic", "line", "point", "polygon", "raster"}
STYLE_SUFFIXES = {".sld", ".mbstyle", ".css"}
SOURCES_WITH_PRODUCTS = {"os-downloads", "ckan", "ogc-api-features", "wfs"}
SOURCES_WITH_ADDRESSES = {"ckan", "ogc-api-features", "wfs"}  # need the Source's spec.url


def schema() -> dict:
    return json.loads(package_files.files("configops").joinpath("schema/v1alpha1.json").read_text())


def geoserver_name(r: Resource) -> str:
    return r.spec.get("name") or (r.name or "").replace("-", "_")


def validate(project: Project) -> list[Problem]:
    problems = list(project.problems)
    problems += check_schema(project)
    problems += check_project(project)
    problems += check_references(project)
    problems += check_styles(project)
    problems += check_groups(project)
    problems += check_names(project)
    problems += check_unused(project)
    problems += check_settings(project)
    problems += check_security(project)
    problems += check_stores(project)
    problems += check_rasters(project)
    problems += check_workspaces_and_styles(project)
    problems += check_blob_stores(project)
    return sorted(problems, key=lambda p: (str(p.file), p.line, p.severity, p.message))


# --- pass 1: each resource on its own -------------------------------------------------


def check_schema(project: Project) -> list[Problem]:
    validator = Draft202012Validator(schema())
    problems = []
    for r in project.resources:
        if not isinstance(r.data, dict):
            problems.append(Problem(r.file, r.line(), "a resource must be a mapping with apiVersion, kind, metadata and spec"))
            continue
        seen = set()
        for error in validator.iter_errors(r.data):
            if error.validator in ("if", "allOf"):
                continue  # the specific errors inside are reported on their own
            for path, text in explain(error):
                message = f"{dotted(path)}: {text}" if path else text
                if message not in seen:
                    seen.add(message)
                    problems.append(Problem(r.file, r.line(path), message, r.label))
    return problems


def explain(error) -> list[tuple[tuple, str]]:
    """(path, message) pairs in plain words for a schema error."""
    path = tuple(error.absolute_path)
    description = error.schema.get("description") if isinstance(error.schema, dict) else None
    if error.context:  # oneOf / anyOf
        if description:
            return [(path, f"{error.instance!r} isn't allowed: use {description}")]
        return explain(best_match(error.context))
    if error.validator == "additionalProperties" and isinstance(error.instance, dict):
        allowed = list((error.schema.get("properties") or {}).keys())
        out = []
        for key in sorted(set(error.instance) - set(allowed)):
            close = difflib.get_close_matches(str(key), allowed, n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else f" (allowed: {', '.join(allowed)})"
            out.append((path + (key,), f"unknown field {key!r}{hint}"))
        return out
    if error.validator == "pattern" and description:
        return [(path, f"{error.instance!r} isn't allowed: use {description}")]
    if error.validator == "enum":
        return [(path, f"{error.instance!r} isn't one of: {', '.join(map(str, error.validator_value))}")]
    if error.validator == "required":
        return [(path, error.message.replace("is a required property", "is required"))]
    return [(path, error.message)]


def dotted(path: tuple) -> str:
    out = ""
    for step in path:
        out += f"[{step}]" if isinstance(step, int) else (f".{step}" if out else str(step))
    return out


# --- pass 2: rules across files ----------------------------------------------------------


def check_project(project: Project) -> list[Problem]:
    problems = []
    projects = project.of_kind("Project")
    if not projects:
        problems.append(Problem(project.root, 0, "no Project resource: every directory needs exactly one"))
    for extra in projects[1:]:
        problems.append(Problem(extra.file, extra.line(), "a second Project: every directory has exactly one", extra.label))

    for kind in ("Project", "Source", "Dataset", "Layer", "LayerGroup", "Style", "Settings", "Service", "Role", "Group", "User", "AccessRule", "Store", "Workspace", "Gridset", "BlobStore"):
        first: dict[str, Resource] = {}
        for r in project.of_kind(kind):
            if r.name in first and kind != "Project":
                other = first[r.name]
                problems.append(Problem(r.file, r.line(("metadata", "name")),
                                        f"{kind} name already used in {other.file.name}:{other.line()}", r.label))
            first.setdefault(r.name, r)
    return problems


def check_references(project: Project) -> list[Problem]:
    problems = []
    sources, datasets = project.named("Source"), project.named("Dataset")

    for s in project.of_kind("Source"):
        kind = s.spec.get("type")
        if kind in SOURCES_WITH_ADDRESSES and not s.spec.get("url"):
            problems.append(Problem(s.file, s.line(("spec",)), f"spec.url: a {kind} source needs its address, e.g. https://data.gov.uk", s.label))
        if kind not in SOURCES_WITH_ADDRESSES and s.spec.get("url"):
            problems.append(Problem(s.file, s.line(("spec", "url")), f"spec.url: not used by a {kind} source", s.label))

    for d in project.of_kind("Dataset"):
        source = sources.get(d.spec.get("source"))
        if d.spec.get("source") and not source:
            problems.append(Problem(d.file, d.line(("spec", "source")), f"spec.source: no Source named {d.spec['source']!r}{known(sources)}", d.label))
        if not source:
            continue
        kind = source.spec.get("type")
        if kind in SOURCES_WITH_PRODUCTS and not d.spec.get("product"):
            problems.append(Problem(d.file, d.line(("spec",)), f"spec.product: required for a {kind} source", d.label))
        if kind == "url" and not d.spec.get("url"):
            problems.append(Problem(d.file, d.line(("spec",)), "spec.url: required for a url source", d.label))
        if kind != "url" and d.spec.get("url"):
            problems.append(Problem(d.file, d.line(("spec", "url")), f"spec.url: only used with a url source, not {kind}", d.label))
        if kind != "ckan" and d.spec.get("resource"):
            problems.append(Problem(d.file, d.line(("spec", "resource")), f"spec.resource: only used with a ckan source, not {kind}", d.label))

    for layer in project.of_kind("Layer"):
        name = layer.spec.get("dataset")
        if not name:
            continue
        dataset = datasets.get(name)
        if not dataset:
            problems.append(Problem(layer.file, layer.line(("spec", "dataset")), f"spec.dataset: no Dataset named {name!r}{known(datasets)}", layer.label))
            continue
        tables = dataset.spec.get("tables")
        table = layer.spec.get("table")
        if tables and isinstance(table, str) and table not in tables:
            problems.append(Problem(layer.file, layer.line(("spec", "table")),
                                    f"spec.table: dataset {name} has no table {table!r} (its tables: {', '.join(tables)})", layer.label))
    return problems


def style_names(project: Project) -> tuple[set[str], list[Problem]]:
    """Every style a layer may use, and problems with style files and Style resources."""
    problems = []
    by_stem = defaultdict(list)
    styles_dir = project.root / "styles"
    if styles_dir.is_dir():
        for f in sorted(styles_dir.iterdir()):
            if f.is_file() and f.suffix in STYLE_SUFFIXES:
                by_stem[f.stem].append(f)
    for stem, files in by_stem.items():
        if len(files) > 1:
            problems.append(Problem(files[1], 0, f"two style files named {stem} ({', '.join(f.name for f in files)}); rename one"))
    for s in project.of_kind("Style"):
        f = s.spec.get("file")
        if isinstance(f, str) and not (project.root / f).is_file():
            problems.append(Problem(s.file, s.line(("spec", "file")), f"spec.file: {f} not found under {project.root}", s.label))
    return set(by_stem) | set(project.named("Style")) | BUILT_IN_STYLES, problems


def check_styles(project: Project) -> list[Problem]:
    names, problems = style_names(project)

    def check(r: Resource, path: tuple, style) -> None:
        if isinstance(style, str) and style not in names:
            problems.append(Problem(r.file, r.line(path), f"{dotted(path)}: no style named {style!r} (a file in styles/, a Style resource, or one of GeoServer's built-in styles)", r.label))

    for layer in project.of_kind("Layer"):
        check(layer, ("spec", "style"), layer.spec.get("style"))
        for i, style in enumerate(layer.spec.get("styles") or []):
            check(layer, ("spec", "styles", i), style)
    for group in project.of_kind("LayerGroup"):
        for i, entry in enumerate(group.spec.get("layers") or []):
            if isinstance(entry, dict):
                check(group, ("spec", "layers", i, "style"), entry.get("style"))
    return problems


def members(group: Resource):
    for i, entry in enumerate(group.spec.get("layers") or []):
        yield i, entry if isinstance(entry, str) else (entry or {}).get("layer")


def check_groups(project: Project) -> list[Problem]:
    problems = []
    layers, groups = project.named("Layer"), project.named("LayerGroup")
    for group in project.of_kind("LayerGroup"):
        for i, member in members(group):
            if member and member not in layers and member not in groups:
                problems.append(Problem(group.file, group.line(("spec", "layers", i)),
                                        f"spec.layers[{i}]: no Layer or LayerGroup named {member!r}", group.label))

    def path_back(start: str, target: str, seen: tuple) -> tuple | None:
        for _, member in members(groups[start]):
            if member == target:
                return seen + (member,)
            if member in groups and member not in seen:
                found = path_back(member, target, seen + (member,))
                if found:
                    return found
        return None

    for name, group in groups.items():
        loop = path_back(name, name, (name,))
        if loop:
            problems.append(Problem(group.file, group.line(("spec", "layers")), f"spec.layers: the group contains itself ({' -> '.join(loop)})", group.label))
    return problems


def check_names(project: Project) -> list[Problem]:
    """Names that would clash once applied: GeoServer layer names, and live tables."""
    problems = []
    projects = project.of_kind("Project")
    default_ws = projects[0].spec.get("workspace") if projects else None

    published: dict[tuple, Resource] = {}
    for r in project.of_kind("Layer") + project.of_kind("LayerGroup"):
        key = (r.spec.get("workspace") or default_ws, geoserver_name(r))
        if key in published:
            other = published[key]
            problems.append(Problem(r.file, r.line(("metadata", "name")),
                                    f"GeoServer name {key[0]}:{key[1]} is also used by {other.label} ({other.file.name}:{other.line()})", r.label))
        published.setdefault(key, r)

    tables: dict[str, Resource] = {}
    for d in project.of_kind("Dataset"):
        for key, options in (d.spec.get("tables") or {}).items():
            table = (options or {}).get("name") or default_table(d.name or "", key)
            if table in tables and tables[table] is not d:
                other = tables[table]
                problems.append(Problem(d.file, d.line(("spec", "tables", key)),
                                        f"spec.tables.{key}: table {table} is also loaded by {other.label}", d.label))
            tables.setdefault(table, d)
    return problems


def check_unused(project: Project) -> list[Problem]:
    """Warnings: things declared but never used."""
    problems = []
    used_sources = {d.spec.get("source") for d in project.of_kind("Dataset")}
    for s in project.of_kind("Source"):
        if s.name not in used_sources:
            problems.append(Problem(s.file, s.line(), "no Dataset uses this source", s.label, "warning"))
    published = {layer.spec.get("dataset") for layer in project.of_kind("Layer")} | {s.spec.get("dataset") for s in project.of_kind("Store")}
    for d in project.of_kind("Dataset"):
        if d.name not in published:
            problems.append(Problem(d.file, d.line(), "loaded but no Layer publishes it", d.label, "warning"))
    return problems


def known(named: dict) -> str:
    return f" (known: {', '.join(sorted(named))})" if named else ""


def check_settings(project: Project) -> list[Problem]:
    """One Settings for the global settings and one per workspace; `global` only on the global one."""
    problems = []
    first: dict[str | None, Resource] = {}
    for r in project.of_kind("Settings"):
        ws = r.spec.get("workspace")
        where = f"workspace {ws}" if ws else "the global settings"
        if ws in first:
            other = first[ws]
            problems.append(Problem(r.file, r.line(("spec",)), f"a second Settings for {where} (the first is in {other.file.name}:{other.line()})", r.label))
        first.setdefault(ws, r)
        if ws and "global" in r.spec:
            problems.append(Problem(r.file, r.line(("spec", "global")), "spec.global: only the global Settings (no workspace) has these", r.label))
    return problems


def check_security(project: Project) -> list[Problem]:
    """Built-ins left to GeoServer, references between users, groups and roles, one rule per key."""
    from .security import BUILT_IN_ROLES, BUILT_IN_USERS, account_name, role_name, rule_key

    problems = []
    roles = {role_name(r) for r in project.of_kind("Role")} | BUILT_IN_ROLES
    groups = {account_name(r) for r in project.of_kind("Group")}

    def check_roles(r: Resource, path: tuple, allow_everyone: bool = False) -> None:
        for i, role in enumerate(r.spec.get("roles") or []):
            if role == "*" and allow_everyone:
                continue
            if role not in roles:
                problems.append(Problem(r.file, r.line(path + (i,)), f"{dotted(path)}[{i}]: no Role named {role!r} (roles: {', '.join(sorted(roles))})", r.label))

    for r in project.of_kind("Role"):
        if role_name(r) in BUILT_IN_ROLES:
            problems.append(Problem(r.file, r.line(), f"{role_name(r)} is GeoServer's own role; use it without defining it", r.label))
    for r in project.of_kind("Group"):
        check_roles(r, ("spec", "roles"))
    for r in project.of_kind("User"):
        if account_name(r) in BUILT_IN_USERS:
            problems.append(Problem(r.file, r.line(), "admin is GeoServer's own user, and configops logs in as it: change its password in GeoServer and the geoserver-admin Secret", r.label))
        check_roles(r, ("spec", "roles"))
        for i, group in enumerate(r.spec.get("groups") or []):
            if group not in groups:
                problems.append(Problem(r.file, r.line(("spec", "groups", i)), f"spec.groups[{i}]: no Group named {group!r}{known({g: None for g in groups})}", r.label))
    first: dict[tuple[str, str], Resource] = {}
    for r in project.of_kind("AccessRule"):
        if sum(k in r.spec for k in ("layer", "service", "rest")) != 1:
            continue  # the schema reports it
        check_roles(r, ("spec", "roles"), allow_everyone=True)
        key = rule_key(r)
        if key in first:
            other = first[key]
            problems.append(Problem(r.file, r.line(("spec",)), f"the same rule as AccessRule {other.name} ({key[0]} {key[1]})", r.label))
        first.setdefault(key, r)
    return problems


STORES_WITH_ADDRESSES = {"cog", "wms", "wmts", "wfs"}
STORES_WITH_PATHS = {"geopackage", "shapefile", "shapefiles", "flatgeobuf", "geotiff", "worldimage", "arcgrid", "imagemosaic"}


def check_stores(project: Project) -> list[Problem]:
    """Each store has what its type needs; a layer on a store names one that exists, and nothing else to publish."""
    problems = []
    stores = project.named("Store")
    for s in project.of_kind("Store"):
        kind = s.spec.get("type")
        if kind in STORES_WITH_ADDRESSES and not s.spec.get("url"):
            problems.append(Problem(s.file, s.line(("spec",)), f"spec.url: a {kind} store needs its address", s.label))
        if kind in STORES_WITH_ADDRESSES and s.spec.get("path"):
            problems.append(Problem(s.file, s.line(("spec", "path")), f"spec.path: a {kind} store is read from spec.url", s.label))
        if kind in STORES_WITH_PATHS and not s.spec.get("path") and not s.spec.get("dataset"):
            problems.append(Problem(s.file, s.line(("spec",)), f"spec.path: a {kind} store needs its file's path on the data volume, e.g. rasters/surrey.tif", s.label))
        if kind in STORES_WITH_PATHS and s.spec.get("url"):
            problems.append(Problem(s.file, s.line(("spec", "url")), f"spec.url: a {kind} store reads a file on the data volume: use spec.path", s.label))
        if s.spec.get("dataset"):
            dataset = project.named("Dataset").get(s.spec["dataset"])
            if dataset is None:
                problems.append(Problem(s.file, s.line(("spec", "dataset")), f"spec.dataset: no Dataset named {s.spec['dataset']!r}{known(project.named('Dataset'))}", s.label))
            elif "raster" not in dataset.spec:
                problems.append(Problem(s.file, s.line(("spec", "dataset")), f"spec.dataset: {dataset.name} loads tables, not a raster: publish its tables with Layers", s.label))
            if kind != "geotiff":
                problems.append(Problem(s.file, s.line(("spec", "type")), "spec.type: a store on a raster Dataset is a geotiff", s.label))
            for key in ("path", "url"):
                if key in s.spec:
                    problems.append(Problem(s.file, s.line(("spec", key)), f"spec.{key}: a store on a raster Dataset reads the Dataset's file", s.label))
            continue
        if kind == "pmtiles" and bool(s.spec.get("path")) == bool(s.spec.get("url")):
            problems.append(Problem(s.file, s.line(("spec",)), "a pmtiles store needs spec.path (on the data volume) or spec.url, not both", s.label))
        if kind == "postgis":
            missing = [k for k in ("host", "database") if k not in (s.spec.get("connection") or {})]
            if missing:
                problems.append(Problem(s.file, s.line(("spec",)), f"spec.connection: a postgis store needs {', '.join(missing)}", s.label))
            if s.spec.get("url"):
                problems.append(Problem(s.file, s.line(("spec", "url")), "spec.url: a postgis store connects with spec.connection", s.label))
    for layer in project.of_kind("Layer"):
        name = layer.spec.get("store")
        if name is None:
            if "resource" in layer.spec:
                problems.append(Problem(layer.file, layer.line(("spec", "resource")), "spec.resource: only for a layer on a store", layer.label))
            continue
        if name not in stores:
            problems.append(Problem(layer.file, layer.line(("spec", "store")), f"spec.store: no Store named {name!r}{known(stores)}", layer.label))
        for key in ("dataset", "table"):
            if key in layer.spec:
                problems.append(Problem(layer.file, layer.line(("spec", key)), f"spec.{key}: a layer on a store doesn't take a {key}", layer.label))
    for layer in project.of_kind("Layer"):
        if "sql" in layer.spec:
            for key in ("dataset", "table", "store"):
                if key in layer.spec:
                    problems.append(Problem(layer.file, layer.line(("spec", key)), f"spec.{key}: a SQL view doesn't take a {key}", layer.label))
    return problems


def check_rasters(project: Project) -> list[Problem]:
    """A Dataset loads tables or a raster, not both; no two raster Datasets write one file."""
    problems, paths = [], {}
    for d in project.of_kind("Dataset"):
        if "raster" not in d.spec:
            continue
        if "tables" in d.spec:
            problems.append(Problem(d.file, d.line(("spec", "raster")), "spec.raster: a Dataset loads tables or a raster, not both", d.label))
        path = d.spec["raster"].get("path")
        if path in paths:
            problems.append(Problem(d.file, d.line(("spec", "raster", "path")), f"spec.raster.path: {path} is also written by {paths[path].label}", d.label))
        paths.setdefault(path, d)
    return problems


def check_workspaces_and_styles(project: Project) -> list[Problem]:
    """One default workspace; a global style is in no workspace and isn't one of GeoServer's own."""
    problems = []
    defaults = [w for w in project.of_kind("Workspace") if w.spec.get("default")]
    for w in defaults[1:]:
        problems.append(Problem(w.file, w.line(("spec", "default")), f"spec.default: {defaults[0].label} is already the default", w.label))
    for s in project.of_kind("Style"):
        if not s.spec.get("global"):
            continue
        if "workspace" in s.spec:
            problems.append(Problem(s.file, s.line(("spec", "workspace")), "spec.workspace: a global style is in no workspace", s.label))
        if s.name in BUILT_IN_STYLES:
            problems.append(Problem(s.file, s.line(), f"{s.name} is one of GeoServer's own styles: use it without defining it", s.label))
    return problems


def check_blob_stores(project: Project) -> list[Problem]:
    """Each blob store has what its type needs, at most one is the default, and caches name ones that exist."""
    problems = []
    stores = {(b.spec.get("name") or b.name): b for b in project.of_kind("BlobStore")}
    for b in project.of_kind("BlobStore"):
        kind = b.spec.get("type")
        if kind == "file" and not b.spec.get("path"):
            problems.append(Problem(b.file, b.line(("spec",)), "spec.path: a file blob store needs a folder on the data volume, e.g. gwc", b.label))
        if kind == "s3" and not b.spec.get("bucket"):
            problems.append(Problem(b.file, b.line(("spec",)), "spec.bucket: an s3 blob store needs its bucket", b.label))
    defaults = [b for b in project.of_kind("BlobStore") if b.spec.get("default")]
    for b in defaults[1:]:
        problems.append(Problem(b.file, b.line(("spec", "default")), f"spec.default: {defaults[0].label} is already the default", b.label))
    for r in project.of_kind("Layer") + project.of_kind("LayerGroup"):
        cache = r.spec.get("cache")
        name = cache.get("blobStore") if isinstance(cache, dict) else None
        if name and name not in stores:
            problems.append(Problem(r.file, r.line(("spec", "cache", "blobStore")), f"spec.cache.blobStore: no BlobStore named {name!r}{known(stores)}", r.label))
    return problems
