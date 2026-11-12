"""Which of GeoServer's settings the e2e tests prove configops sets, worked out after they've run.

tests/conftest.py records what every e2e test writes to GeoServer and reads back. This test
runs last and replays it (tests/geoserver_coverage.py's Evidence): a `yes` field in
docs/geoserver-fields.yaml is proved when configops changed it and GeoServer then showed the
new value, or when the test its entry names passed.

It fails when GeoServer shows a field the list doesn't have (sort it), when GeoServer isn't
the version the list was read from, or when a field docs/geoserver-tested.txt says is proved
was written and read back wrong. With COVERAGE_WRITE=1 it adds what this run proved to
docs/geoserver-tested.txt and updates the README's figure:

    COVERAGE_WRITE=1 E2E_GEOSERVER_URL=... pytest -m e2e
"""

import json
import os
import warnings

import pytest
import requests

import conftest
from tests import geoserver_coverage as coverage

pytestmark = [pytest.mark.e2e, pytest.mark.last]

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL:
    pytest.skip("E2E_GEOSERVER_URL not set", allow_module_level=True)
AUTH = ("admin", os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver"))


def get(path: str) -> dict:
    r = requests.get(f"{URL}{path}.json", auth=AUTH, timeout=60)
    r.raise_for_status()
    return r.json()


def names(listing: dict) -> list[str]:
    """Names from a REST listing: {"dataStores": {"dataStore": [{name}, ...]}}, or "" for none."""
    inner = next(iter(listing.values()))
    if not isinstance(inner, dict):
        return []
    items = next(iter(inner.values()))
    return [i["name"] for i in (items if isinstance(items, list) else [items])]


def walk_server() -> None:
    """Read what's on the server now (through the recorder), so its fields are checked too."""
    get("/settings")
    get("/settings/contact")
    for service in ("wms", "wfs", "wcs", "wmts"):
        get(f"/services/{service}/settings")
    for ws in names(get("/workspaces")):
        get(f"/workspaces/{ws}")
        get(f"/namespaces/{ws}")
        for kind in ("datastores", "coveragestores", "wmsstores", "wmtsstores", "layergroups", "styles", "layers",
                     "featuretypes", "coverages"):
            for name in names(get(f"/workspaces/{ws}/{kind}")):
                get(f"/workspaces/{ws}/{kind}/{name}")


@pytest.fixture(scope="module")
def evidence():
    walk_server()
    if os.environ.get("COVERAGE_EVENTS"):  # to look into what was proved and why: a JSON file of every request
        with open(os.environ["COVERAGE_EVENTS"], "w") as f:
            json.dump([(k, p, b if isinstance(b, (dict, list, str)) or b is None else b.decode("utf-8", "replace"))
                       for k, p, b in conftest.LIVE_EVENTS], f)
    manifest = coverage.load()
    evidence = coverage.Evidence(manifest)
    for kind, path, body in conftest.LIVE_EVENTS:
        (evidence.write if kind == "write" else evidence.read)(path, body)
    return manifest, evidence


def test_versions_are_the_ones_read():
    manifest = coverage.load()
    about = {r["@name"]: str(r.get("Version")) for r in get("/about/version")["about"]["resource"]}
    assert about["GeoServer"].removesuffix("-CLOUD") == manifest["versions"]["geoserver"], \
        "GeoServer changed: set versions in docs/geoserver-fields.yaml, then python tests/geoserver_coverage.py refresh"
    assert about["GeoWebCache"] == manifest["versions"]["geowebcache"]


def test_every_field_geoserver_shows_is_listed(evidence):
    _, ev = evidence
    assert len(conftest.LIVE_EVENTS) > 10
    assert not ev.unlisted, "fields GeoServer shows that docs/geoserver-fields.yaml doesn't list:\n" + \
        "\n".join(f"  {field} (seen at {path})" for field, path in sorted(ev.unlisted.items()))


def test_proved_fields_still_hold(evidence):
    """Work out what this run proved; fail if a field proved before now reads back wrong."""
    manifest, ev = evidence
    yes = coverage.yes_fields(manifest)
    proved = (ev.proved & yes) | coverage.named_tests(manifest, conftest.PASSED)
    before = coverage.tested()
    broken = {f: why for f, why in ev.mismatched.items() if f in before and f not in proved}
    if os.environ.get("COVERAGE_WRITE"):
        coverage.save_tested((before | proved) - set(broken))
        coverage.report(manifest)
    new = sorted(proved - before)
    warnings.warn(f"coverage: this run proved {len(proved)} of {len(yes)} yes fields, {len(new)} not in "
                  f"{coverage.TESTED.name}{': ' + ', '.join(new[:20]) if new else ''}"
                  + ("" if os.environ.get("COVERAGE_WRITE") else " (COVERAGE_WRITE=1 adds them)"))
    assert not broken, "fields proved before that GeoServer now shows differently:\n" + \
        "\n".join(f"  {f}: {why}" for f, why in sorted(broken.items()))
