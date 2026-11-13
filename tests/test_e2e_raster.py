"""A GeoTIFF in GeoServer's /data folder, against the real GeoServer: published through a Store with its
WCS settings, bands and parameters, imported back, then replaced by a larger file and refreshed.

The test writes the file with GDAL's command-line tools to E2E_DATA_DIR, which tests/e2e/up.sh mounts at
/data in every GeoServer service:

    E2E_GEOSERVER_URL=http://localhost:59090/geoserver-cloud/rest E2E_DB_PORT=55433 E2E_DATA_DIR=tests/e2e/data pytest -m e2e
"""

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
if shutil.which("gdal_translate") is None:
    pytest.skip("GDAL's command-line tools not installed", allow_module_level=True)

WS = "configops_raster"
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
PATH = "e2e-raster/heights.tif"


def write_heights(columns: int) -> None:
    """A grid of heights, 10 rows by `columns`, 50 m cells from (510000, 140000), as a GeoTIFF at /data/<PATH>."""
    target = Path(DATA) / PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    grid = target.with_suffix(".asc")
    rows = "".join(" ".join(str(r) for _ in range(columns)) + "\n" for r in range(10))
    grid.write_text(f"ncols {columns}\nnrows 10\nxllcorner 510000\nyllcorner 140000\ncellsize 50\n{rows}")
    subprocess.run(["gdal_translate", "-q", "-a_srs", "EPSG:27700", "-of", "GTiff", str(grid), str(target)], check=True)
    grid.unlink()
    for p in target.parent.iterdir():
        p.chmod(0o666)  # GeoServer's user writes overviews and the like beside it
    target.parent.chmod(0o777)


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    gs.delete_workspace(WS)
    shutil.rmtree(Path(DATA) / "e2e-raster", ignore_errors=True)
    write_heights(20)
    yield gs
    gs.delete_workspace(WS)
    shutil.rmtree(Path(DATA) / "e2e-raster", ignore_errors=True)


@pytest.fixture
def project(tmp_path, monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    (tmp_path / "project.yaml").write_text(f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-raster }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  workspace: {WS}
  crs: EPSG:27700
---
apiVersion: configops/v1alpha1
kind: Store
metadata: {{ name: heights }}
spec: {{ type: geotiff, path: {PATH} }}
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: heights }}
spec:
  store: heights
  title: Heights
  wcs: {{ requestCRS: [EPSG:27700, EPSG:4326], responseCRS: [EPSG:27700], formats: [GeoTIFF, PNG], defaultInterpolation: bilinear }}
  bands: [ {{ name: HEIGHT, description: Height above sea level, unit: m, nullValues: [-9999] }} ]
  parameters: {{ InputTransparentColor: "#000000", SUGGESTED_TILE_SIZE: "256,256" }}
""")
    return tmp_path


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def test_publish_then_nothing_to_do(gs, project, capsys):
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2 and f"+ store {WS}:heights   (geotiff: /data/{PATH})" in out, out
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    code, out = configops(capsys, "plan", "-C", str(project))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
    coverage = gs.get(f"workspaces/{WS}/coveragestores/heights/coverages/heights")["coverage"]
    assert coverage["srs"] == "EPSG:27700"
    assert coverage["nativeBoundingBox"]["minx"] == 510000 and coverage["nativeBoundingBox"]["maxx"] == 511000
    assert coverage["supportedFormats"]["string"] == ["GeoTIFF", "PNG"] and coverage["defaultInterpolationMethod"] == "bilinear"
    band = coverage["dimensions"]["coverageDimension"]
    assert band["name"] == "HEIGHT" and band["unit"] == "m"


def test_import_round_trip(gs, project, tmp_path_factory, capsys):
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text((project / "project.yaml").read_text().split("\n---\n")[0] + "\n")
    assert configops(capsys, "import", WS, "-C", str(imported))[0] == 0
    text = (imported / f"{WS}.yaml").read_text()
    assert "name: HEIGHT" in text and "InputTransparentColor" in text and "defaultInterpolation: bilinear" in text
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out


def test_a_new_file_is_refreshed(gs, project, capsys):
    """A larger file in its place: `configops refresh <path>` resets the store and works the bounds out again."""
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    write_heights(40)
    code, out = configops(capsys, "refresh", "-C", str(project), f"/data/{PATH}")
    assert code == 0 and "bounds of 1 layer(s) worked out again" in out, out
    coverage = gs.get(f"workspaces/{WS}/coveragestores/heights/coverages/heights")["coverage"]
    assert coverage["nativeBoundingBox"]["maxx"] == 512000
    assert configops(capsys, "plan", "-C", str(project))[0] == 0
