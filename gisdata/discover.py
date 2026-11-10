"""`gisdata discover`: what a source offers, and the layers inside one product."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import remote, secrets
from .loader import load
from .sources import Download, NotFound, Product, SourceError, source_types

# Key in a source's credentials secret that holds the API key
API_KEY = "apiKey"


class DiscoverError(Exception):
    """Ends the command with a message; `code` is the exit code."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


@dataclass
class ResolvedSource:
    label: str  # how to name it in messages: the Source's name, or the type
    type: str
    client: object
    command: str  # how to run discover on it again, for hints


def resolve(name: str, root: Path, url: str | None = None) -> ResolvedSource:
    """
    A Source named `name` in the project at `root`, or else a source type
    used directly. `url` is the source's address (a CKAN portal); it
    overrides the Source's own.
    """
    types = source_types()
    source = load(root).named("Source").get(name) if root.is_dir() else None
    if source is not None:
        stype = source.spec.get("type")
        if stype not in types:
            raise DiscoverError(f"Source {name} has type '{stype}', which can't be discovered (yet). Types that can: {', '.join(types)}")
        api_key = None
        secret = (source.spec.get("credentials") or {}).get("secret")
        if secret:
            api_key = secrets.lookup(secret, API_KEY, root)
            if api_key is None:
                print(
                    f"gisdata: note: secret {secret} (key {API_KEY}) isn't set "
                    f"(${secrets.env_name(secret, API_KEY)} or secrets.yaml); trying without a key",
                    file=sys.stderr,
                )
        command = f"gisdata discover {name}" + (f" --url {url}" if url else "")
        return ResolvedSource(name, stype, client(types[stype], api_key, url or source.spec.get("url")), command)
    if name in types:
        command = f"gisdata discover {name}" + (f" --url {url}" if url else "")
        return ResolvedSource(name, name, client(types[name], None, url), command)
    raise DiscoverError(
        f"no Source named '{name}' in {root}, and '{name}' isn't a source type. "
        f"Name a Source from the project, or a type: {', '.join(types)}",
        code=2,
    )


def client(source_type: type, api_key: str | None, url: str | None):
    try:
        return source_type(api_key=api_key, url=url)
    except SourceError as e:
        raise DiscoverError(str(e), code=2) from None


# --- layer cache: a product version never changes, so its layers don't either


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "gisdata" / "layers"


def cache_path(source_type: str, product: Product) -> Path | None:
    d = product.chosen
    if not product.version or d is None:
        return None
    key = json.dumps([source_type, product.id, product.version, d.id, d.url if d.id else "", d.format, d.subformat, d.area, d.file_name, d.md5])
    return cache_dir() / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"


def cached_layers(path: Path | None) -> list[remote.Layer] | None:
    if path is None:
        return None
    try:
        return [remote.Layer.from_dict(d) for d in json.loads(path.read_text())]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_layers(path: Path | None, layers: list[remote.Layer]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([layer.to_dict() for layer in layers]))
    except OSError:
        pass  # a cache that can't be written only costs time


def product_layers(src: ResolvedSource, product: Product, use_cache: bool = True) -> list[remote.Layer]:
    path = cache_path(src.type, product)
    if use_cache:
        hit = cached_layers(path)
        if hit is not None:
            return hit
    d = product.chosen
    print(
        f"Reading layers from {d.file_name or d.url} ({size(d.size)}) without downloading it; "
        "a large file can take a minute or two...",
        file=sys.stderr,
    )
    layers = remote.url_layers(src.client.download_url(d), file_name=d.file_name)
    save_layers(path, layers)
    return layers


# --- output


def size(n: int | None) -> str:
    if n is None:
        return "size unknown"
    if n < 1000:
        return f"{n} bytes"
    for unit in ("KB", "MB", "GB"):
        n /= 1000
        if n < 1000 or unit == "GB":
            return f"{n:.1f} {unit}"


