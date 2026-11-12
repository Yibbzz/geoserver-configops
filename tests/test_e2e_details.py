"""Layer details against the real GeoServer Cloud REST service: a CQL filter, per-layer WFS
settings, metadata links, HTTP cache, a time dimension, opaque, buffer, attribution with a link
and logo; then drift, and import back to a clean plan.

    E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_DB_PORT=55433 pytest -m e2e
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

WS = "configops_details"
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
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    gs.delete_workspace(WS)
    sql("CREATE EXTENSION IF NOT EXISTS postgis; DROP SCHEMA IF EXISTS e2e_details CASCADE; CREATE SCHEMA e2e_details;"
        "CREATE TABLE e2e_details.readings (id serial PRIMARY KEY, kind text, observed timestamp, ended timestamp, depth int, geom geometry(Point, 27700));"
        "INSERT INTO e2e_details.readings (kind, observed, geom) VALUES "
        "('river', '2026-01-01', ST_SetSRID(ST_MakePoint(1, 1), 27700)), ('sea', '2026-02-01', ST_SetSRID(ST_MakePoint(2, 2), 27700))")
    yield gs
    gs.delete_workspace(WS)
    sql("DROP SCHEMA IF EXISTS e2e_details CASCADE")


@pytest.fixture
def env(monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": str(DB["port"]), "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": DB["user"],
                       "E2EDB_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)


def project_yaml() -> str:
    return f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-details }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
"""


