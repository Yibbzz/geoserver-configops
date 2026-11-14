"""Files GeoServer keeps beside its configuration, taken from the project folder as they are.

    styles/**/*.png (.svg .jpg .jpeg .gif)   images a style points to: uploaded beside the styles, at the same
                                             path under styles/, in every workspace the project has styles in
    styles/**/*.ttf (.otf)                   fonts: to GeoServer's own styles folder, where it looks for them
    templates/*.ftl                          FreeMarker templates (GetFeatureInfo and others) for the workspace
    templates/<layer>/*.ftl                  ... for one Layer

GeoServer's services keep images and fonts in memory: after an upload, a reset (POST /rest/reset) has them
all read the files again. Templates are read on every request.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from urllib.parse import quote, unquote

from .geoserver import q
from .validate import geoserver_name

GRAPHICS = {".png", ".svg", ".jpg", ".jpeg", ".gif"}
FONTS = {".ttf", ".otf"}
TEMPLATE = ".ftl"


def walk(folder: Path) -> list[Path]:
    """Every file under a folder (following links: Kubernetes mounts a ConfigMap's folders as links)."""
    return sorted(Path(d) / n for d, _, names in os.walk(folder, followlinks=True) for n in names)


def style_files(planner) -> dict[str, Path]:
    """GeoServer resource path -> file, for the images and fonts under styles/."""
    root = planner.root / "styles"
    folders = sorted({f"workspaces/{ws}/styles" if ws else "styles" for _, ws in planner.styles.values()})
    out = {}
    for f in walk(root):
        suffix = f.suffix.lower()
        if suffix in FONTS:
            out[f"styles/{f.name}"] = f
        elif suffix in GRAPHICS:
            for folder in folders or [f"workspaces/{planner.ws}/styles"]:
                out[f"{folder}/{f.relative_to(root).as_posix()}"] = f
    return out


def template_files(root: Path) -> list[tuple[str | None, Path]]:
    """(Layer name, or None for the workspace's; file) for the templates under templates/."""
    folder = root / "templates"
    out = []
    for f in walk(folder):
        parts = f.relative_to(folder).parts
        if f.suffix == TEMPLATE and len(parts) <= 2:
            out.append((parts[0] if len(parts) == 2 else None, f))
    return out


def resource_of(gs, ws: str, name: str) -> str | None:
    """A live layer's feature type or coverage, as a REST path."""
    href = (((gs.layer(ws, name) or {}).get("layer") or {}).get("resource") or {}).get("href")
    return unquote(href.split("/rest/", 1)[1]).removesuffix(".json") if href else None


def same_text(a: str, b: str) -> bool:
    return a.replace("\r\n", "\n").strip() == b.replace("\r\n", "\n").strip()


def plan(planner, managed: list[str]) -> None:
    gs, add = planner.gs, planner.add
    kept: set[str] = set()  # resource paths of the files the project has, as GeoServer stores them

    # images and fonts
    waiting: list[str] = []

    def upload(path: str, body: bytes) -> None:
        gs.request("PUT", f"resource/{path}", body, content_type=mimetypes.guess_type(path)[0] or "application/octet-stream")
        waiting.remove(path)
        if not waiting:  # the last one: every service drops the images and fonts it holds
            gs.request("POST", "reset")

    for path, f in style_files(planner).items():
        kept.add(path)
        body = f.read_bytes()
        live = gs.read(f"resource/{path}")
        if live == body:
            continue
        waiting.append(path)
        add("+" if live is None else "~", "file", path, note=str(f.relative_to(planner.root)),
            run=lambda path=path, body=body: upload(path, body))

    # templates
    for layer_name, f in template_files(planner.root):
        text = f.read_text(encoding="utf-8")
        if layer_name is None:
            ws, base = planner.ws, f"workspaces/{q(planner.ws)}"
            kept.add(f"workspaces/{ws}/{f.name}")
        else:
            layer = planner.layers.get(layer_name)
            if layer is None or layer_name in planner.unpublished:
                continue  # validate reports a folder that names no Layer; an unpublished layer has its own line
            ws, name = planner.workspace_of(layer), geoserver_name(layer)
            base = resource_of(gs, ws, name)
            if base:  # GeoServer keeps it at workspaces/<ws>/<store>/<resource>/<file>
                parts = base.split("/")
                kept.add("/".join([*parts[:2], parts[3], parts[5], f.name]))
        target = f"{ws}:{geoserver_name(planner.layers[layer_name])} {f.name}" if layer_name else f"{ws} {f.name}"

        def put(base=base, ws=ws, layer_name=layer_name, name=f.name, text=text):
            base = base or resource_of(gs, ws, geoserver_name(planner.layers[layer_name]))  # a layer made by this apply
            gs.request("PUT", f"{base}/templates/{name}", text.encode(), content_type="text/plain")

        live = gs.read(f"{base}/templates/{f.name}") if base else None
        if live is None:
            add("+", "template", target, note=str(f.relative_to(planner.root)), run=put)
        elif not same_text(live.decode("utf-8", "replace"), text):
            add("~", "template", target, note=f"{f.relative_to(planner.root)} differs from GeoServer's copy", run=put)

    # what the project's workspaces hold that the files don't
    for path in [p for ws in managed for p in gs.files(f"workspaces/{ws}")]:
        suffix = Path(path).suffix.lower()
        in_styles = path.split("/")[2] == "styles"
        if path in kept or not (suffix == TEMPLATE and not in_styles or suffix in GRAPHICS and in_styles):
            continue
        kind = "template" if suffix == TEMPLATE else "file"
        if planner.prune:
            add("-", kind, path, note="not in the files",
                run=lambda path=path: gs.request("DELETE", f"resource/{quote(path, safe='/')}"))  # a live name: anything goes
        else:
            add("?", kind, path, note="unmanaged; removed with --prune")


def download(gs, ws: str, layers: dict[str, str]) -> tuple[dict[str, bytes], list[str]]:
    """What import writes for a workspace: its images as styles/..., its templates as templates/...
    (path in the project -> content), and notes on what it left. `layers`: GeoServer name -> Layer name."""
    found, notes = {}, []
    for path in gs.files(f"workspaces/{ws}"):
        parts, suffix = path.split("/")[2:], Path(path).suffix.lower()
        if parts[0] == "styles":
            if suffix not in GRAPHICS:
                continue
            where = "/".join(parts)
        elif suffix != TEMPLATE:
            continue
        elif len(parts) == 1:
            where = f"templates/{parts[0]}"
        elif len(parts) == 3 and parts[1] in layers:
            where = f"templates/{layers[parts[1]]}/{parts[2]}"
        else:
            notes.append(f"template {path} isn't the workspace's or a layer's: not imported")
            continue
        body = gs.read(f"resource/{path}")
        if body is not None:
            found[where] = body
    return found, notes


def write(target: Path, found: dict[str, bytes]) -> list[str]:
    """Write what `download` found where it differs; the paths written."""
    written = []
    inside = target.resolve()
    for where, body in found.items():
        path = target / where
        if inside not in path.resolve().parents:  # a name from GeoServer that climbs out of the project (../..)
            raise ValueError(f"GeoServer names a file {where!r}, which is outside {target}: not written")
        if not path.exists() or path.read_bytes() != body:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            written.append(where)
    return written
