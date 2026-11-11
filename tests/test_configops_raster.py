"""Raster Datasets (configops/raster.py): unpacking, mosaicking into a COG, swapping in, backups."""

import shutil
import zipfile

import pytest

from configops import raster
from configops.ingest import LoadError
from configops.loader import load
from configops.validate import validate

pytestmark = pytest.mark.skipif(shutil.which("gdalbuildvrt") is None, reason="GDAL's command-line tools not installed")

PROJECT = """\
apiVersion: configops/v1alpha1
kind: Project
metadata: { name: t }
spec:
  geoserver: { url: http://gs:8080/geoserver-cloud, credentials: { secret: gs } }
  database: { jndi: java:comp/env/jdbc/x, connection: { secret: pg } }
  workspace: uk
---
apiVersion: configops/v1alpha1
kind: Source
metadata: { name: os }
spec: { type: os-downloads }
"""


def grid(x: int, value: int) -> str:
    """A 2 x 2 ASCII grid at x, like one OS tile, without a CRS."""
    return f"ncols 2\nnrows 2\nxllcorner {x}\nyllcorner 0\ncellsize 50\n{value} {value}\n{value} {value}\n"


@pytest.fixture
def download(tmp_path):
    """Two tiles in zips inside a zip, as OS packs Terrain 50."""
    inner = []
    for i, x in enumerate((0, 100)):
        path = tmp_path / f"tile{i}.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(f"tile{i}/t{i}.asc", grid(x, 10 * (i + 1)))
            z.writestr(f"tile{i}/readme.txt", "not a raster")
        inner.append(path)
    outer = tmp_path / "terrain.zip"
    with zipfile.ZipFile(outer, "w") as z:
        for path in inner:
            z.write(path, f"data/{path.name}")
    return outer


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv(raster.DATA_ENV, str(root))
    return root


def test_unpacks_zips_inside_zips(download, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    assert [p.name for p in raster.unpack(download, work)] == ["t0.asc", "t1.asc"]


def test_mosaics_into_a_cog_with_the_fallback_crs(download, tmp_path, data):
    work = tmp_path / "work"
    work.mkdir()
    pixels = raster.load(download, work, "rasters/terrain.tif", None, "EPSG:27700", keep=1)
    live = data / "rasters" / "terrain.tif"
    built = raster.info(live)
    assert built["size"] == [4, 2] and pixels == 8  # two tiles side by side
    assert "27700" in built["coordinateSystem"]["wkt"]
    assert (built.get("metadata", {}).get("IMAGE_STRUCTURE") or {}).get("LAYOUT") == "COG"
    assert not list((data / "rasters").glob(".*.new"))  # nothing half-written left behind


def test_nodata_where_no_tile_covers(tmp_path, data):
    """Two tiles with a gap between: the gap is nodata, not 0."""
    for i, x in enumerate((0, 200)):
        (tmp_path / f"t{i}.asc").write_text(grid(x, 10 * (i + 1)))
    path = tmp_path / "tiles.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.write(tmp_path / "t0.asc", "t0.asc")
        z.write(tmp_path / "t1.asc", "t1.asc")
    work = tmp_path / "work"
    work.mkdir()
    raster.load(path, work, "gappy.tif", "EPSG:27700", None, keep=1, nodata=-9999)
    built = raster.info(data / "gappy.tif")
    assert built["size"] == [6, 2] and built["bands"][0]["noDataValue"] == -9999


def test_each_load_backs_up_the_last_and_keeps_the_newest(download, tmp_path, data, monkeypatch):
    stamps = iter(["20260101000000", "20260102000000", "20260103000000"])
    monkeypatch.setattr(raster, "stamp", lambda: next(stamps))
    for i in range(3):
        work = tmp_path / f"work{i}"
        work.mkdir()
        raster.load(download, work, "terrain.tif", "EPSG:27700", None, keep=1)
    backups = sorted(p.name for p in (data / raster.BACKUPS).iterdir())
    assert backups == ["terrain.20260102000000.tif"]  # the second load's file; the first was dropped
    assert (data / "terrain.tif").exists()


def test_needs_the_data_volume(download, tmp_path, monkeypatch):
    monkeypatch.setenv(raster.DATA_ENV, str(tmp_path / "nowhere"))
    with pytest.raises(LoadError, match="no data volume"):
        raster.load(download, tmp_path, "terrain.tif", None, None, keep=1)


def test_a_zip_without_rasters(tmp_path):
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("readme.txt", "nothing here")
    with pytest.raises(LoadError, match="no rasters in empty.zip"):
        raster.unpack(path, tmp_path)


def test_validate_raster_datasets_and_their_stores(tmp_path):
    (tmp_path / "project.yaml").write_text(PROJECT + """---
apiVersion: configops/v1alpha1
kind: Dataset
metadata: { name: both }
spec: { source: os, product: X, tables: { a: {} }, raster: { path: a.tif } }
---
apiVersion: configops/v1alpha1
kind: Dataset
metadata: { name: heights }
spec: { source: os, product: Terrain50, raster: { path: a.tif } }
---
apiVersion: configops/v1alpha1
kind: Dataset
metadata: { name: roads }
spec: { source: os, product: OpenRoads, tables: { road_link: {} } }
---
apiVersion: configops/v1alpha1
kind: Store
metadata: { name: s1 }
spec: { type: geopackage, dataset: heights }
---
apiVersion: configops/v1alpha1
kind: Store
metadata: { name: s2 }
spec: { type: geotiff, dataset: roads }
---
apiVersion: configops/v1alpha1
kind: Store
metadata: { name: s3 }
spec: { type: geotiff, dataset: heights, path: other.tif }
""")
    messages = [p.message for p in validate(load(tmp_path)) if p.severity == "error"]
    assert "spec.raster: a Dataset loads tables or a raster, not both" in messages
    assert "spec.raster.path: a.tif is also written by Dataset both" in messages
    assert "spec.type: a store on a raster Dataset is a geotiff" in messages
    assert "spec.dataset: roads loads tables, not a raster: publish its tables with Layers" in messages
    assert "spec.path: a store on a raster Dataset reads the Dataset's file" in messages
