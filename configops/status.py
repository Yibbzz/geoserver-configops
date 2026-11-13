"""`configops status`: the project's layers and groups as GeoServer has them: published,
drifted from the files, tiles cached. Nothing is changed."""

from __future__ import annotations

import json
from pathlib import Path

from .geoserver import GeoServerError, MissingCredentials
from .plan import Planner, PlanError, connect
from .validate import geoserver_name


def table(rows: list[list[str]]) -> str:
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


def layer_rows(planner: Planner) -> list[dict]:
    changes = planner.plan()
    drifted = {c.target: [f for f, _, _ in c.fields] or [c.note] for c in changes if c.action == "~" and c.kind in ("layer", "group")}
    blocked = {c.target: c.note for c in changes if c.action == "!"}
    rows = []
    for kind, resources in (("layer", planner.layers), ("group", planner.groups)):
        for r in resources.values():
            ws, name = planner.workspace_of(r), geoserver_name(r)
            target = f"{ws}:{name}"
            live = planner.gs.layer(ws, name) if kind == "layer" else planner.gs.layergroup(name, ws)
            rows.append({
                "kind": kind, "name": target,
                "published": live is not None,
                "drifted": drifted.get(target, []),
                "cached": planner.gs.tile_layer(target) is not None if live is not None else False,
                "problem": blocked.get(target),
            })
    return rows


def print_status(layers: list[dict]) -> None:
    if not layers:
        print("No layers or groups.")
        return
    rows = [["LAYER", "PUBLISHED", "DRIFTED", "TILES CACHED"]]
    for x in layers:
        drift = ", ".join(x["drifted"]) if x["drifted"] else "no"
        rows.append([f"{x['name']}" + (" (group)" if x["kind"] == "group" else ""),
                     "yes" if x["published"] else ("no: " + x["problem"] if x["problem"] else "no"),
                     drift if x["published"] else "-", "yes" if x["cached"] else "no"])
    print(table(rows))
    if any(x["drifted"] or not x["published"] for x in layers):
        print("\n`configops plan` shows the changes; `configops apply` makes them.")


def run(root: Path, fmt: str = "text", geoserver_url: str | None = None) -> int:
    files, gs, conn, db = connect(root, geoserver_url)
    try:
        layers = layer_rows(Planner(files, root, gs, conn, db))
    except (GeoServerError, MissingCredentials) as e:
        raise PlanError(str(e), code=1) from None
    finally:
        if conn is not None:
            conn.close()
    if fmt == "json":
        print(json.dumps({"layers": layers}, indent=2, ensure_ascii=False))
    else:
        print_status(layers)
    return 0