def table(rows: list[list[str]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


def short(text: str, width: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def version(v: str | None) -> str:
    """A date-time version (CKAN's last change) shown as its date."""
    if not v:
        return "-"
    return v[:10] if len(v) > 10 and v[4:5] == "-" and v[10:11] == "T" else v


def print_products(src: ResolvedSource, products: list[Product], total: int | None, search: str | None) -> None:
    if not products:
        print(f"No products from {src.label}" + (f" match '{search}'" if search else ""))
        return
    rows = [["PRODUCT", "VERSION", "LOADS AS", "NAME"]]
    for p in products:
        rows.append([p.id, version(p.version), p.loads_as or "-", short(p.name)])
    print(table(rows))
    skipped = sum(1 for p in products if not p.loads_as)
    shown = f"{len(products)} of {total:,}" if total and total > len(products) else f"{len(products)}"
    print(f"\n{shown} products from {src.label}" + (f" matching '{search}'" if search else ""), end="")
    print(f"; {skipped} have no file gisdata can load" if skipped else "")
    if total and total > len(products):
        print("More with --limit, or narrow with --search")
    print(f"Layers inside one: {src.command} <product>")


def download_line(d: Download, used: bool) -> str:
    label = f"{d.format} {d.subformat}".strip()
    file_name = d.file_name if "." in d.file_name and d.file_name != d.name else ""  # not ".../download"
    what = ", ".join(x for x in (d.name, file_name) if x) or d.area or d.url
    line = f"  {'*' if used else ' '} {label}: {short(what, 60)}"
    if d.loadable:
        line += f", {size(d.size)}"
    else:
        line += " (not a file gisdata loads)"
    return line + (f"  [{d.id}]" if d.id else "")


def print_product(src: ResolvedSource, p: Product, layers: list[remote.Layer] | None) -> None:
    if src.type == "url":
        print(f"{p.name}\n  {p.id}")
    else:
        print(f"{p.name} ({p.id}), version {version(p.version) if p.version else 'unknown'}")
    groups: dict[str, list[Download]] = {}
    for d in p.downloads:
        groups.setdefault(f"{d.format} {d.subformat}".strip(), []).append(d)
    for label, ds in groups.items():
        used = p.chosen in ds
        if len(ds) > 1 and all(d.area and not d.id for d in ds):
            # one format cut into many areas (OS grid squares): one line
            line = f"  {'*' if used else ' '} {label}: {len(ds)} files by area"
            if used:
                line += f"; using {p.chosen.file_name or p.chosen.area}, {size(p.chosen.size)}"
            print(line)
        else:
            for d in ds:
                print(download_line(d, d is p.chosen))
    if not p.downloads:
        print("\nNo downloads listed.")
        return
    if p.chosen is None:
        print(f"\nNo download gisdata can load (it reads {', '.join(src.client.formats)} as one file).")
        return
    print("  (* the download sync uses)")
    if sum(1 for d in p.downloads if d.loadable) > 1 and p.chosen.id:
        print(f"Another one: --resource <id or name>; in a Dataset, `resource: {p.chosen.id}`")
    if layers is None:
        return
    print()
    rows = [["LAYER", "GEOMETRY", "CRS", "FEATURES"]]
    for layer in layers:
        rows.append([
            layer.name,
            layer.geometry or "none",
            layer.crs or "-",
            f"{layer.features:,}" if layer.features is not None else "-",
        ])
    print(table(rows))
    files = sorted({layer.file for layer in layers if layer.file})
    if len(files) > 1:
        print(f"\nfrom {len(files)} files: {', '.join(files)}")
    print("\nA Dataset loads every layer unless `tables` names some; each becomes table <dataset>_<layer>.")


def run(
    source: str, product_id: str | None, root: Path, fmt: str = "text", layers: bool = True, use_cache: bool = True,
    search: str | None = None, limit: int | None = None, resource: str | None = None, url: str | None = None,
) -> int:
    src = resolve(source, root, url)
    try:
        if product_id is None:
            if resource:
                raise DiscoverError("--resource needs a product", code=2)
            listing = src.client.products(search, limit)
            if fmt == "json":
                print(json.dumps({"total": listing.total, "products": [p.to_dict() for p in listing.products]}, indent=2, ensure_ascii=False))
            else:
                print_products(src, listing.products, listing.total, search)
            return 0

        if search:
            raise DiscoverError("--search lists products; leave out the product to search", code=2)
        try:
            product = src.client.product(product_id, resource)
        except NotFound:
            raise DiscoverError(f"{src.label} has no product '{product_id}'; find one with: {src.command} --search <words>") from None
    except SourceError as e:
        raise DiscoverError(str(e)) from None

    # A file that can't be read still leaves the downloads worth showing
    found, layer_error = None, None
    if layers and product.chosen:
        try:
            found = product_layers(src, product, use_cache)
        except (SourceError, remote.RemoteError) as e:
            layer_error = f"couldn't read layers: {e}"

    if fmt == "json":
        out = product.to_dict()
        out["downloads"] = [d.to_dict() for d in product.downloads]
        out["download"] = product.chosen.to_dict() if product.chosen else None
        out["layers"] = [layer.to_dict() for layer in found] if found is not None else None
        out["layersError"] = layer_error
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print_product(src, product, found)
    if layer_error:
        print(f"gisdata discover: {layer_error}", file=sys.stderr)
        return 1
    return 0
