"""Gridset resources: tile grids of your own for GeoWebCache, such as British National Grid.

GeoWebCache has many gridsets of its own (EPSG:4326, EPSG:900913, UTM zones);
only the ones the files name are compared, and none is ever removed.
"""

from __future__ import annotations




def wanted(spec: dict) -> dict:
    return {"crs": spec["crs"], "extent": [float(v) for v in spec["extent"]], "resolutions": [float(r) for r in spec["resolutions"]],
            "tileSize": spec.get("tileSize", 256), "alignTopLeft": spec.get("alignTopLeft", False),
            "metersPerUnit": float(spec.get("metersPerUnit", 1))}


def live(gridset: dict) -> dict:
    tile = gridset.get("tileWidth")
    return {"crs": f"EPSG:{(gridset.get('srs') or {}).get('number')}", "extent": [float(v) for v in (gridset.get("extent") or {}).get("coords", [])],
            "resolutions": [float(r) for r in gridset.get("resolutions") or []], "tileSize": tile,
            "alignTopLeft": gridset.get("alignTopLeft", False), "metersPerUnit": float(gridset.get("metersPerUnit") or 1)}


def plan(planner) -> None:
    gs = planner.gs
    for r in planner.files.of_kind("Gridset"):
        name = r.spec.get("name") or r.name
        want = wanted(r.spec)

        def run(name=name, want=want):
            gs.put_gridset(name, int(want["crs"].split(":", 1)[1]), want["extent"], want["resolutions"], want["tileSize"],
                           want["alignTopLeft"], want["metersPerUnit"])
        current = gs.gridset(name)
        if current is None:
            planner.add("+", "gridset", name, note=f"{want['crs']}, {len(want['resolutions'])} levels", run=run)
            continue
        have = live(current)
        changed = [(k, have[k], v) for k, v in want.items() if have[k] != v]
        if changed:
            planner.add("~", "gridset", name, changed, run=run)



