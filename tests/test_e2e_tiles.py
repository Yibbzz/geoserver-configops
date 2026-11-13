"""Tile caching against the real GeoServer Cloud REST service and its GeoWebCache: a gridset of
our own, and a layer cached in it with metatiling, gutter and expiry; then import to a clean plan.

    E2E_GEOSERVER_URL=http://localhost:59090/geoserver-cloud/rest E2E_DB_PORT=55433 \
      pytest -m e2e
"""

import os

import psycopg2
import pytest

from configops.cli import main
from configops.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL:
    pytest.skip("E2E_GEOSERVER_URL not set", allow_module_level=True)

WS = "configops_tiles"
GRIDSET = "e2e-british-national-grid"
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=int(os.environ.get("E2E_DB_PORT", 55433)),
          dbname="postgres", user="postgres", password=os.environ.get("E2E_DB_PASSWORD", "postgres"))


def sql(statement):
    conn = psycopg2.connect(**DB)
    with conn, conn.cursor() as cur:
        cur.execute(statement)
    conn.close()


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD, gwc_url=os.environ.get("E2E_GWC_URL"))
    gs.delete_workspace(WS)
    sql("CREATE EXTENSION IF NOT EXISTS postgis; DROP SCHEMA IF EXISTS e2e_tiles CASCADE; CREATE SCHEMA e2e_tiles;"
        "CREATE TABLE e2e_tiles.points (id serial PRIMARY KEY, geom geometry(Point, 27700));"
        "INSERT INTO e2e_tiles.points (geom) VALUES (ST_SetSRID(ST_MakePoint(500000, 150000), 27700))")
    yield gs
    gs.delete_workspace(WS)
    gs.gwc_request("DELETE", f"gridsets/{GRIDSET}")
    gs.gwc_request("DELETE", "gridsets/e2e-by-scale")
    sql("DROP SCHEMA IF EXISTS e2e_tiles CASCADE")


@pytest.fixture
def env(monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": str(DB["port"]), "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": DB["user"],
                       "E2EDB_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    if os.environ.get("E2E_GWC_URL"):
        monkeypatch.setenv("CONFIGOPS_GWC_URL", os.environ["E2E_GWC_URL"])


def project_yaml() -> str:
    return f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-tiles }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
"""


@pytest.fixture
def project(tmp_path, env):
    (tmp_path / "project.yaml").write_text(project_yaml() + f"""---
apiVersion: configops/v1alpha1
kind: Gridset
metadata: {{ name: {GRIDSET} }}
spec:
  crs: EPSG:27700
  extent: [-238375, 0, 900000, 1376256]
  resolutions: [896, 448, 224, 112, 56, 28, 14]
  description: British National Grid for the e2e tests
  levelNames: [bng0, bng1, bng2, bng3, bng4, bng5, bng6]
  pixelSize: 0.0003
  yAxisFirst: true
---
apiVersion: configops/v1alpha1
kind: Gridset
metadata: {{ name: e2e-by-scale }}
spec: {{ crs: EPSG:27700, extent: [0, 0, 700000, 1300000], scales: [5000000, 1000000, 250000] }}
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: points }}
spec:
  table: {{ schema: e2e_tiles, name: points }}
  cache:
    gridsets: [{GRIDSET}, EPSG:900913]
    metaTiles: [2, 2]
    gutter: 10
    expireCache: 60
    expireClients: 30
    levels: {{ {GRIDSET}: {{ published: [1, 6], cached: [1, 4], bounds: [0, 0, 700000, 1300000] }} }}
    expireByZoom: {{ 0: 3600, 4: 60 }}
    warningSkips: [Default, Nearest]
    parameterFilters:
      ENV: {{ type: values, values: [A, B], default: A, case: upper, locale: en }}
      CQL_FILTER: {{ type: values, values: [INCLUDE, "id = 1"], default: INCLUDE }}
      TIME: {{ type: regex, regex: "[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}" }}
      ELEVATION: {{ type: numbers, values: [0, 10.5], default: 0, threshold: 0.01 }}
      STYLES: {{ type: styles, values: [point], default: point }}
      BUFFER: {{ type: integers, values: [0, 8, 16], default: 0, threshold: 2 }}
""")
    return tmp_path


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def test_apply_then_nothing_to_do(gs, project, capsys):
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    code, out = configops(capsys, "plan", "-C", str(project))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
    assert gs.gridset("e2e-by-scale")["scaleDenominators"] == [5000000, 1000000, 250000]
    gridset = gs.gridset(GRIDSET)
    assert gridset["resolutions"][-1] == 14 and gridset["description"] == "British National Grid for the e2e tests"
    assert gridset["yCoordinateFirst"] is True and float(gridset["pixelSize"]) == 0.0003 and "bng6" in str(gridset["scaleNames"])
    tiles = gs.tile_layer(f"{WS}:points")
    assert tiles["metaWidthHeight"] == [2, 2] and tiles["gutter"] == 10 and tiles["expireCache"] == 60
    filters = gs.parameter_filters(f"{WS}:points")
    assert filters["CQL_FILTER"]["values"] == ["INCLUDE", "id = 1"] and filters["ELEVATION"]["values"] == [0.0, 10.5]
    assert filters["TIME"]["regex"] == "[0-9]{4}-[0-9]{2}-[0-9]{2}" and filters["STYLES"]["values"] == ["point"]
    assert filters["STYLES"].get("default") == "point"
    assert filters["BUFFER"]["values"] == [0, 8, 16] and float(filters["BUFFER"]["threshold"]) == 2
    assert (filters["ENV"]["case"], filters["ENV"]["locale"]) == ("upper", "en")
    extras = gs.tile_layer_extras(f"{WS}:points")
    assert extras["levels"][GRIDSET] == {"published": [1, 6], "cached": [1, 4], "bounds": [0.0, 0.0, 700000.0, 1300000.0]}
    assert extras["expireByZoom"] == {0: 3600, 4: 60} and extras["warningSkips"] == ["Default", "Nearest"]


def test_a_changed_filter_is_drift(gs, project, capsys):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    gs.put_tile_layer(f"{WS}:points", [GRIDSET, "EPSG:900913"], ["image/png"],
                      {"metaTiles": [2, 2], "gutter": 10, "expireCache": 60, "expireClients": 30,
                       "parameterFilters": {"CQL_FILTER": {"type": "values", "values": ["INCLUDE"], "default": "INCLUDE"}}})
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2 and "cache." in out, out
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    assert configops(capsys, "plan", "-C", str(project))[0] == 0


def test_caching_defaults(gs, project, capsys):
    """Written to gwc-gs.xml, which GeoServer reads when it starts: plan compares the file."""
    saved = gs.request("GET", "resource/gwc-gs.xml").text
    try:
        (project / "settings.yaml").write_text("""\
