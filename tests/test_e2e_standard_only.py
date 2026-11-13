"""What only standard GeoServer can set: group styles, CORS, the Content-Security-Policy header and
GeoWebCache's disk quota; Oracle stores have their own test (test_e2e_oracle.py).

On standard GeoServer apply sets them and the services show it (CORS and the disk quota store after
a restart: E2E_REST_CONTAINER). On GeoServer Cloud plan shows each as `!`, says why, and leaves it out.

    E2E_GEOSERVER_URL=http://localhost:59091/geoserver/rest E2E_SERVICES_URL=http://localhost:59091/geoserver \\
      E2E_GWC_URL=http://localhost:59091/geoserver/gwc/rest E2E_REST_CONTAINER=gscloud-e2e-geoserver-1 pytest -m e2e
"""

import os
import subprocess
import time

import psycopg2
import pytest
import requests

from configops.cli import main
from configops.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL:
    pytest.skip("E2E_GEOSERVER_URL not set", allow_module_level=True)

WS = "configops_standard"
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
SERVICES = os.environ.get("E2E_SERVICES_URL")
CONTAINER = os.environ.get("E2E_REST_CONTAINER")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=int(os.environ.get("E2E_DB_PORT", 55433)),
          dbname="postgres", user="postgres", password=os.environ.get("E2E_DB_PASSWORD", "postgres"))
ORIGIN = "https://maps.example.org"


def sql(statement):
    conn = psycopg2.connect(**DB)
    with conn, conn.cursor() as cur:
        cur.execute(statement)
    conn.close()


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD, gwc_url=os.environ.get("E2E_GWC_URL"))
    gs.delete_workspace(WS)
    sql("CREATE EXTENSION IF NOT EXISTS postgis; DROP SCHEMA IF EXISTS e2e_standard CASCADE; CREATE SCHEMA e2e_standard;"
        "CREATE TABLE e2e_standard.sites (id serial PRIMARY KEY, geom geometry(Point, 4326));"
        "INSERT INTO e2e_standard.sites (geom) VALUES (ST_SetSRID(ST_MakePoint(-1, 51), 4326))")
    saved = None
    if not gs.cloud:
        settings = gs.get("settings")
        csp = gs.request("GET", "resource/security/csp.xml", ok=(200, 404))
        saved = (settings, csp.text if csp.status_code == 200 else None)
    yield gs
    gs.delete_workspace(WS)
    sql("DROP SCHEMA IF EXISTS e2e_standard CASCADE")
    if saved:  # put back what the test changed: CORS goes with the next restart
        settings, csp = saved
        settings["global"].pop("updateSequence", None)
        gs.request("PUT", "settings", settings)
        if csp:
            gs.request("PUT", "resource/security/csp.xml", csp, content_type="application/xml")
        gs.gwc_request("PUT", "diskquota.xml", "<gwcQuotaConfiguration><enabled>false</enabled><layerQuotas/></gwcQuotaConfiguration>")
        gs.request("DELETE", "resource/gwc/geowebcache-diskquota.xml", ok=(200, 404))


@pytest.fixture
def env(monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": str(DB["port"]), "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": DB["user"],
                       "E2EDB_PASSWORD": DB["password"], "ORACLE_USERNAME": "gis", "ORACLE_PASSWORD": "gis"}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    if os.environ.get("E2E_GWC_URL"):
        monkeypatch.setenv("CONFIGOPS_GWC_URL", os.environ["E2E_GWC_URL"])


PROJECT = f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-standard }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: sites }}
spec:
  table: {{ schema: e2e_standard, name: sites }}
  cache: {{ gridsets: [EPSG:4326], formats: [image/png] }}
---
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata: {{ name: sites-group }}
spec:
  layers: [sites]
  styles:
    - name: as-squares
      title: Sites as squares
      abstract: The same sites, drawn another way
      internationalTitle: {{ cy: Safleoedd }}
      internationalAbstract: {{ cy: Yr un safleoedd }}
      layers: [ {{ layer: sites, style: point }} ]
---
apiVersion: configops/v1alpha1
kind: Settings
metadata: {{ name: global }}
spec:
  global:
    cors:
      enabled: true
      allowedOriginPatterns: [{ORIGIN}]
      allowedMethods: [GET, POST]
      allowedHeaders: [Content-Type]
      maxAge: 600
      supportsCredentials: true
    contentSecurityPolicy:
      enabled: true
      reportOnly: false
      allowOverride: true
      injectProxyBase: true
      remoteResources: https://cdn.example.org
      formAction: "'self'"
      frameAncestors: https://portal.example.org
      policies:
        - name: e2e-policy
          description: One rule for everything
          enabled: true
          rules:
            - name: everything
              description: Every request
              enabled: true
              filter: PATH(^.*$)
              directives: "default-src 'self'; frame-ancestors ${{geoserver.csp.frameAncestors}};"
    diskQuota:
      enabled: true
      quotaStore: HSQL
      globalQuota: 2 GiB
      globalExpirationPolicyName: LRU
      cacheCleanUpFrequency: 5
      cacheCleanUpUnits: MINUTES
      maxConcurrentCleanUps: 3
      layerQuotas: [ {{ layer: "{WS}:sites", quota: 100 MiB, expirationPolicyName: LFU }} ]
