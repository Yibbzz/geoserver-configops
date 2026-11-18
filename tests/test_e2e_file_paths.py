"""File stores where a GeoServer installed on a server keeps its files, not only on /data: a Store's absolute
path, the Project's `geoserver.files` set to another folder, and (standard GeoServer) `geoserver.files: data`,
relative to GeoServer's own data directory, as its web UI writes `file:data/...`. Each is applied, read
back by GeoServer (attributes, a coverage's grid), planned clean, and imported back to the same files.

Needs the e2e data volume (E2E_DATA_DIR or E2E_DATA_POD); the data directory case needs the standard
GeoServer container (E2E_REST_CONTAINER) to copy a file into its data directory.
"""

import json
import os
import shutil
import subprocess

import pytest

from configops.geoserver import GeoServer
from test_e2e_file_stores import PASSWORD, URL, configops, env, run  # noqa: F401 (fixtures)

pytestmark = pytest.mark.e2e

WS = "configops_paths"
CONTAINER = os.environ.get("E2E_REST_CONTAINER")


def project(tmp_path, files: str | None, stores: list[tuple[str, str, str]]) -> object:
    geoserver = f"{{ url: {URL.removesuffix('/rest')}, credentials: {{ secret: e2egs }}" + (f", files: {files}" if files else "") + " }"
    docs = [f"""apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-paths }}
spec:
  geoserver: {geoserver}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
"""]
    for name, kind, path in stores:
        docs.append(f'apiVersion: configops/v1alpha1\nkind: Store\nmetadata: {{ name: {name} }}\nspec: {{ type: {kind}, path: "{path}" }}\n')
        docs.append(f"apiVersion: configops/v1alpha1\nkind: Layer\nmetadata: {{ name: {name} }}\nspec: {{ store: {name} }}\n")
    folder = tmp_path / (files or "default").strip("/").replace("/", "_")
    folder.mkdir(exist_ok=True)
    (folder / "project.yaml").write_text("---\n".join(docs))
    return folder


@pytest.fixture(scope="module")
def placed(data_volume, tmp_path_factory):
    """sites.gpkg and dem.tif under /data/paths."""
    root = tmp_path_factory.mktemp("paths") / "paths"
    root.mkdir()
    src = root / "sites.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "a"}, "geometry": {"type": "Point", "coordinates": [517900, 151300]}}]}))
    run("ogr2ogr", "-f", "GPKG", str(root / "sites.gpkg"), str(src), "-a_srs", "EPSG:27700")
    asc = root / "seed.asc"
    asc.write_text("ncols 2\nnrows 2\nxllcorner 510000\nyllcorner 140000\ncellsize 5000\n10 20\n30 40\n")
    run("gdal_translate", "-q", "-a_srs", "EPSG:27700", "-outsize", "20", "20", str(asc), str(root / "dem.tif"))
    src.unlink()
    asc.unlink()
    data_volume.put(root, "paths")
    yield root
    data_volume.remove("paths")


@pytest.fixture
def gs(placed):
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    gs.delete_workspace(WS)
    yield gs
    gs.delete_workspace(WS)


def published(gs) -> dict:
    """What GeoServer read from each store: the feature type's attributes, the coverage's grid size."""
    ft = gs.get(f"workspaces/{WS}/datastores/sites/featuretypes/sites")["featureType"]
    cov = gs.get(f"workspaces/{WS}/coveragestores/dem/coverages/dem")["coverage"]
    names = [a["name"] for a in ft["attributes"]["attribute"]]
    high = cov["grid"]["range"]["high"].split()
    low = cov["grid"]["range"]["low"].split()
    return {"attributes": names, "grid": [int(h) - int(lo) for h, lo in zip(high, low)]}


