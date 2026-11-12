"""What the files set, seen in GeoServer's own services rather than its REST API: WMS capabilities
(a layer's path, time and custom dimensions, an EO group), a workspace's own WFS and WCS
settings, tiles kept apart by a parameter filter, and for a raster: interpolation in maps, the
formats WCS allows, and band details in DescribeCoverage.

Needs the whole chart, reached through its gateway (the kind test, or the dev cluster):

    E2E_GEOSERVER_URL=http://localhost:18080/geoserver-cloud/rest E2E_SERVICES_URL=http://localhost:18080/geoserver-cloud \\
      E2E_DB_PORT=15432 E2E_DB_PASSWORD=... E2E_DATA_POD=gscloud/<a GeoServer pod> pytest -m e2e
"""

import os
import re
import subprocess
import time

import psycopg2
import pytest
import requests

from configops.cli import main
from configops.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
SERVICES = os.environ.get("E2E_SERVICES_URL")
if not URL or not SERVICES:
    pytest.skip("E2E_GEOSERVER_URL or E2E_SERVICES_URL not set", allow_module_level=True)

WS = "configops_services"
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
    sql("CREATE EXTENSION IF NOT EXISTS postgis; DROP SCHEMA IF EXISTS e2e_services CASCADE; CREATE SCHEMA e2e_services;"
        "CREATE TABLE e2e_services.readings (id serial PRIMARY KEY, kind text, depth int, observed timestamp, geom geometry(Point, 4326));"
        "INSERT INTO e2e_services.readings (kind, depth, observed, geom) VALUES "
        "('river', 5, '2026-01-01', ST_SetSRID(ST_MakePoint(-1, 51), 4326)), ('sea', 10, '2026-02-01', ST_SetSRID(ST_MakePoint(1, 52), 4326))")
    yield gs
    gs.delete_workspace(WS)
    sql("DROP SCHEMA IF EXISTS e2e_services CASCADE")


@pytest.fixture
def env(monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": str(DB["port"]), "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": DB["user"],
                       "E2EDB_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)


HEAD = f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-services }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
"""

VECTOR = """---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: readings }
spec:
  table: { schema: e2e_services, name: readings }
  dimensions:
    time: { attribute: observed }
    depth: { attribute: depth, units: m }
  cache:
    gridsets: [EPSG:4326]
    parameterFilters:
      TIME: { type: regex, regex: "2026-0[12]-01" }
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: tree }
spec: { table: { schema: e2e_services, name: readings }, wms: { path: /water/gauges } }  # in no group: a group lists its members under it
---
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata: { name: readings-eo }
spec: { mode: eo, rootLayer: readings, layers: [readings] }
---
apiVersion: configops/v1alpha1
kind: Service
metadata: { name: own-wfs }
spec: { service: wfs, workspace: configops_services, title: Readings features }
---
apiVersion: configops/v1alpha1
kind: Service
metadata: { name: own-wcs }
spec: { service: wcs, workspace: configops_services, title: Readings coverages }
"""


@pytest.fixture
def project(tmp_path, env):
    (tmp_path / "project.yaml").write_text(HEAD + VECTOR)
    return tmp_path


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def eventually(fetch, check, seconds=90):
    """Other services hear of catalog changes over the message bus: wait for them."""
    end = time.time() + seconds
    while True:
        value = fetch()
        if check(value) or time.time() > end:
            return value
        time.sleep(3)


def capabilities(service: str, version: str) -> str:
    return requests.get(f"{SERVICES}/{WS}/{service}", timeout=60,
                        params={"service": service.upper(), "request": "GetCapabilities", "version": version}).text


def test_wms_capabilities_show_path_dimensions_and_eo_group(gs, project, capsys):
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    caps = eventually(lambda: capabilities("wms", "1.3.0"), lambda c: "readings_eo" in c)
    flat = re.sub(r"\s+", " ", caps)
    assert re.search(r"<Name>water</Name>.*?<Name>gauges</Name>.*?<Name>tree</Name>", flat), re.findall(r"<Name>[^<]*</Name>", flat)
    assert '<Dimension name="time"' in flat and '<Dimension name="depth"' in flat and 'units="m"' in flat
    assert "<Name>readings_eo</Name>" in flat


def test_a_workspaces_own_wfs_and_wcs_settings(gs, project, capsys):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    wfs = eventually(lambda: capabilities("wfs", "2.0.0"), lambda c: "Readings features" in c)
    assert "Readings features" in wfs
    assert "Readings coverages" in eventually(lambda: capabilities("wcs", "2.0.1"), lambda c: "Readings coverages" in c)
    assert "Readings features" not in requests.get(f"{SERVICES}/wfs", timeout=60,
                                                   params={"service": "WFS", "request": "GetCapabilities"}).text


def test_a_parameter_filter_keeps_tiles_apart_and_refuses_others(gs, project, capsys):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    tile = (f"{SERVICES}/gwc/service/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER={WS}:readings&STYLE="
            "&TILEMATRIXSET=EPSG:4326&TILEMATRIX=EPSG:4326:0&TILEROW=0&TILECOL=1&FORMAT=image/png")

    gs.truncate(f"{WS}:readings")  # tiles from an earlier run outlive the layer

    def get(time_value):
        return requests.get(tile + f"&TIME={time_value}", timeout=60)
    first = eventually(lambda: get("2026-01-01"), lambda r: r.status_code == 200)
    assert first.status_code == 200, first.text[:300]
    assert get("2026-01-01").headers.get("geowebcache-cache-result") == "HIT"
    assert get("2026-02-01").headers.get("geowebcache-cache-result") == "MISS"  # its own tiles
    assert get("2025-12-31").status_code >= 400  # not a value the filter allows


LAYER_OPTIONS = """---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: wms-only }
spec:
  table: { schema: e2e_services, name: readings }
  disabledServices: [wfs]
  authorityURLs: [ { name: e2e, href: "https://example.org/authority" } ]
  identifiers: [ { authority: e2e, identifier: e2e-wms-only } ]
