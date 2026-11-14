"""Check a project's files against the spec, without touching the network.

Two passes: each resource against the JSON Schema (configops/schema/v1alpha1.json),
then the rules that span files: references between resources, style files,
names that would clash in GeoServer or PostGIS, and layer groups that contain
themselves.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from importlib import resources as package_files
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from .loader import Problem, Project, Resource

BUILT_IN_STYLES = {"generic", "line", "point", "polygon", "raster"}
STYLE_SUFFIXES = {".sld", ".mbstyle", ".css"}


def schema() -> dict:
    return json.loads(package_files.files("configops").joinpath("schema/v1alpha1.json").read_text())


def geoserver_name(r: Resource) -> str:
    return r.spec.get("name") or (r.name or "").replace("-", "_")


def validate(project: Project) -> list[Problem]:
    problems = list(project.problems)
    problems += check_schema(project)
    problems += check_project(project)
    problems += check_styles(project)
    problems += check_templates(project)
    problems += check_groups(project)
    problems += check_names(project)
    problems += check_settings(project)
    problems += check_security(project)
    problems += check_stores(project)
    problems += check_database(project)
    problems += check_workspaces_and_styles(project)
    problems += check_blob_stores(project)
    problems += check_auth(project)
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

    for kind in ("Project", "Layer", "LayerGroup", "Style", "Settings", "Service", "Role", "Group", "User", "AccessRule", "Store", "Workspace", "Gridset", "BlobStore",
                 "PasswordPolicy", "AuthFilter", "AuthProvider", "UserGroupService", "RoleService", "URLCheck",
                 "MasterPasswordProvider"):
        first: dict[str, Resource] = {}
        for r in project.of_kind(kind):
            if r.name in first and kind != "Project":
                other = first[r.name]
                problems.append(Problem(r.file, r.line(("metadata", "name")),
                                        f"{kind} name already used in {other.file.name}:{other.line()}", r.label))
            first.setdefault(r.name, r)
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
    checked = {f for files in by_stem.values() for f in files}
    for s in project.of_kind("Style"):
        f = s.spec.get("file")
        if isinstance(f, str) and not (project.root / f).is_file():
            problems.append(Problem(s.file, s.line(("spec", "file")), f"spec.file: {f} not found under {project.root}", s.label))
        elif isinstance(f, str):
            checked.add(project.root / f)
    for f in sorted(checked):
        found = style_problem(f)
        if found:
            problems.append(Problem(f, found[0], found[1]))
    return set(by_stem) | set(project.named("Style")) | BUILT_IN_STYLES, problems


def style_problem(f: Path) -> tuple[int, str] | None:
    """(line, what's wrong) with a style file GeoServer couldn't read: apply sends it as it is, as UTF-8, and
    GeoServer keeps a broken style without a word until a map asks for it."""
    try:
        data = f.read_bytes()
        data.decode("utf-8")
    except UnicodeDecodeError:
        return 0, "not UTF-8 text: configops sends a style as UTF-8"
    except OSError as e:
        return 0, f"can't be read: {e.strerror or e}"
    if f.suffix == ".sld":
        declared = re.match(rb'\s*<\?xml[^>]*encoding=["\']([^"\']+)["\']', data)
        other = declared and declared.group(1).decode("ascii", "replace").lower().replace("_", "-") not in ("utf-8", "utf8")
        if other and not data.isascii():  # plain ASCII reads the same either way
            return 1, f"says its encoding is {declared.group(1).decode('ascii', 'replace')}, but it is sent as UTF-8: save it as UTF-8 and say so"
        try:
            ET.fromstring(data)
        except ET.ParseError as e:
            return e.position[0], f"not well-formed XML: {e}"
    elif f.suffix == ".mbstyle":
        try:
            json.loads(data)
        except ValueError as e:
            return getattr(e, "lineno", 0), f"not valid JSON: {e}"
    return None


def check_templates(project: Project) -> list[Problem]:
    """templates/x.ftl is the workspace's, templates/<layer>/x.ftl a Layer's: anything else would be left out."""
    problems = []
    folder = project.root / "templates"
    layers = project.named("Layer")
    for d, _, names in os.walk(folder, followlinks=True):
        for f in sorted(Path(d) / n for n in names):
            parts = f.relative_to(folder).parts
            if f.suffix != ".ftl":
                problems.append(Problem(f, 0, "templates/ holds FreeMarker templates (.ftl) only"))
            elif len(parts) > 2:
                problems.append(Problem(f, 0, "a template goes in templates/ (the workspace's) or templates/<layer>/ (one Layer's)"))
            elif len(parts) == 2 and parts[0] not in layers:
                problems.append(Problem(f, 0, f"templates/{parts[0]}/: no Layer named {parts[0]!r}"))
    return problems


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
        for s, style in enumerate(group.spec.get("styles") or []):
            for i, entry in enumerate(style.get("layers") or [] if isinstance(style, dict) else []):
                member = entry if isinstance(entry, str) else (entry or {}).get("layer")
                if member and member not in layers and member not in groups:
                    problems.append(Problem(group.file, group.line(("spec", "styles", s, "layers", i)),
                                            f"spec.styles[{s}].layers[{i}]: no Layer or LayerGroup named {member!r}", group.label))
        root = group.spec.get("rootLayer")
        if (group.spec.get("mode") == "eo") != bool(root):
            problems.append(Problem(group.file, group.line(("spec",)), "spec.rootLayer: an eo group needs one, and only an eo group has one",
                                    group.label))
        elif root and root not in layers:
            problems.append(Problem(group.file, group.line(("spec", "rootLayer")), f"spec.rootLayer: no Layer named {root!r}", group.label))

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

    # A layer on a table is published from a store configops makes for the table's schema, named after it:
    # a Store with that name in the same workspace would be the same store in GeoServer, each apply undoing the other
    database = projects[0].spec.get("database") if projects else None
    default_schema = (database.get("schema") if isinstance(database, dict) else None) or "public"
    own = {(s.spec.get("workspace") or default_ws, geoserver_name(s)): s for s in project.of_kind("Store")}
    reported = set()
    for layer in project.of_kind("Layer"):
        table, sql = layer.spec.get("table"), layer.spec.get("sql")
        if "store" in layer.spec or not (table or sql):
            continue
        schema = sql.get("schema") if isinstance(sql, dict) else table.get("schema") if isinstance(table, dict) else default_schema
        key = (layer.spec.get("workspace") or default_ws, schema)
        if key in own and key not in reported:
            reported.add(key)
            s = own[key]
            problems.append(Problem(s.file, s.line(("metadata", "name")),
                                    f"GeoServer store {key[0]}:{key[1]} is also the store configops makes for the layers on tables in "
                                    f"schema {key[1]} ({layer.label}): give the Store another name", s.label))
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
STORES_WITH_PATHS = {"geopackage", "shapefile", "shapefiles", "flatgeobuf", "geotiff", "worldimage", "arcgrid", "imagemosaic",
                     "imagepyramid", "geoparquet", "property", "csv", "appschema"}


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
        if kind in STORES_WITH_PATHS and not s.spec.get("path"):
            problems.append(Problem(s.file, s.line(("spec",)), f"spec.path: a {kind} store needs its file's path on the data volume, e.g. rasters/surrey.tif", s.label))
        if kind in STORES_WITH_PATHS and s.spec.get("url"):
            problems.append(Problem(s.file, s.line(("spec", "url")), f"spec.url: a {kind} store reads a file on the data volume: use spec.path", s.label))
        if kind == "pmtiles" and bool(s.spec.get("path")) == bool(s.spec.get("url")):
            problems.append(Problem(s.file, s.line(("spec",)), "a pmtiles store needs spec.path (on the data volume) or spec.url, not both", s.label))
        if kind in ("postgis", "sqlserver", "oracle"):
            missing = [k for k in ("host", "database") if k not in (s.spec.get("connection") or {})]
            if missing:
                problems.append(Problem(s.file, s.line(("spec",)), f"spec.connection: a {kind} store needs {', '.join(missing)}", s.label))
            if s.spec.get("url"):
                problems.append(Problem(s.file, s.line(("spec", "url")), f"spec.url: a {kind} store connects with spec.connection", s.label))
    for layer in project.of_kind("Layer"):
        name = layer.spec.get("store")
        if name is None:
            if "resource" in layer.spec:
                problems.append(Problem(layer.file, layer.line(("spec", "resource")), "spec.resource: only for a layer on a store", layer.label))
            continue
        if name not in stores:
            problems.append(Problem(layer.file, layer.line(("spec", "store")), f"spec.store: no Store named {name!r}{known(stores)}", layer.label))
        elif stores[name].spec.get("type") == "appschema" and "resource" not in layer.spec:
            problems.append(Problem(layer.file, layer.line(("spec",)), "spec.resource: a layer on an appschema store names its feature type, "
                                    "e.g. Site (GeoServer can't list them)", layer.label))
        if "table" in layer.spec:
            problems.append(Problem(layer.file, layer.line(("spec", "table")), "spec.table: a layer on a store doesn't take a table", layer.label))
    for layer in project.of_kind("Layer"):
        if "sql" in layer.spec:
            for key in ("table", "store"):
                if key in layer.spec:
                    problems.append(Problem(layer.file, layer.line(("spec", key)), f"spec.{key}: a SQL view doesn't take a {key}", layer.label))
    return problems


def check_database(project: Project) -> list[Problem]:
    """Layers on tables and SQL views need the Project's database: GeoServer reaches it through its JNDI datasource."""
    projects = project.of_kind("Project")
    if not projects or projects[0].spec.get("database"):
        return []
    return [Problem(layer.file, layer.line(("spec", "table" if "table" in layer.spec else "sql")),
                    "a layer on a table needs the Project's database (spec.database: jndi and connection)", layer.label)
            for layer in project.of_kind("Layer") if "table" in layer.spec or "sql" in layer.spec]


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
        if kind in ("s3", "gcs") and not b.spec.get("bucket"):
            problems.append(Problem(b.file, b.line(("spec",)), f"spec.bucket: an {kind} blob store needs its bucket", b.label))
        if kind == "azure" and not b.spec.get("container"):
            problems.append(Problem(b.file, b.line(("spec",)), "spec.container: an azure blob store needs its container", b.label))
        if kind == "azure" and not b.spec.get("credentials"):
            problems.append(Problem(b.file, b.line(("spec",)), "spec.credentials: an azure blob store needs a Secret with accountName and accountKey", b.label))
    defaults = [b for b in project.of_kind("BlobStore") if b.spec.get("default")]
    for b in defaults[1:]:
        problems.append(Problem(b.file, b.line(("spec", "default")), f"spec.default: {defaults[0].label} is already the default", b.label))
    for r in project.of_kind("Layer") + project.of_kind("LayerGroup"):
        cache = r.spec.get("cache")
        name = cache.get("blobStore") if isinstance(cache, dict) else None
        if name and name not in stores:
            problems.append(Problem(r.file, r.line(("spec", "cache", "blobStore")), f"spec.cache.blobStore: no BlobStore named {name!r}{known(stores)}", r.label))
    return problems


def check_auth(project: Project) -> list[Problem]:
    """Security objects say what they are; filter chains keep one for the REST API and name filters that exist."""
    from .auth import BUILT_IN_FILTERS, KINDS

    problems = []
    for r in [r for kind in KINDS if kind != "PasswordPolicy" for r in project.of_kind(kind)]:
        if bool(r.spec.get("class")) != bool(r.spec.get("className")):
            problems.append(Problem(r.file, r.line(("spec",)), "spec.class and spec.className: give both (a new one needs them), "
                                    "or neither (changing one GeoServer has)", r.label))
    filters = BUILT_IN_FILTERS | {r.spec.get("name") or r.name for r in project.of_kind("AuthFilter")}
    for r in project.of_kind("Settings"):
        chains = ((r.spec.get("global") or {}).get("security") or {}).get("filterChains")
        if not chains:
            continue
        if not any(p.startswith("/rest") for c in chains for p in c["paths"]):
            problems.append(Problem(r.file, r.line(("spec", "global", "security", "filterChains")),
                                    "spec.global.security.filterChains: no chain for /rest/**: configops would lock itself out", r.label))
        if not any(p == "/**" for c in chains for p in c["paths"]):  # the list is the whole list: GeoServer's other chains go
            problems.append(Problem(r.file, r.line(("spec", "global", "security", "filterChains")),
                                    "spec.global.security.filterChains: no chain for /**: the list replaces every chain GeoServer has, "
                                    "and requests no chain matches go through no authentication filters", r.label, "warning"))
        for i, c in enumerate(chains):
            for f in c.get("filters", []):
                if f not in filters:
                    problems.append(Problem(r.file, r.line(("spec", "global", "security", "filterChains", i)),
                                            f"filter {f!r} isn't GeoServer's own or an AuthFilter in the files: it must exist in GeoServer",
                                            r.label, "warning"))
    return problems