@pytest.fixture
def project(tmp_path, env):
    (tmp_path / "project.yaml").write_text(project_yaml() + """---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: river-readings }
spec:
  table: { schema: e2e_details, name: readings }
  keywords: [e2e, rivers, { keyword: afonydd, language: cy, vocabulary: GEMET }]
  filter: "kind = 'river'"
  internationalTitle: { cy: Darlleniadau afonydd, fr: Relevés des rivières }
  internationalAbstract: { cy: Dyfnder afonydd }
  attributes:
    - id
    - { name: kind, options: [river, sea] }
    - observed
    - ended
    - { name: depth, description: Depth in metres, nillable: false, minOccurs: 1, maxOccurs: 2, range: [0, 100] }
    - { name: kind_upper, source: "strToUpperCase(kind)", binding: java.lang.String }
  wfs: { maxFeatures: 100, decimals: 2, skipNumberMatched: true, otherCRS: [EPSG:4326], padWithZeros: true, forcedDecimal: true, encodeMeasures: true, simpleConversion: true }
  circularArcs: true
  linearizationTolerance: 0.5
  disabledServices: [wcs, wps]
  kml: { regionateAttribute: depth, regionateStrategy: random, regionateFeatureLimit: 20 }
  authorityURLs: [ { name: e2e, href: "https://example.org/authority" } ]
  identifiers: [ { authority: e2e, identifier: river-readings } ]
  metadataLinks: [ { url: "https://example.org/readings.xml", type: text/xml, standard: ISO19115:2003 } ]
  httpCache: { maxAge: 300 }
  dimensions:
    time: { attribute: observed, endAttribute: ended, presentation: interval, resolution: "86400000", units: ISO8601, nearestMatch: true,
            acceptableInterval: PT12H, nearestFailBehavior: EXCEPTION, rawNearestMatch: true, default: { strategy: MAXIMUM } }
    elevation: { attribute: depth, presentation: list, units: EPSG:5030, unitSymbol: m, start: "0", end: "100", default: { strategy: NEAREST, value: "5" } }
  opaque: true
  buffer: 6
  attribution: { title: Readings, link: "https://example.org", logo: { url: "https://example.org/logo.png", width: 16, height: 16, type: image/png } }
  wms: { path: /water/rivers, interpolation: bicubic }
  dataLinks: [ { url: "https://example.org/readings.csv", type: text/csv } ]
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: kinds }
spec:
  table: { schema: e2e_details, name: readings }
  name: kinds
  dimensions: { kind: { attribute: kind } }
---
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata: { name: readings-eo }
spec:
  mode: eo
  rootLayer: river-readings
  layers: [kinds]
  abstract: Readings by kind
  keywords: [e2e, readings, { keyword: darlleniadau, language: cy }]
  queryable: false
  internationalTitle: { cy: Darlleniadau }
  internationalAbstract: { cy: Darlleniadau fesul math }
  attribution: { title: Readings group, link: "https://example.org/group" }
  authorityURLs: [ { name: e2e, href: "https://example.org/authority" } ]
  identifiers: [ { authority: e2e, identifier: readings-group } ]
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
    ft = gs.featuretype(WS, "e2e_details", "river_readings")["featureType"]
    assert ft["cqlFilter"] == "kind = 'river'" and ft["maxFeatures"] == 100
    assert ft["store"]["name"] == f"{WS}:e2e_details" and ft["namespace"]["name"] == WS and ft["nativeName"] == "readings"
    assert ft["keywords"]["string"] == ["e2e", "rivers", "afonydd\\@language=cy\\;\\@vocabulary=GEMET\\;"]
    assert gs.layer(WS, "river_readings")["layer"]["attribution"]["logoURL"] == "https://example.org/logo.png"
    layer = gs.layer(WS, "river_readings")["layer"]
    assert layer["resource"]["name"] == f"{WS}:river_readings"
    assert layer["path"] == "/water/rivers" and layer["defaultWMSInterpolationMethod"] == "Bicubic"
    assert ft["dataLinks"]["org.geoserver.catalog.impl.DataLinkInfoImpl"]["content"] == "https://example.org/readings.csv"
    kinds = gs.featuretype(WS, "e2e_details", "kinds")["featureType"]
    assert kinds["metadata"]["entry"]["@key"] == "dim_kind"
    group = gs.layergroup("readings_eo", WS)["layerGroup"]
    assert group["mode"] == "EO" and group["rootLayer"]["name"] == f"{WS}:river_readings"
    assert group["abstractTxt"] == "Readings by kind" and "readings" in str(group["keywords"])
    dims = {e["@key"]: e.get("dimensionInfo") for e in ft["metadata"]["entry"] if "dimensionInfo" in e}
    assert dims["time"]["endAttribute"] == "ended" and dims["time"]["nearestMatchEnabled"] is True
    assert str(dims["time"]["resolution"]) == "86400000" and dims["elevation"]["unitSymbol"] == "m"
    assert ft["metadataLinks"]["metadataLink"]["content"] == "https://example.org/readings.xml"
    attrs = {x["name"]: x for x in ft["attributes"]["attribute"]}
    assert (attrs["depth"]["nillable"], attrs["depth"]["minOccurs"], attrs["depth"]["maxOccurs"]) == (False, 1, 2)
    assert "Depth in metres" in str(attrs["depth"]["description"]) and attrs["kind_upper"]["source"] == "strToUpperCase(kind)"
    assert attrs["kind_upper"]["binding"] == "java.lang.String" and attrs["kind_upper"]["maxOccurs"] == 1
    assert attrs["depth"]["range"] == {"min": 0, "max": 100} and attrs["kind"]["options"] == {"string": ["river", "sea"]}
    assert ft["simpleConversionEnabled"] is True
    assert (ft["padWithZeros"], ft["forcedDecimal"], ft["encodeMeasures"], ft["circularArcPresent"], ft["linearizationTolerance"]) == (
        True, True, True, True, 0.5)
    assert ft["serviceConfiguration"] is True and sorted(ft["disabledServices"]["string"]) == ["WCS", "WPS"]
    assert dims["time"]["defaultValue"]["strategy"] == "MAXIMUM" and dims["time"]["acceptableInterval"] == "PT12H"
    assert str(dims["elevation"]["defaultValue"]["referenceValue"]) == "5" and str(dims["elevation"]["endValue"]) == "100"
    assert layer["identifiers"]["Identifier"]["identifier"] == "river-readings"
    assert ft["internationalTitle"]["cy"] == "Darlleniadau afonydd" and group["internationalTitle"] == {"cy": "Darlleniadau"}
    assert group["queryDisabled"] is True and group["attribution"]["href"] == "https://example.org/group"
    assert group["authorityURLs"]["AuthorityURL"]["href"] == "https://example.org/authority"


def test_drift_is_shown_and_put_back(gs, project, capsys):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    gs.request("PUT", f"workspaces/{WS}/datastores/e2e_details/featuretypes/river_readings", {"featureType": {"cqlFilter": "kind = 'sea'"}})
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2 and "filter: \"kind = 'sea'\" -> \"kind = 'river'\"" in out, out
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    assert configops(capsys, "plan", "-C", str(project))[0] == 0


def test_import_round_trip(gs, project, tmp_path_factory, capsys, env):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text(project_yaml())
    assert configops(capsys, "import", WS, "-C", str(imported))[0] == 0
    text = (imported / f"{WS}.yaml").read_text()
    assert "filter: kind = 'river'" in text and "maxAge: 300" in text and "attribute: observed" in text
    assert "path: /water/rivers" in text and "readings.csv" in text and "rootLayer: river-readings" in text
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out


def test_import_brings_a_change_made_in_geoserver_back_into_the_files(gs, project, capsys):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    gs.request("PUT", f"workspaces/{WS}/datastores/e2e_details/featuretypes/river_readings",
               {"featureType": {"title": "Edited in the web UI"}})
    before = (project / "project.yaml").read_text()
    code, out = configops(capsys, "import", WS, "-C", str(project))
    assert code == 0 and "1 changed: Layer river-readings (title)" in out, out
    after = (project / "project.yaml").read_text()
    assert "title: Edited in the web UI" in after and after.replace("  title: Edited in the web UI\n", "") == before
    code, out = configops(capsys, "plan", "-C", str(project))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