---
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata: { name: not-queryable }
spec: { layers: [wms-only], queryable: false }
"""


def test_layer_services_identifiers_and_a_group_that_isnt_queryable(gs, project, capsys):
    (project / "project.yaml").write_text(HEAD + VECTOR + LAYER_OPTIONS)
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    wms = re.sub(r"\s+", " ", eventually(lambda: capabilities("wms", "1.3.0"), lambda c: "not_queryable" in c))
    assert "<Name>wms_only</Name>" in wms and "e2e-wms-only" in wms and "https://example.org/authority" in wms
    group = re.search(r'<Layer( [^>]*)?>\s*<Name>not_queryable</Name>', wms)
    assert group and 'queryable="0"' in (group.group(1) or ""), group
    wfs = eventually(lambda: capabilities("wfs", "2.0.0"), lambda c: "readings" in c)
    assert f"{WS}:readings" in wfs or "<Name>readings</Name>" in wfs  # the others are there
    assert "wms_only" not in wfs


# --- a raster on the data volume

RASTER = """---
apiVersion: configops/v1alpha1
kind: Store
metadata: { name: dem }
spec: { type: geotiff, path: e2e-services/dem.tif }
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: dem }
spec:
  store: dem
  wms: { interpolation: INTERPOLATION }
  wcs: { formats: [GeoTIFF], requestCRS: [EPSG:27700], interpolations: [nearest neighbor] }
  bands: [ { name: HEIGHT, description: Height above sea level, unit: m, nullValues: [-9999], range: [0, 1000] } ]
