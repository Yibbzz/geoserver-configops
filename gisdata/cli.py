"""The gisdata command."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .loader import load
from .validate import validate

LATER = {
    "plan": "compare the files with live GeoServer and PostGIS",
    "apply": "make the changes plan shows",
    "sync": "load new versions of datasets",
    "status": "show datasets and layers as they are live",
    "import": "write files that describe a live GeoServer workspace",
    "discover": "list what a source offers",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gisdata", description="Version-controlled open GIS datasets for GeoServer Cloud.")
    parser.add_argument("--version", action="version", version=f"gisdata {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")

    v = commands.add_parser("validate", help="check the files against the spec (no network)")
    v.add_argument("dir", nargs="?", default=".", help="project directory (default: current)")
    v.add_argument("--format", choices=["text", "json"], default="text")
    v.set_defaults(func=cmd_validate)

    for name, what in LATER.items():
        c = commands.add_parser(name, help=f"{what} (not built yet)")
        c.set_defaults(func=lambda args, name=name: not_yet(name))

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    return args.func(args)


def not_yet(name: str) -> int:
    print(f"gisdata {name}: not built yet; see docs/spec/datasets.md", file=sys.stderr)
    return 2
