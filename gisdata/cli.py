"""The gisdata command."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .discover import DiscoverError
from .discover import run as discover
from .plan import PlanError
from .plan import run as plan
from .sync import SyncError
from .sync import run as sync
from .loader import load
from .validate import validate

LATER = {
    "status": "show datasets and layers as they are live",
    "import": "write files that describe a live GeoServer workspace",
}


def shown(path: Path) -> str:
    """A path relative to the current directory when that's shorter."""
    try:
        rel = os.path.relpath(path)
    except ValueError:
        return str(path)
    return rel if len(rel) < len(str(path)) else str(path)


def cmd_validate(args) -> int:
    root = Path(args.dir)
    if not root.is_dir():
        print(f"gisdata: {root} is not a directory", file=sys.stderr)
        return 2
    project = load(root)
    problems = validate(project)
    errors = [p for p in problems if p.severity == "error"]

    if args.format == "json":
        print(json.dumps([
            {"file": shown(p.file), "line": p.line, "severity": p.severity, "resource": p.resource, "message": p.message}
            for p in problems
        ], indent=2))
    else:
        for p in problems:
            where = f"{shown(p.file)}:{p.line}" if p.line else shown(p.file)
            resource = f"{p.resource}: " if p.resource else ""
            print(f"{where}: {p.severity}: {resource}{p.message}")
        warnings = len(problems) - len(errors)
        summary = f"{len(project.resources)} resources in {len(project.files)} files"
        if problems:
            print(f"\n{len(errors)} error(s), {warnings} warning(s); {summary}")
        else:
            print(f"Valid: {summary}")
    return 1 if errors else 0


def cmd_discover(args) -> int:
    try:
        return discover(
            args.source, args.product, Path(args.dir), fmt=args.format,
            layers=not args.no_layers, use_cache=not args.refresh,
            search=args.search, limit=args.limit, resource=args.resource, url=args.url,
        )
    except DiscoverError as e:
        print(f"gisdata discover: {e}", file=sys.stderr)
        return e.code


def cmd_plan(args) -> int:
    try:
        return plan(Path(args.dir), apply=args.command == "apply", prune=args.prune, geoserver_url=args.geoserver)
    except (PlanError, SyncError) as e:
        print(f"gisdata {args.command}: {e}", file=sys.stderr)
        return e.code


def cmd_sync(args) -> int:
    try:
        return sync(Path(args.dir), args.datasets, force=args.force)
    except SyncError as e:
        print(f"gisdata sync: {e}", file=sys.stderr)
        return e.code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gisdata", description="Version-controlled open GIS datasets for GeoServer Cloud.")
    parser.add_argument("--version", action="version", version=f"gisdata {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")

    v = commands.add_parser("validate", help="check the files against the spec (no network)")
    v.add_argument("dir", nargs="?", default=".", help="project directory (default: current)")
    v.add_argument("--format", choices=["text", "json"], default="text")
    v.set_defaults(func=cmd_validate)

    d = commands.add_parser(
        "discover", help="list what a source offers, or the layers inside one product",
        description="List the products a source offers, or the downloads and layers of one product. "
        "SOURCE is a Source in the project (e.g. os) or a source type: os-downloads, ckan, wfs or ogc-api-features (with --url), "
        "or url (the product is then a file's address).",
    )
    d.add_argument("source", help="a Source name from the project, or a source type")
    d.add_argument("product", nargs="?", help="a product ID (a CKAN package, a file's URL), to list its downloads and layers")
    d.add_argument("--search", metavar="WORDS", help="list only products matching these words")
    d.add_argument("--limit", type=int, metavar="N", help="list at most N products (CKAN default: 20)")
    d.add_argument("--resource", metavar="ID", help="read this download of the product (a CKAN resource ID or name)")
    d.add_argument("--url", help="the source's address, e.g. a CKAN portal (overrides the Source's spec.url)")
    d.add_argument("-C", "--dir", default=".", help="project directory, for Sources and secrets (default: current)")
    d.add_argument("--format", choices=["text", "json"], default="text")
    d.add_argument("--no-layers", action="store_true", help="list downloads only; don't read layers from the file")
    d.add_argument("--refresh", action="store_true", help="read layers again instead of using the cache")
    d.set_defaults(func=cmd_discover)

    for name, what in (
        ("plan", "show what apply would change in GeoServer (exit code 2 when there are changes)"),
        ("apply", "make GeoServer match the files: workspace, stores, styles, first loads, layers, groups"),
    ):
        c = commands.add_parser(name, help=what, description=what[0].upper() + what[1:] + ".")
        c.add_argument("-C", "--dir", default=".", help="project directory (default: current)")
        c.add_argument("--prune", action="store_true",
                       help="also delete what the project's workspaces hold that the files don't mention")
        c.add_argument("--geoserver", metavar="URL", help="GeoServer's address, if not the Project's (e.g. through a port-forward)")
        c.set_defaults(func=cmd_plan)

    s = commands.add_parser(
        "sync", help="load new versions of datasets into PostGIS",
        description="Check each due Dataset for a new version and load it: stage, check, back up, swap in. "
        "Named datasets are checked whatever their schedule. The database comes from the Project's connection secret.",
    )
    s.add_argument("datasets", nargs="*", help="only these Datasets, due or not")
    s.add_argument("-C", "--dir", default=".", help="project directory (default: current)")
    s.add_argument("--force", action="store_true", help="load even when the version hasn't changed")
    s.set_defaults(func=cmd_sync)

    for name, what in LATER.items():
        c = commands.add_parser(name, help=f"{what} (not built yet)")
        c.set_defaults(func=lambda args, name=name: not_yet(name))

    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["discover"]:
        # options may come between the source and the product
        args = d.parse_intermixed_args(argv[1:])
        args.command = "discover"
    else:
        args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    return args.func(args)


def not_yet(name: str) -> int:
    print(f"gisdata {name}: not built yet; see docs/spec/datasets.md", file=sys.stderr)
    return 2
