"""`configops import` into an existing project: bring changes made in GeoServer back into the files.

What import reads from GeoServer is matched with the files' resources by what
they are in GeoServer (a layer's workspace and name, a role's name, a rule's
key), not by resource name, so the names you chose, and the references to
them, stay. Then:

- a resource whose meaning changed is rewritten in place, in its own file;
  the comments around it are kept (those inside it aren't);
- a resource that's the same, however it's written, is left alone;
- a resource only in GeoServer is added to the import's file for its kind;
- a resource only in the files is reported, and removed with --prune.

Some things can't be read back from GeoServer: passwords and keys (the files
keep the Secrets they name), a layer's attributes, the master password. The
files' own values for those are kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .importer import DEFAULTS_KEY
from .loader import Project, Resource
from .security import account_name, role_name, rule_key
from .validate import geoserver_name

KEPT_FROM_FILES = {  # fields import can't read back, kept as the files have them
    "Layer": ("attributes", "dataset", "table"),
    "User": ("password",),
    "Store": ("credentials", "dataset"),
    "BlobStore": ("credentials",),
}
DEFAULTS = {  # what import leaves out; a file that spells them out means the same
    "Layer": {"enabled": True, "advertised": True, "queryable": True, "opaque": False, "cache": False},
    "LayerGroup": {"mode": "single", "enabled": True, "advertised": True, "cache": False},
    "Store": {"enabled": True},
    "User": {"enabled": True, "groups": [], "roles": []},
    "Group": {"roles": []},
    "BlobStore": {"enabled": True, "default": False},
    "Workspace": {"isolated": False, "default": False},
}
WORKSPACE_KINDS = ("Workspace", "Store", "Layer", "LayerGroup")
SECURITY_KINDS = ("Role", "Group", "User", "AccessRule")
SETTINGS_KINDS = ("Settings", "Service")


@dataclass
class Result:
    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)


def as_resource(data: dict) -> Resource:
    return Resource(data=data, file=Path(), node=None)


def identity(r: Resource, default_ws: str | None) -> tuple:
    """What a resource is in GeoServer, whatever the files call it."""
    kind, spec = r.kind, r.spec
    if kind in ("Layer", "LayerGroup", "Store"):
        return kind, spec.get("workspace") or default_ws, geoserver_name(r)
    if kind == "Workspace":
        return kind, geoserver_name(r)
    if kind == "Style":
        return kind, None if spec.get("global") else (spec.get("workspace") or default_ws), r.name
    if kind == "Settings":
        return kind, spec.get("workspace")
    if kind == "Role":
        return kind, role_name(r)
    if kind in ("Group", "User"):
        return kind, account_name(r)
    if kind == "AccessRule":
        return (kind,) + rule_key(r)
    return kind, spec.get("name") or r.name  # Service, Gridset, BlobStore


def normal(kind: str, key: str, value, name: str, default_ws: str | None = None, own: dict | None = None):
    """A field's value as it means, with leaving it out and its default the same."""
    if value is None:
        if own and key in own:  # a default that depends on the resource, e.g. a layer's style from its geometry
            return own[key]
        if key in ("title", "name") and kind in ("Layer", "LayerGroup", "Store", "Workspace"):
            return name
        if key == "workspace" and kind in WORKSPACE_KINDS:
            return default_ws
        return DEFAULTS.get(kind, {}).get(key)
    if key == "cache" and value is not False:
        setting = {} if value is True else dict(value)
        setting["gridsets"] = sorted(setting.get("gridsets", ["EPSG:4326", "EPSG:900913"]))
        setting["formats"] = sorted(setting.get("formats", ["image/png"]))
        return setting
    if value in ([], {}):
        return DEFAULTS.get(kind, {}).get(key, value)
    return value


def differences(kind: str, old: dict, new: dict, name: str, default_ws: str | None = None,
                own: dict | None = None) -> list[str]:
    """The spec keys whose meaning differs between the files and GeoServer."""
    return [k for k in dict.fromkeys([*old, *new])
            if normal(kind, k, old.get(k), name, default_ws, own) != normal(kind, k, new.get(k), name, default_ws, own)]


def in_scope(r: Resource, ws: str | None, settings: bool, security: bool, default_ws: str | None) -> bool:
    """Whether this import reads the resource, so that its absence from GeoServer means something."""
    if r.kind == "Workspace":
        return geoserver_name(r) == ws
    if r.kind in WORKSPACE_KINDS:
        return ws is not None and identity(r, default_ws)[1] == ws
    if r.kind in SETTINGS_KINDS:
        return settings
    return security and r.kind in SECURITY_KINDS


