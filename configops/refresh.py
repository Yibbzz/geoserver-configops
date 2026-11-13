"""`configops refresh`: after new data, bring GeoServer up to date with it.

For the layers on the named tables (`schema.table`) or files (a file store's `path`, under
/data): work out their bounding boxes again (resetting a raster store first, so GeoServer
reads the new file), clear their cached tiles and those of the groups that hold them,
directly or through other groups, and seed again what the files seed. Whatever loaded the
data runs it afterwards: `geodata sync --loaded FILE`, then `configops refresh --from FILE`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .database import schema_of
from .geoserver import GeoServer, GeoServerError, MissingCredentials, for_project
from .loader import Project, Resource, load
from .plan import PlanError, seed_cache, seed_of, wanted_cache
from .stores import DATA, store_path
from .validate import geoserver_name, members, validate


def layer_table(layer: Resource, schema: str) -> str | None:
    """`schema.table` of a layer on a table, or None (a SQL view, a store's layer)."""
    table = layer.spec.get("table")
    if isinstance(table, dict):
        return f"{table['schema']}.{table['name']}"
    return f"{schema}.{table}" if isinstance(table, str) else None


def normal_path(path: str) -> str:
    return path.removeprefix(DATA).strip("/")


def refresh(gs: GeoServer, files: Project, targets: list[str] | None) -> str:
    """Refresh the layers on `targets` (all layers on tables and file stores when None); returns what was done."""
    project = files.of_kind("Project")[0]
    default_ws, schema = project.spec["workspace"], schema_of(project)
    wanted = {normal_path(t) if "/" in t else t for t in targets} if targets is not None else None

    def chosen(key: str | None) -> bool:
        return key is not None and (wanted is None or key in wanted)

    stores = files.named("Store")
    table_layers = [r for r in files.of_kind("Layer") if chosen(layer_table(r, schema))]
    file_stores = {name: s for name, s in stores.items() if s.spec.get("path") and chosen(normal_path(s.spec["path"]))}
    store_layers = [r for r in files.of_kind("Layer") if r.spec.get("store") in file_stores]
    names = {r.name for r in table_layers + store_layers}
    if not names:
        return "nothing published on " + (", ".join(sorted(wanted)) if wanted else "tables or files")

    groups = files.named("LayerGroup")
    holding: set[str] = set()
    changed = True
    while changed:  # groups that hold the layers, or groups that hold those
        changed = False
        for name, group in groups.items():
            if name not in holding and any(m in names or m in holding for _, m in members(group)):
                holding.add(name)
                changed = True

    by_target = {f"{r.spec.get('workspace') or default_ws}:{geoserver_name(r)}": r
                 for r in files.of_kind("Layer") + files.of_kind("LayerGroup")}
    refreshed, cleared, seeded = 0, 0, 0

    def clear(target: str) -> None:
        nonlocal cleared, seeded
        if not gs.tile_layer(target):
            return
        gs.truncate(target)
        cleared += 1
        r = by_target.get(target)
        seed = seed_of(r.spec) if r else None
        if seed:
            seed_cache(gs, target, wanted_cache(r.spec["cache"]), seed)
            seeded += 1

    for store in file_stores.values():
        ws = store.spec.get("workspace") or default_ws
        path, _ = store_path(ws, store)
        if gs.get(path) is None:
            continue  # not made yet: apply does that
        if path.split("/")[2] == "coveragestores":
            gs.request("POST", f"{path}/reset")  # forget the old file's details
    for r in store_layers:
        store = file_stores[r.spec["store"]]
        sws, ws, name = store.spec.get("workspace") or default_ws, r.spec.get("workspace") or default_ws, geoserver_name(r)
        path, _ = store_path(sws, store)
        collection = "coverages" if path.split("/")[2] == "coveragestores" else "featuretypes"
        item = f"{path}/{collection}/{name}"
        if gs.get(item) is None:
            continue
        key = "coverage" if collection == "coverages" else "featureType"
        gs.request("PUT", item, {key: {"enabled": r.spec.get("enabled", True)}}, params={"calculate": "nativebbox,latlonbbox"})
        refreshed += 1
        clear(f"{ws}:{name}")
    for r in table_layers:
        ws, name = r.spec.get("workspace") or default_ws, geoserver_name(r)
        table_schema = layer_table(r, schema).split(".", 1)[0]
        if gs.featuretype(ws, table_schema, name) is None:
            continue  # not published yet: apply does that
        gs.recalculate(ws, table_schema, name)
        refreshed += 1
        clear(f"{ws}:{name}")
    for name in sorted(holding):
        g = groups[name]
        clear(f"{g.spec.get('workspace') or default_ws}:{geoserver_name(g)}")
    note = f"; seeding {seeded} again" if seeded else ""
    return f"bounds of {refreshed} layer(s) worked out again, cached tiles of {cleared} layer(s) or group(s) cleared{note}"


def run(root: Path, targets: list[str], from_file: Path | None = None, geoserver_url: str | None = None) -> int:
    files = load(root)
    errors = [p for p in validate(files) if p.severity == "error"]
    if errors:
        raise PlanError(f"the files have {len(errors)} error(s); `configops validate` lists them")
    if from_file is not None:
        targets = targets + [line.strip() for line in Path(from_file).read_text(encoding="utf-8").splitlines() if line.strip()]
        if not targets:
            print("Nothing was loaded: nothing to refresh.")
            return 0
    try:
        gs = for_project(files.of_kind("Project")[0], root, geoserver_url)
        print(f"Refreshed: {refresh(gs, files, targets or None)}")
    except MissingCredentials as e:
        raise PlanError(str(e)) from None
    except GeoServerError as e:
        print(f"configops refresh: {e}", file=sys.stderr)
        return 1
    return 0
