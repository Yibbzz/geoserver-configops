"""Import/export round trip against the real GeoServer Cloud REST service.

Start it first with tests/e2e/up.sh, then:

    E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_DB_PORT=55433 pytest -m e2e

Skipped unless E2E_GEOSERVER_URL is set. The catalog is wiped at the start, so
never point this at a GeoServer you care about.
"""

import json
import os
import subprocess

import psycopg2
import pytest
import requests

from conftest import ROOT

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL:
    pytest.skip("E2E_GEOSERVER_URL not set", allow_module_level=True)

AUTH = ("admin", os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver"))
CONFIG = ROOT / "geoserver-config"
FEATURETYPES = sorted(CONFIG.glob("workspaces/*/datastores/*/featuretypes/*/featuretype.json"))
CUSTOM_STYLES = sorted(
    p.stem for p in CONFIG.glob("styles/*.sld") if p.stem not in {"generic", "line", "point", "polygon", "raster"}
)

PG_TYPES = {
    "java.lang.String": "text", "java.lang.Integer": "integer", "java.lang.Long": "bigint",
    "java.lang.Double": "double precision", "java.lang.Float": "real", "java.lang.Boolean": "boolean",
    "java.math.BigDecimal": "numeric", "java.sql.Date": "date", "java.sql.Timestamp": "timestamp",
    "java.util.Date": "timestamp", "java.lang.Short": "smallint",
}


def create_tables():
    """Empty tables matching each exported layer, so GeoServer can publish them."""
    conn = psycopg2.connect(
        host=os.environ.get("E2E_DB_HOST", "localhost"), port=int(os.environ.get("E2E_DB_PORT", 55433)),
        dbname="postgres", user="postgres", password=os.environ.get("E2E_DB_PASSWORD", "postgres"),
    )
    with conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis; CREATE SCHEMA IF NOT EXISTS data_layers")
        for path in FEATURETYPES:
            ft = json.loads(path.read_text())["featureType"]
            attrs = ft.get("attributes", {}).get("attribute", [])
            attrs = [attrs] if isinstance(attrs, dict) else attrs
            cols = []
            for a in attrs:
                binding = a["binding"]
                if binding.startswith("org.locationtech.jts.geom."):
                    cols.append(f'"{a["name"]}" geometry({binding.rsplit(".", 1)[1]}, 27700)')
                else:
                    cols.append(f'"{a["name"]}" {PG_TYPES.get(binding, "text")}')
            cur.execute(f'DROP TABLE IF EXISTS data_layers."{ft["nativeName"]}"')
            cur.execute(f'CREATE TABLE data_layers."{ft["nativeName"]}" ({", ".join(cols)})')
    conn.close()


def wipe_catalog():
    for ws in {p.parts[-6] for p in FEATURETYPES}:
        requests.delete(f"{URL}/workspaces/{ws}?recurse=true", auth=AUTH, timeout=60)
    for style in CUSTOM_STYLES:
        requests.delete(f"{URL}/styles/{style}?purge=true", auth=AUTH, timeout=60)


def run(script, config_dir):
    env = {**os.environ, "GEOSERVER_URL": URL, "GEOSERVER_PASSWORD": AUTH[1], "CONFIG_DIR": str(config_dir)}
    result = subprocess.run([str(ROOT / script)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def stable(path):
    """An exported JSON file without the fields that change on every save."""
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in ("dateCreated", "dateModified")}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    return strip(json.loads(path.read_text()))


@pytest.fixture(scope="module")
def imported():
    create_tables()
    wipe_catalog()
    first = run("import-geoserver-config.sh", CONFIG)
    second = run("import-geoserver-config.sh", CONFIG)
    return first, second


def test_import_creates_then_updates(imported):
    first, second = imported
    assert "HTTP 201" in first
    assert "HTTP 201" not in second, "re-import should update, not create"


def test_layers_are_published(imported):
    layers = requests.get(f"{URL}/layers.json", auth=AUTH, timeout=60).json()["layers"]["layer"]
    names = {layer["name"] for layer in layers}
    for path in FEATURETYPES:
        ft = json.loads(path.read_text())["featureType"]
        assert f'{ft["namespace"]["name"]}:{ft["name"]}' in names


def test_styles_round_trip_byte_for_byte(imported, tmp_path):
    run("export-geoserver-config.sh", tmp_path)
    for style in CUSTOM_STYLES:
        exported = (tmp_path / "styles" / f"{style}.sld").read_bytes()
        assert exported == (CONFIG / "styles" / f"{style}.sld").read_bytes(), style


def test_export_is_stable_across_reimport(imported, tmp_path):
    """Export, import that export, export again: nothing but timestamps may change."""
    first, second = tmp_path / "first", tmp_path / "second"
    run("export-geoserver-config.sh", first)
    run("import-geoserver-config.sh", first)
    run("export-geoserver-config.sh", second)

    # only this test's workspaces: others on the same GeoServer (gisdata's, in the kind test) aren't its to check
    own = {path.parts[-6] for path in FEATURETYPES}

    def mine(rel):
        return rel.parts[0] != "workspaces" or len(rel.parts) == 1 or rel.parts[1] in own

    files = sorted(rel for rel in (p.relative_to(first) for p in first.rglob("*") if p.is_file()) if mine(rel))
    assert files == sorted(rel for rel in (p.relative_to(second) for p in second.rglob("*") if p.is_file()) if mine(rel))
    for rel in files:
        if rel.suffix == ".json":
            assert stable(first / rel) == stable(second / rel), rel
        else:
            assert (first / rel).read_bytes() == (second / rel).read_bytes(), rel


def test_exported_layers_keep_their_settings(imported, tmp_path):
    run("export-geoserver-config.sh", tmp_path)
    for path in FEATURETYPES:
        rel = path.relative_to(CONFIG)
        before = json.loads(path.read_text())["featureType"]
        after = json.loads((tmp_path / rel).read_text())["featureType"]
        for key in ("name", "nativeName", "srs", "enabled", "projectionPolicy"):
            assert after.get(key) == before.get(key), f"{rel}: {key}"
