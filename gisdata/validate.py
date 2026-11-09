"""Check a project's files against the spec, without touching the network.

Two passes: each resource against the JSON Schema (gisdata/schema/v1alpha1.json),
then the rules that span files: references between resources, style files,
names that would clash in GeoServer or PostGIS, and layer groups that contain
themselves.
"""

from __future__ import annotations

import difflib
import json
import re
from collections import defaultdict
from importlib import resources as package_files
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from .loader import Problem, Project, Resource

BUILT_IN_STYLES = {"generic", "line", "point", "polygon", "raster"}
STYLE_SUFFIXES = {".sld", ".mbstyle", ".css"}
SOURCES_WITH_PRODUCTS = {"os-downloads", "ckan", "ogc-api-features", "wfs"}
SOURCES_NEEDING_KEYS = {"os-downloads"}


def schema() -> dict:
    return json.loads(package_files.files("gisdata").joinpath("schema/v1alpha1.json").read_text())


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

    for kind in ("Project", "Source", "Dataset", "Layer", "LayerGroup", "Style"):
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
        if s.spec.get("type") in SOURCES_NEEDING_KEYS and "credentials" not in s.spec:
            problems.append(Problem(s.file, s.line(("spec",)), f"spec.credentials: a {s.spec['type']} source needs an API key secret", s.label))

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
            table = (options or {}).get("name") or re.sub(r"[^a-z0-9]+", "_", f"{d.name}_{key}".lower()).strip("_")
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
    published = {layer.spec.get("dataset") for layer in project.of_kind("Layer")}
    for d in project.of_kind("Dataset"):
        if d.name not in published:
            problems.append(Problem(d.file, d.line(), "loaded but no Layer publishes it", d.label, "warning"))
    return problems


def known(named: dict) -> str:
    return f" (known: {', '.join(sorted(named))})" if named else ""
