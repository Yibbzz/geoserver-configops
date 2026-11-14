"""URLCheck resources: the external URLs GeoServer may fetch (remote styles, graphics, WMS/WFS references).

GeoServer refuses every external URL no check allows while URL checks are on, which they
are in GeoServer Cloud by default (the switch is the global Settings' `urlChecks`). A
project manages URL checks only when it has at least one; then checks the files don't
name are unmanaged, removed only with --prune.
"""

from __future__ import annotations

from .geoserver import q
from .loader import Resource

FIELDS = ("description", "enabled", "regex")


def check_name(r: Resource) -> str:
    return r.spec.get("name") or r.name or ""


def wanted(r: Resource) -> dict:
    spec = r.spec
    return {"description": spec.get("description", ""), "enabled": spec.get("enabled", True), "regex": spec["regex"]}


def live_checks(gs) -> dict[str, dict]:
    """GeoServer's URL checks by name: {description, enabled, regex}."""
    items = ((gs.get("urlchecks") or {}).get("urlChecks") or {}) or {}
    items = items.get("urlCheck") or []
    out = {}
    for item in items if isinstance(items, list) else [items]:
        name = item["name"]
        check = (gs.get(f"urlchecks/{q(name)}") or {}).get("regexUrlCheck") or {}
        out[name] = {"description": check.get("description") or "", "enabled": check.get("enabled", True),
                     "regex": check.get("regex", "")}
    return out


def plan(planner) -> None:
    files, gs = planner.files, planner.gs
    resources = files.of_kind("URLCheck")
    if not resources:
        return
    live = live_checks(gs)
    names = set()
    for r in resources:
        name = check_name(r)
        names.add(name)
        want = wanted(r)
        body = {"regexUrlCheck": {"name": name, **want}}
        if name not in live:
            planner.add("+", "url check", name, note=want["regex"],
                        run=lambda b=body: gs.request("POST", "urlchecks", b))
            continue
        changed = [(k, live[name][k], want[k]) for k in FIELDS if live[name][k] != want[k]]
        if changed:
            planner.add("~", "url check", name, changed,
                        run=lambda n=name, b=body: gs.request("PUT", f"urlchecks/{q(n)}", b))
    for name in sorted(set(live) - names):
        if planner.prune:
            planner.add("-", "url check", name, note="not in the files",
                        run=lambda n=name: gs.request("DELETE", f"urlchecks/{q(n)}"))
        else:
            planner.add("?", "url check", name, note="unmanaged; removed with --prune")
