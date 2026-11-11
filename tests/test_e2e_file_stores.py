"""File stores on the shared data volume against the real GeoServer Cloud REST service:
GeoPackage, Shapefile, a directory of shapefiles, FlatGeobuf, PMTiles, GeoParquet, property files,
CSV, GeoTIFF, ArcGrid, an image mosaic and an image pyramid; then import and a clean plan.

tests/e2e/up.sh mounts tests/e2e/data at /data in the REST service, as the chart's
values-data-volume.yaml does. The test writes its files there with GDAL, so it needs
ogr2ogr and gdal_translate, and E2E_DATA_DIR set to that directory:

    E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_DB_PORT=55433 E2E_DATA_DIR=tests/e2e/data pytest -m e2e
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from configops.cli import main
from configops.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
DATA = os.environ.get("E2E_DATA_DIR")
if not URL or not DATA:
    pytest.skip("E2E_GEOSERVER_URL or E2E_DATA_DIR not set", allow_module_level=True)
if shutil.which("ogr2ogr") is None or shutil.which("gdal_translate") is None or shutil.which("gdal_retile.py") is None:
    pytest.skip("GDAL's ogr2ogr, gdal_translate and gdal_retile.py not installed", allow_module_level=True)

WS = "configops_files"
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=os.environ.get("E2E_DB_PORT", "55433"))
STORES = [("sites-gpkg", "geopackage", "e2e/sites.gpkg"), ("sites-shp", "shapefile", "e2e/sites.shp"),
          ("vector-dir", "shapefiles", "e2e/shp"), ("sites-fgb", "flatgeobuf", "e2e/sites.fgb"),
          ("sites-pmtiles", "pmtiles", "e2e/sites.pmtiles"), ("dem", "geotiff", "e2e/dem.tif"),
          ("dem-grid", "arcgrid", "e2e/dem.asc"), ("dem-mosaic", "imagemosaic", "e2e/mosaic"),
          ("sites-parquet", "geoparquet", "e2e/sites.parquet"), ("sites-props", "property", "e2e/props"),
          ("places-csv", "csv", "e2e/places.csv", "connection: { strategy: LatLon, latField: lat, lngField: lon }"),
          ("dem-pyramid", "imagepyramid", "e2e/pyramid")]
FIXTURES = Path(__file__).parent / "e2e" / "fixtures"


def run(*cmd):
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def files():
    root = Path(DATA) / "e2e"
    shutil.rmtree(root, ignore_errors=True)
    (root / "shp").mkdir(parents=True)
    (root / "mosaic").mkdir()
    (root / "props").mkdir()
    shutil.copy(FIXTURES / "sites.parquet", root / "sites.parquet")  # GDAL here may not write Parquet
    (root / "props" / "sites.properties").write_text("_=name:String,geom:Point:srid=27700\nsites.1=a|POINT(517900 151300)\n")
    (root / "places.csv").write_text("name,lat,lon\nalpha,51.5,-0.1\nbeta,52.2,0.1\n")
    src = root / "sites.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "a"}, "geometry": {"type": "Point", "coordinates": [517900, 151300]}}]}))
    for driver, name in (("GPKG", "sites.gpkg"), ("ESRI Shapefile", "sites.shp"), ("ESRI Shapefile", "shp/sites.shp"),
                         ("FlatGeobuf", "sites.fgb"), ("PMTiles", "sites.pmtiles")):
        run("ogr2ogr", "-f", driver, str(root / name), str(src), "-a_srs", "EPSG:27700")
    # a 100 x 100 elevation-like grid, from a tiny ASCII grid GDAL can read
    asc = root / "seed.asc"
    asc.write_text("ncols 2\nnrows 2\nxllcorner 510000\nyllcorner 140000\ncellsize 5000\n10 20\n30 40\n")
    run("gdal_translate", "-q", "-a_srs", "EPSG:27700", "-outsize", "100", "100", str(asc), str(root / "dem.tif"))
    run("gdal_translate", "-q", "-of", "AAIGrid", str(root / "dem.tif"), str(root / "dem.asc"))
    run("gdal_translate", "-q", "-srcwin", "0", "0", "50", "100", str(root / "dem.tif"), str(root / "mosaic/west.tif"))
    run("gdal_translate", "-q", "-srcwin", "50", "0", "50", "100", str(root / "dem.tif"), str(root / "mosaic/east.tif"))
    (root / "pyramid").mkdir()  # an image pyramid: gdal_retile's levels 1, 2, with the full-size tiles moved into 0
    run("gdal_retile.py", "-q", "-levels", "2", "-ps", "25", "25", "-targetDir", str(root / "pyramid"), str(root / "dem.tif"))
    (root / "pyramid" / "0").mkdir()
    for tile in (root / "pyramid").glob("*.tif"):
        tile.rename(root / "pyramid" / "0" / tile.name)
    for p in root.rglob("*"):
        p.chmod(0o777 if p.is_dir() else 0o666)  # GeoServer writes a mosaic's index next to its files
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def gs(files):
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    gs.delete_workspace(WS)
    yield gs
    gs.delete_workspace(WS)


@pytest.fixture
def env(monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": DB["port"], "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": "postgres",
                       "E2EDB_PASSWORD": os.environ.get("E2E_DB_PASSWORD", "postgres")}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)


def project_yaml() -> str:
    return f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-files }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
"""


@pytest.fixture
def project(tmp_path, env):
    docs = [project_yaml()]
    for name, kind, path, *extra in STORES:
        more = f", {extra[0]}" if extra else ""
        docs.append(f"apiVersion: configops/v1alpha1\nkind: Store\nmetadata: {{ name: {name} }}\nspec: {{ type: {kind}, path: {path}{more} }}\n")
        docs.append(f"apiVersion: configops/v1alpha1\nkind: Layer\nmetadata: {{ name: {name} }}\nspec: {{ store: {name} }}\n")
    (tmp_path / "project.yaml").write_text("---\n".join(docs))
    return tmp_path


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def test_every_file_store_then_nothing_to_do(gs, project, capsys):
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    code, out = configops(capsys, "plan", "-C", str(project))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
    assert gs.get(f"workspaces/{WS}/coveragestores/dem_mosaic/coverages/dem_mosaic")["coverage"]["srs"] == "EPSG:27700"
    assert gs.layer(WS, "sites_gpkg")["layer"]["defaultStyle"]["name"] == "point"


def test_import_round_trip(gs, project, tmp_path_factory, capsys, env):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text(project_yaml())
    code, out = configops(capsys, "import", WS, "-C", str(imported))
    assert code == 0 and f"{len(STORES)} store(s), {len(STORES)} layer(s)" in out, out
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