"""

ORACLE = """---
apiVersion: configops/v1alpha1
kind: Store
metadata: { name: warehouse }
spec:
  type: oracle
  connection: { host: oracle, port: 1521, database: FREEPDB1, schema: GIS }
  credentials: { secret: oracle }
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: parcels }
spec: { store: warehouse, resource: PARCELS }
"""


@pytest.fixture
def project(tmp_path, env):
    (tmp_path / "project.yaml").write_text(PROJECT)
    return tmp_path


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def settled(gs, code: int, out: str) -> None:
    """Nothing left to do: on GeoServer 2.x, only CORS, which came in 3.0, left out with its reason."""
    if gs.series and gs.series < (3, 0):
        assert code == 3 and f"! settings global   (GeoServer {gs.version} can't set global.cors: it came in GeoServer 3.0)" in out, out
    else:
        assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out


def restart(gs):
    subprocess.run(["docker", "restart", CONTAINER], check=True, capture_output=True)
    end = time.time() + 300
    while time.time() < end:
        try:
            if gs.request("GET", "about/version.json", ok=(200, 401, 403, 404, 502, 503)).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(5)
    raise AssertionError("GeoServer didn't come back")


@pytest.mark.skipif(not SERVICES, reason="E2E_SERVICES_URL not set")
def test_standard_geoserver_takes_them_and_the_services_show_it(gs, project, capsys):
    if gs.cloud:
        pytest.skip("standard GeoServer only: test_geoserver_cloud_leaves_them_out")
    cors = not gs.series or gs.series >= (3, 0)  # GeoServer 2.28 has no CORS settings
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    assert ("restart it to use them" in out) is cors  # CORS
    settled(gs, *configops(capsys, "plan", "-C", str(project)))

    # A group style draws, and WMS lists it
    r = requests.get(f"{SERVICES}/{WS}/wms", params={
        "service": "WMS", "version": "1.1.1", "request": "GetMap", "layers": f"{WS}:sites_group", "styles": "as-squares",
        "srs": "EPSG:4326", "bbox": "-2,50,0,52", "width": 64, "height": 64, "format": "image/png"}, timeout=60)
    assert r.headers["content-type"] == "image/png", r.text[:300]
    caps = requests.get(f"{SERVICES}/{WS}/wms", params={"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"}, timeout=60).text
    assert "<Name>as-squares</Name>" in caps and "<Title>Sites as squares</Title>" in caps

    # The Content-Security-Policy header is ours within seconds (GeoServer watches the file)
    wanted = "default-src 'self'; frame-ancestors 'self' https://portal.example.org;"
    end = time.time() + 30
    while True:
        header = requests.get(f"{SERVICES}/ows", params={"service": "WMS", "request": "GetCapabilities"}, timeout=60).headers
        if header.get("Content-Security-Policy", "").startswith(wanted) or time.time() > end:
            break
        time.sleep(1)
    assert header.get("Content-Security-Policy", "").startswith(wanted), header

    # The disk quota is on now; its store and CORS take a restart
    quota = gs.gwc_request("GET", "diskquota.xml", accept="application/xml").text
    assert "<enabled>true</enabled>" in quota and "<bytes>2147483648</bytes>" in quota and "<bytes>104857600</bytes>" in quota
    if not CONTAINER:
        pytest.skip("E2E_REST_CONTAINER not set: can't restart GeoServer for CORS and the disk quota store")
    restart(gs)
    quota = gs.gwc_request("GET", "diskquota.xml", accept="application/xml").text
    assert "<quotaStore>HSQL</quotaStore>" in quota and "<bytes>2147483648</bytes>" in quota and f"<layer>{WS}:sites</layer>" in quota
    if cors:
        r = requests.options(f"{SERVICES}/ows", params={"service": "WMS", "request": "GetCapabilities"}, timeout=60,
                             headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"})
        assert r.headers.get("Access-Control-Allow-Origin") == ORIGIN, r.headers
        assert r.headers.get("Access-Control-Max-Age") == "600" and r.headers.get("Access-Control-Allow-Credentials") == "true"
        r = requests.get(f"{SERVICES}/ows", params={"service": "WMS", "request": "GetCapabilities"}, timeout=60,
                         headers={"Origin": "https://elsewhere.example.org"})
        assert "Access-Control-Allow-Origin" not in r.headers
    settled(gs, *configops(capsys, "plan", "-C", str(project)))

    # Import writes them back
    imported = project / "imported"
    imported.mkdir()
    (imported / "project.yaml").write_text(PROJECT.split("---")[0])
    assert configops(capsys, "import", WS, "--settings", "-C", str(imported))[0] == 0
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
    written = "".join(p.read_text() for p in imported.glob("*.yaml"))
    for text in ("as-squares", "contentSecurityPolicy:", "diskQuota:") + (("cors:",) if cors else ()):
        assert text in written, text


def test_geoserver_cloud_leaves_them_out(gs, project, capsys):
    if not gs.cloud:
        pytest.skip("GeoServer Cloud only")
    (project / "project.yaml").write_text(PROJECT + ORACLE)
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2, out
    for line in ("! group sites-group   (GeoServer Cloud can't set styles",
                 "! settings global   (GeoServer Cloud can't set global.cors",
                 "! settings global   (GeoServer Cloud can't set global.contentSecurityPolicy",
                 "! settings global   (GeoServer Cloud can't set global.diskQuota",
                 "! store warehouse   (GeoServer Cloud can't have oracle stores",
                 "! layer parcels   (its store warehouse can't be made on GeoServer Cloud"):
        assert line in out, out
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 3, out  # only the ! lines are left
    assert gs.exists(f"workspaces/{WS}/layergroups/sites_group") and not gs.exists(f"workspaces/{WS}/datastores/warehouse")