apiVersion: configops/v1alpha1
kind: Settings
metadata: { name: global }
spec:
  global:
    tileCaching:                       # each one different from GeoWebCache's default
      cacheLayersByDefault: false
      metaTilingX: 2
      metaTilingY: 3
      gutter: 5
      defaultCachingGridSetIds: [EPSG:900913]
      WMTSEnabled: true
      directWMSIntegrationEnabled: true
      requireTiledParameter: false
      WMSCEnabled: false
      TMSEnabled: false
      securityEnabled: true
      cacheNonDefaultStyles: false
      defaultCoverageCacheFormats: [image/png]
      defaultVectorCacheFormats: [image/png8]
      defaultOtherCacheFormats: [image/gif]
      multiLayerCachingEnabled: true
      metaTilingThreads: 2
      seederCorePoolSize: 4
      seederMaxPoolSize: 8
      cacheWarningSkips: [Default, Nearest]
      lockProviderName: nioLock               # only written to the file: GeoServer reads it when it restarts, and this test puts it back
""")
        code, out = configops(capsys, "plan", "-C", str(project))
        assert code == 2 and "global.tileCaching.cacheLayersByDefault" in out, out
        code, out = configops(capsys, "apply", "-C", str(project))
        assert code == 0 and "rollout restart" in out, out
        text = gs.request("GET", "resource/gwc-gs.xml").text
        assert "<cacheLayersByDefault>false</cacheLayersByDefault>" in text and "<metaTilingY>3</metaTilingY>" in text
        assert "<securityEnabled>true</securityEnabled>" in text and "<string>image/png8</string>" in text
        assert "<multiLayerCachingEnabled>true</multiLayerCachingEnabled>" in text
        assert "<warning>Nearest</warning>" in text and "<lockProviderName>nioLock</lockProviderName>" in text
        code, out = configops(capsys, "plan", "-C", str(project))
        if gs.series and gs.series < (3, 0):  # the seeder's pool sizes came in GeoServer 3.0: left out, with the reason
            assert "<seederMaxPoolSize>" not in text
            assert code == 3 and f"GeoServer {gs.version} can't set global.tileCaching.seederMaxPoolSize: it came in GeoServer 3.0" in out, out
        else:
            assert "<seederMaxPoolSize>8</seederMaxPoolSize>" in text
            assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
    finally:
        gs.request("PUT", "resource/gwc-gs.xml", saved, content_type="application/xml")


def test_drift_and_import_round_trip(gs, project, tmp_path_factory, capsys, env):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    gs.put_tile_layer(f"{WS}:points", [GRIDSET, "EPSG:900913"], ["image/png"], {"metaTiles": [4, 4], "gutter": 10, "expireCache": 60,
                                                                                "expireClients": 30})
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2 and "cache." in out, out
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text(project_yaml())
    assert configops(capsys, "import", WS, "-C", str(imported))[0] == 0
    text = (imported / f"{WS}.yaml").read_text()
    assert "kind: Gridset" in text and "metaTiles:" in text
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out


@pytest.mark.skipif(not os.environ.get("E2E_REST_CONTAINER"), reason="E2E_REST_CONTAINER not set: can't restart GeoServer")
def test_caching_defaults_apply_after_a_restart(gs, project, capsys):
    """With cacheLayersByDefault off and GeoServer restarted, a new layer isn't cached by itself."""
    import subprocess
    import time

    container = os.environ["E2E_REST_CONTAINER"]

    def restart():
        subprocess.run(["docker", "restart", container], check=True, capture_output=True)
        end = time.time() + 300
        while time.time() < end:
            try:
                if gs.request("GET", "about/version.json", ok=(200, 401, 403, 404, 502, 503)).status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(5)
        raise AssertionError("GeoServer didn't come back")

    saved = gs.request("GET", "resource/gwc-gs.xml").text
    sql("CREATE TABLE IF NOT EXISTS e2e_tiles.later (id serial PRIMARY KEY, geom geometry(Point, 27700))")
    try:
        (project / "settings.yaml").write_text("""\
apiVersion: configops/v1alpha1
kind: Settings
metadata: { name: global }
spec: { global: { tileCaching: { cacheLayersByDefault: false } } }
""")
        assert configops(capsys, "apply", "-C", str(project))[0] == 0
        restart()
        gs.request("POST", f"workspaces/{WS}/datastores/e2e_tiles/featuretypes", {"featureType": {"name": "later", "nativeName": "later"}})
        assert gs.tile_layer(f"{WS}:later") is None  # GeoServer made no cache for it
    finally:
        gs.request("PUT", "resource/gwc-gs.xml", saved, content_type="application/xml")
        restart()
