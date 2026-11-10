"""`gisdata status`: how the project's datasets and layers are, live.

Datasets from the ingest log (what was loaded, when, the last error, the
next check); layers and groups from GeoServer (published, drifted from the
files, tiles cached). Nothing is changed. Without GeoServer credentials,
the datasets are still shown.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import psycopg2

from . import ingest, schedule, sync
from .discover import table, version
from .geoserver import GeoServerError, MissingCredentials, for_project
from .loader import load
from .plan import Planner, PlanError
from .validate import geoserver_name, validate


def dataset_rows(conn, files) -> list[dict]:
    ingest.bootstrap(conn)
    rows = []
    with conn.cursor() as cur:
        for name, dataset in sorted(files.named("Dataset").items()):
            cur.execute(
                f"SELECT version, finished_at, rows_loaded, tables FROM {ingest.LOG_TABLE} "
                "WHERE dataset_id = %s AND status = 'success' ORDER BY finished_at DESC LIMIT 1", (name,))
            loaded = cur.fetchone()
            cur.execute(
                f"SELECT status, error, started_at FROM {ingest.LOG_TABLE} WHERE dataset_id = %s "
                "ORDER BY started_at DESC, id DESC LIMIT 1", (name,))
            last = cur.fetchone()
            update = dataset.spec.get("update") or "manual"
            nxt = schedule.next_check(update, ingest.last_check(conn, name))
            rows.append({
                "dataset": name,
                "update": update,
                "version": loaded[0] if loaded else None,
                "loadedAt": loaded[1].isoformat(timespec="minutes") if loaded and loaded[1] else None,
                "rows": loaded[2] if loaded else None,
                "tables": [t for t in (loaded[3] or "").split(",") if t] if loaded else [],
                "lastCheck": last[0] if last else None,
                "lastError": last[1] if last and last[0] == "failed" else None,
                "nextCheck": nxt.isoformat(timespec="minutes") if nxt else None,
            })
    return rows


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


def when(iso: str | None, now: datetime) -> str:
    if not iso:
        return "-"
    t = datetime.fromisoformat(iso)
    return "now" if t <= now else iso.replace("T", " ")


def print_status(datasets: list[dict], layers: list[dict] | None, note: str) -> None:
    now = datetime.now()
    if datasets:
        rows = [["DATASET", "UPDATE", "LOADED", "AT", "ROWS", "NEXT CHECK", "LAST CHECK"]]
        for d in datasets:
            last = d["lastCheck"] or "-"
            if d["lastError"]:
                last = f"failed: {' '.join(d['lastError'].split())[:80]}"
            rows.append([d["dataset"], d["update"], version(d["version"]) if d["version"] else "never",
                         (d["loadedAt"] or "-").replace("T", " "), f"{d['rows']:,}" if d["rows"] is not None else "-",
                         when(d["nextCheck"], now) if d["update"] != "manual" else "manual", last])
        print(table(rows))
    if layers:
        print()
        rows = [["LAYER", "PUBLISHED", "DRIFTED", "TILES CACHED"]]
        for x in layers:
            drift = ", ".join(x["drifted"]) if x["drifted"] else "no"
            rows.append([f"{x['name']}" + (" (group)" if x["kind"] == "group" else ""),
                         "yes" if x["published"] else ("no: " + x["problem"] if x["problem"] else "no"),
                         drift if x["published"] else "-", "yes" if x["cached"] else "no"])
        print(table(rows))
        if any(x["drifted"] or not x["published"] for x in layers):
            print("\n`gisdata plan` shows the changes; `gisdata apply` makes them.")
    if note:
        print(f"\nnote: {note}")


def run(root: Path, fmt: str = "text", geoserver_url: str | None = None) -> int:
    files = load(root)
    errors = [p for p in validate(files) if p.severity == "error"]
    if errors:
        raise PlanError(f"the files have {len(errors)} error(s); `gisdata validate` lists them")
    project = files.of_kind("Project")[0]
    db = sync.database(project, root)
    try:
        conn = db.connect()
    except psycopg2.OperationalError as e:
        raise PlanError(f"can't connect to the database at {db.host}:{db.port}: {str(e).strip().splitlines()[0]}", code=1) from None
    note, layers = "", None
    try:
        datasets = dataset_rows(conn, files)
        try:
            gs = for_project(project, root, geoserver_url)
            layers = layer_rows(Planner(files, root, gs, conn, db))
        except MissingCredentials as e:
            note = f"layers not shown: {e}"
        except GeoServerError as e:
            note = f"layers not shown: {e}"
    finally:
        conn.close()
    if fmt == "json":
        print(json.dumps({"datasets": datasets, "layers": layers, "note": note or None}, indent=2, ensure_ascii=False))
    else:
        print_status(datasets, layers, note)
    return 0