"""


@pytest.fixture
def raster(data_volume, tmp_path, env):
    """A 4 x 4 grid of very different heights, so resampling shows."""
    asc = tmp_path / "dem.asc"
    asc.write_text("ncols 4\nnrows 4\nxllcorner 510000\nyllcorner 140000\ncellsize 1000\n"
                   "0 900 0 900\n900 0 900 0\n0 900 0 900\n900 0 900 0\n")
    folder = tmp_path / "e2e-services"
    folder.mkdir()
    subprocess.run(["gdal_translate", "-q", "-a_srs", "EPSG:27700", "-ot", "Float32", str(asc), str(folder / "dem.tif")], check=True)
    data_volume.put(folder, "e2e-services")
    yield tmp_path
    data_volume.remove("e2e-services")


def draw(layer: str) -> bytes:
    r = requests.get(f"{SERVICES}/{WS}/wms", timeout=120, params={
        "service": "WMS", "version": "1.1.1", "request": "GetMap", "layers": layer, "styles": "", "srs": "EPSG:27700",
        "bbox": "510000,140000,514000,144000", "width": 256, "height": 256, "format": "image/png"})
    assert r.headers["content-type"] == "image/png", r.text[:300]
    return r.content


def test_raster_interpolation_wcs_formats_and_bands(gs, raster, env, capsys):
    def apply(interpolation):
        (raster / "project.yaml").write_text(HEAD + RASTER.replace("INTERPOLATION", interpolation))
        code, out = configops(capsys, "apply", "-C", str(raster))
        assert code == 0, out
        assert configops(capsys, "plan", "-C", str(raster))[0] == 0
        return eventually(lambda: gs.layer(WS, "dem")["layer"].get("defaultWMSInterpolationMethod"),
                          lambda v: v == interpolation.capitalize())
    apply("nearest")
    nearest = eventually(lambda: draw("dem"), lambda img: len(img) > 0)
    apply("bicubic")
    bicubic = eventually(lambda: draw("dem"), lambda img: img != nearest)
    assert bicubic != nearest  # the same grid scaled up, drawn differently

    describe = eventually(lambda: requests.get(f"{SERVICES}/{WS}/wcs", timeout=60, params={
        "service": "WCS", "version": "2.0.1", "request": "DescribeCoverage", "coverageId": f"{WS}__dem"}).text,
        lambda t: 'name="HEIGHT"' in t)
    assert 'name="HEIGHT"' in describe and 'code="m"' in describe and "-9999" in describe
    coverage = gs.request("GET", f"workspaces/{WS}/coverages/dem.json").json()["coverage"]
    assert coverage["interpolationMethods"]["string"] in ("nearest neighbor", ["nearest neighbor"])
    band = coverage["dimensions"]["coverageDimension"]
    band = band[0] if isinstance(band, list) else band
    assert (float(band["range"]["min"]), float(band["range"]["max"])) == (0.0, 1000.0)
    png = requests.get(f"{SERVICES}/{WS}/wcs", timeout=60, params={
        "service": "WCS", "version": "2.0.1", "request": "GetCoverage", "coverageId": f"{WS}__dem", "format": "image/png"})
    assert "ExceptionReport" in png.text[:2000] or png.status_code >= 400  # only GeoTIFF is allowed
    tiff = requests.get(f"{SERVICES}/{WS}/wcs", timeout=60, params={
        "service": "WCS", "version": "2.0.1", "request": "GetCoverage", "coverageId": f"{WS}__dem", "format": "image/tiff"})
    assert tiff.headers["content-type"].startswith("image/tiff"), tiff.text[:300]


MOSAIC = """---
apiVersion: configops/v1alpha1
kind: Store
metadata: { name: depths }
spec: { type: imagemosaic, path: e2e-services-mosaic }
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: depths }
spec:
  store: depths
  dimensions: { depth: { attribute: depth, units: m } }
"""


@pytest.fixture
def mosaic(data_volume, tmp_path, env):
    """Two grids in a mosaic indexed by a depth taken from their file names."""
    folder = tmp_path / "e2e-services-mosaic"
    folder.mkdir()
    for depth in (10, 20):
        asc = tmp_path / f"d{depth}.asc"
        asc.write_text(f"ncols 2\nnrows 2\nxllcorner 510000\nyllcorner 140000\ncellsize 1000\n{depth} {depth}\n{depth} {depth}\n")
        subprocess.run(["gdal_translate", "-q", "-a_srs", "EPSG:27700", str(asc), str(folder / f"depth_{depth}.tif")], check=True)
    (folder / "indexer.properties").write_text(
        "Schema=*the_geom:Polygon,location:String,depth:Integer\n"
        "PropertyCollectors=IntegerFileNameExtractorSPI[depthregex](depth)\n"
        "AdditionalDomainAttributes=depth\n")
    (folder / "depthregex.properties").write_text("regex=[0-9]+\n")
    data_volume.put(folder, "e2e-services-mosaic")
    yield tmp_path
    data_volume.remove("e2e-services-mosaic")


def test_a_raster_custom_dimension(gs, mosaic, capsys):
    (mosaic / "project.yaml").write_text(HEAD + MOSAIC)
    code, out = configops(capsys, "apply", "-C", str(mosaic))
    assert code == 0, out
    assert configops(capsys, "plan", "-C", str(mosaic))[0] == 0
    caps = eventually(lambda: capabilities("wms", "1.3.0"), lambda c: "DEPTH" in c.upper() and "depths" in c)
    flat = re.sub(r"\s+", " ", caps)
    assert re.search(r'<Dimension name="DEPTH"[^>]*>10,20<', flat, re.I), re.findall(r"<Dimension[^<]*<", flat)