def merge(target: Path, existing: Project, imported: dict[Path, list[dict]], ws: str | None,
          settings: bool, security: bool, prune: bool) -> Result:
    """Bring the imported resources into the project's files; returns what changed."""
    project = existing.of_kind("Project")[0]
    default_ws = project.spec.get("workspace")
    result = Result()
    known = {identity(r, default_ws): r for r in existing.resources if r.kind}
    edits: dict[Path, list[tuple[int, int, str | None]]] = {}  # file -> (first line, end line, new text or None)
    appends: dict[Path, list[dict]] = {}
    renames: dict[tuple[str, str], str] = {}  # (kind, imported name) -> the files' name

    flat = [(path, doc) for path, docs in imported.items() for doc in docs]
    for _, doc in flat:
        match = known.get(identity(as_resource(doc), default_ws))
        if match is not None:
            renames[(doc["kind"], doc["metadata"]["name"])] = match.name
    seen = set()
    for path, doc in flat:
        own = doc.get(DEFAULTS_KEY) or {}
        doc = rename_references({k: v for k, v in doc.items() if k != DEFAULTS_KEY}, renames)
        key = identity(as_resource(doc), default_ws)
        seen.add(key)
        match = known.get(key)
        label = f"{doc['kind']} {doc['metadata']['name']}"
        if match is None:
            appends.setdefault(path, []).append(doc)
            result.added.append(label)
            continue
        spec = dict(doc.get("spec") or {})
        for kept in KEPT_FROM_FILES.get(doc["kind"], ()):
            if kept in match.spec:
                spec[kept] = match.spec[kept]
        changed = differences(doc["kind"], match.spec, spec, key[-1], default_ws, own)
        if not changed:
            continue
        edits.setdefault(match.file, []).extend(key_edits(match, spec, changed, own))
        result.changed.append(f"{match.kind} {match.name} ({', '.join(changed)})")

    for key, r in known.items():
        if key in seen or not in_scope(r, ws, settings, security, default_ws):
            continue
        if prune:
            edits.setdefault(r.file, []).append((*span(r), None))
            result.removed.append(f"{r.kind} {r.name}")
        else:
            result.missing.append(f"{r.kind} {r.name}")

    for path, changes in edits.items():
        write_edits(path, changes)
    for path, docs in appends.items():
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        added = "\n---\n".join(dump_one(d).rstrip("\n") for d in docs) + "\n"
        path.write_text(text + ("---\n" if text.strip() else "") + added, encoding="utf-8")
    return result


def rename_references(doc: dict, renames: dict[tuple[str, str], str]) -> dict:
    """The imported resource, naming other resources as the files do."""
    doc = {**doc, "metadata": dict(doc["metadata"]), "spec": dict(doc.get("spec") or {})}
    spec = doc["spec"]
    if doc["kind"] == "LayerGroup":
        members = []
        for entry in spec.get("layers", []):
            if isinstance(entry, dict):
                name = entry["layer"]
                entry = {**entry, "layer": renames.get(("Layer", name)) or renames.get(("LayerGroup", name)) or name}
            else:
                entry = renames.get(("Layer", entry)) or renames.get(("LayerGroup", entry)) or entry
            members.append(entry)
        spec["layers"] = members
    if doc["kind"] == "Layer" and spec.get("store"):
        spec["store"] = renames.get(("Store", spec["store"]), spec["store"])
    return doc


def span(r: Resource) -> tuple[int, int]:
    """The 0-based first line and end line (exclusive) of a resource's document in its file."""
    start, end = r.node.start_mark, r.node.end_mark
    return start.line, end.line + (1 if end.column else 0)


def key_edits(r: Resource, spec: dict, changed: list[str], own: dict | None = None) -> list[tuple[int, int, str | None]]:
    """Line edits that change only these spec keys, leaving the rest of the resource (and its comments) as written."""
    values = {}  # the changed keys' new values; None takes the key out
    for key in changed:
        value = spec.get(key)
        if value is None and own and key in own:  # GeoServer has the default, and the files name another value
            value = own[key]
        values[key] = value
    spec_node = next((v for k, v in r.node.value if k.value == "spec"), None)
    if spec_node is None or spec_node.flow_style or not spec_node.value:  # spec: { ... } on one line: write the whole spec
        whole = {k: v for k, v in r.spec.items() if k not in values}
        whole.update({k: v for k, v in values.items() if v is not None})
        return [(*span(r), dump_one({**r.data, "spec": whole}))]
    pairs = spec_node.value
    indent = pairs[0][0].start_mark.column
    spans = {}
    for i, (k, v) in enumerate(pairs):
        end = pairs[i + 1][0].start_mark.line if i + 1 < len(pairs) else v.end_mark.line + (1 if v.end_mark.column else 0)
        spans[k.value] = (k.start_mark.line, end)
    out = []
    added = []
    for key, value in values.items():
        text = indented({key: value}, indent) if value is not None else None
        if key in spans:
            out.append((*spans[key], text))
        elif text:
            added.append(text)
    if added:
        last = max(end for _, end in spans.values())
        out.append((last, last, "".join(added)))
    return out


def indented(data: dict, indent: int) -> str:
    return "".join(" " * indent + line + "\n" for line in dump_one(data).rstrip("\n").split("\n"))


def write_edits(path: Path, changes: list[tuple[int, int, str | None]]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for first, end, text in sorted(changes, reverse=True):  # from the bottom, so earlier spans stay put
        if text is None:  # removed: take the --- before it too, or the one after if it's first
            before = first - 1
            while before >= 0 and lines[before].strip().startswith("#"):
                before -= 1
            if before >= 0 and lines[before].strip() == "---":
                first = before
            elif end < len(lines) and lines[end].strip() == "---":
                end += 1
            lines[first:end] = []
        elif first == end and text:  # an insertion
            lines[first:first] = [text]
        else:
            lines[first:end] = [text if text.endswith("\n") else text + "\n"] if text else []
    path.write_text("".join(lines), encoding="utf-8")


def dump_one(doc: dict) -> str:
    from .importer import Dumper

    return yaml.dump(doc, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=120)