def apply_and_check(gs, capsys, root, expected_urls: dict):
    code, out = configops(capsys, "apply", "-C", str(root))
    assert code == 0, out
    found = published(gs)
    assert found["grid"] == [20, 20] and "name" in found["attributes"], found  # GeoServer read both files
    stores = {"sites": gs.get(f"workspaces/{WS}/datastores/sites")["dataStore"],
              "dem": gs.get(f"workspaces/{WS}/coveragestores/dem")["coverageStore"]}
    entries = {e["@key"]: e["$"] for e in stores["sites"]["connectionParameters"]["entry"]}
    assert (entries["database"], stores["dem"]["url"]) == (expected_urls["sites"], expected_urls["dem"])
    code, out = configops(capsys, "plan", "-C", str(root))
    assert code == 0 and "No changes" in out, out  # GeoServer keeps the addresses as written


def imported_paths(capsys, root, tmp_path) -> dict:
    """What `import` writes back for the stores, into a copy of the project."""
    again = tmp_path / f"{root.name}-imported"
    shutil.copytree(root, again)
    for f in again.glob("*.yaml"):
        if f.name != "project.yaml":
            f.unlink()
    text = (again / "project.yaml").read_text().split("---\n")[0]
    (again / "project.yaml").write_text(text)
    code, out = configops(capsys, "import", WS, "-C", str(again))
    assert code == 0, out
    found = {}
    for f in again.rglob("*.yaml"):
        for doc in f.read_text().split("---\n"):
            if "kind: Store" in doc:
                name = doc.split("name:")[1].split()[0].strip("}")
                found[name] = [line.split("path:", 1)[1].strip().strip('"') for line in doc.splitlines() if "path:" in line][0]
    return found


def test_an_absolute_path_is_used_as_it_is(gs, project_env, capsys, tmp_path):
    root = project(tmp_path, None, [("sites", "geopackage", "/data/paths/sites.gpkg"), ("dem", "geotiff", "/data/paths/dem.tif")])
    apply_and_check(gs, capsys, root, {"sites": "/data/paths/sites.gpkg", "dem": "file:/data/paths/dem.tif"})
    assert imported_paths(capsys, root, tmp_path) == {"sites": "paths/sites.gpkg", "dem": "paths/dem.tif"}  # under /data: relative


def test_the_projects_files_folder_holds_relative_paths(gs, project_env, capsys, tmp_path):
    root = project(tmp_path, "/data/paths", [("sites", "geopackage", "sites.gpkg"), ("dem", "geotiff", "dem.tif")])
    apply_and_check(gs, capsys, root, {"sites": "/data/paths/sites.gpkg", "dem": "file:/data/paths/dem.tif"})
    assert imported_paths(capsys, root, tmp_path) == {"sites": "sites.gpkg", "dem": "dem.tif"}


@pytest.mark.skipif(not CONTAINER or "geoserver-1" not in (CONTAINER or ""),
                    reason="standard GeoServer's container (E2E_REST_CONTAINER) to copy files into its data directory")
def test_files_in_geoservers_own_data_directory(gs, project_env, capsys, tmp_path, placed):
    data_dir = subprocess.run(["docker", "exec", CONTAINER, "sh", "-c", "echo $GEOSERVER_DATA_DIR"],
                              capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["docker", "exec", CONTAINER, "mkdir", "-p", f"{data_dir}/data/paths"], check=True)
    for f in ("sites.gpkg", "dem.tif"):
        subprocess.run(["docker", "cp", str(placed / f), f"{CONTAINER}:{data_dir}/data/paths/{f}"], check=True, capture_output=True)
    try:
        root = project(tmp_path, "data", [("sites", "geopackage", "paths/sites.gpkg"), ("dem", "geotiff", "paths/dem.tif")])
        apply_and_check(gs, capsys, root, {"sites": "file:data/paths/sites.gpkg", "dem": "file:data/paths/dem.tif"})
        assert imported_paths(capsys, root, tmp_path) == {"sites": "paths/sites.gpkg", "dem": "paths/dem.tif"}
    finally:
        subprocess.run(["docker", "exec", "-u", "0", CONTAINER, "rm", "-rf", f"{data_dir}/data/paths"], capture_output=True)


@pytest.fixture
def project_env(env):
    return env
