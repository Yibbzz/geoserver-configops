"""The configops command."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .plan import PlanError
from .importer import run as import_workspace
from .plan import run as plan
from .status import run as status
from .refresh import run as refresh
from .loader import load
from .validate import validate

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
        print(f"configops: {root} is not a directory", file=sys.stderr)
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


def cmd_plan(args) -> int:
    try:
        return plan(Path(args.dir), apply=args.command == "apply", prune=args.prune, geoserver_url=args.geoserver,
                    wait=args.wait)
    except PlanError as e:
        print(f"configops {args.command}: {e}", file=sys.stderr)
        return e.code


def cmd_status(args) -> int:
    try:
        return status(Path(args.dir), fmt=args.format, geoserver_url=args.geoserver)
    except PlanError as e:
        print(f"configops status: {e}", file=sys.stderr)
        return e.code


def cmd_import(args) -> int:
    try:
        return import_workspace(Path(args.dir), args.workspace, geoserver_url=args.geoserver, force=args.force,
                                settings=args.settings, security=args.security, prune=args.prune)
    except PlanError as e:
        print(f"configops import: {e}", file=sys.stderr)
        return e.code


def cmd_refresh(args) -> int:
    try:
        return refresh(Path(args.dir), args.targets, from_file=args.from_file, geoserver_url=args.geoserver)
    except PlanError as e:
        print(f"configops refresh: {e}", file=sys.stderr)
        return e.code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="configops", description="GeoServer configuration as code: plan, apply, import.")
    parser.add_argument("--version", action="version", version=f"configops {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")

    v = commands.add_parser("validate", help="check the files against the spec (no network)")
    v.add_argument("dir", nargs="?", default=".", help="project directory (default: current)")
    v.add_argument("--format", choices=["text", "json"], default="text")
    v.set_defaults(func=cmd_validate)

    for name, what in (
        ("plan", "show what apply would change in GeoServer (exit code 2 when there are changes, 3 when only ! lines are left: layers waiting for their tables, settings GeoServer Cloud can't take)"),
        ("apply", "make GeoServer match the files: workspace, stores, styles, layers, groups, settings, security"),
    ):
        c = commands.add_parser(name, help=what, description=what[0].upper() + what[1:] + ".")
        c.add_argument("-C", "--dir", default=".", help="project directory (default: current)")
        c.add_argument("--prune", action="store_true",
                       help="also delete what the project's workspaces hold that the files don't mention")
        c.add_argument("--geoserver", metavar="URL", help="GeoServer's address, if not the Project's (e.g. through a port-forward)")
        c.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                       help="wait this long for GeoServer to answer first (a job started with a fresh install)")
        c.set_defaults(func=cmd_plan)

    st = commands.add_parser("status", help="layers and groups: published, drifted, cached",
                             description="Show the project's layers and groups as GeoServer has them. Changes nothing.")
    st.add_argument("-C", "--dir", default=".", help="project directory (default: current)")
    st.add_argument("--format", choices=["text", "json"], default="text")
    st.add_argument("--geoserver", metavar="URL", help="GeoServer's address, if not the Project's")
    st.set_defaults(func=cmd_status)

    im = commands.add_parser(
        "import", help="write the files that describe a live GeoServer: a workspace, its settings, its security",
        description="Read a GeoServer workspace and write the files that describe it: its layers and groups in "
        "<workspace>.yaml, its styles in styles/, and project.yaml if the directory has none. --settings writes "
        "server.yaml (settings and services), --security writes security.yaml (roles, groups, users, access rules). "
        "In a project that has files already, only what changed in GeoServer is changed in them, wherever it is, and "
        "new things are added; --prune also removes what GeoServer no longer has. Only what differs from the "
        "defaults is written, so `configops plan` on the result has nothing to do.",
    )
    im.add_argument("workspace", nargs="?", help="the GeoServer workspace to read")
    im.add_argument("--settings", action="store_true", help="also write server.yaml: GeoServer's settings and services")
    im.add_argument("--security", action="store_true", help="also write security.yaml: roles, groups, users and access rules")
    im.add_argument("-C", "--dir", default=".", help="project directory to write into (default: current)")
    im.add_argument("--geoserver", metavar="URL", help="GeoServer's address (needed when the directory has no Project yet)")
    im.add_argument("--force", action="store_true", help="write fresh files, replacing those that exist, rather than updating them")
    im.add_argument("--prune", action="store_true", help="also remove from the files what GeoServer no longer has")
    im.set_defaults(func=cmd_import)

    r = commands.add_parser(
        "refresh", help="after new data: work out layers' bounds again and clear their cached tiles",
        description="For the layers on the named tables (schema.table) or files (a store's path under /data), or on all of "
        "them if none are named: reset raster stores, work out the bounds again, clear the cached tiles of the layers and "
        "the groups that hold them, and seed again what the files seed. Run it after loading new data "
        "(geodata sync --loaded FILE, then configops refresh --from FILE).",
    )
    r.add_argument("targets", nargs="*", metavar="TABLE_OR_PATH", help="schema.table, or a file store's path")
    r.add_argument("--from", dest="from_file", type=Path, metavar="FILE", help="read the tables and paths from FILE, one per line")
    r.add_argument("-C", "--dir", default=".", help="project directory (default: current)")
    r.add_argument("--geoserver", metavar="URL", help="GeoServer's address, if not the Project's")
    r.set_defaults(func=cmd_refresh)

    argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    return args.func(args)
