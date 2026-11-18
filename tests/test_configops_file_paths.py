"""Where file stores are: under the Project's geoserver.files (/data unless set), or at an absolute path."""

import pytest

from configops.importer import data_path
from configops.loader import load
from configops.refresh import normal_path
from configops.stores import files_root, location
from configops.validate import validate


@pytest.mark.parametrize("root, path, url", [
    ("/data", "rasters/dem.tif", "file:/data/rasters/dem.tif"),             # GeoServer Cloud, the container images
    ("/srv/gis", "rasters/dem.tif", "file:/srv/gis/rasters/dem.tif"),       # a folder on the server
    ("data", "rasters/dem.tif", "file:data/rasters/dem.tif"),               # GeoServer's data directory, as its UI writes it
    ("D:/GIS", "rasters/dem.tif", "file:///D:/GIS/rasters/dem.tif"),        # Windows
    ("/data", "/mnt/share/dem.tif", "file:/mnt/share/dem.tif"),             # absolute: as it is, whatever the folder
    ("/data", "E:\\GIS Data\\dem.tif", "file:///E:/GIS Data/dem.tif"),
])
def test_a_stores_path_becomes_geoservers_address_and_imports_back(root, path, url):
    assert location({"path": path}, root) == url
    expected = path.replace("\\", "/")
    assert data_path(url, root) == expected  # import writes back what apply sent


def test_a_geopackage_is_given_a_plain_path():
    from configops.stores import wanted_store
    from configops.loader import Resource
    store = Resource(data={"apiVersion": "configops/v1alpha1", "kind": "Store", "metadata": {"name": "sites"},
                           "spec": {"type": "geopackage", "path": "sites.gpkg"}}, file=None, node=None)
    for root, database in (("/data", "/data/sites.gpkg"), ("data", "file:data/sites.gpkg"), ("D:/GIS", "D:/GIS/sites.gpkg")):
        body, _ = wanted_store(store, None, None, root)
        assert {e["@key"]: e["$"] for e in body["connectionParameters"]["entry"]}["database"] == database


def test_import_leaves_out_a_data_directory_path_it_cant_write():
    assert data_path("file:data/sf/roads.shp", "/data") is None  # the note says to set geoserver.files: data
    assert data_path("file:data/sf/roads.shp", "data") == "sf/roads.shp"


def test_files_root_is_the_projects_or_data():
    assert files_root({"geoserver": {"url": "x"}}) == "/data"
    assert files_root({"geoserver": {"files": "D:\\GIS\\"}}) == "D:/GIS"


def test_refresh_matches_a_loaders_path_to_the_store():
    assert normal_path("/data/rasters/dem.tif") == "rasters/dem.tif"
    assert normal_path("/srv/gis/rasters/dem.tif", "/srv/gis") == "rasters/dem.tif"
    assert normal_path("rasters/dem.tif", "/srv/gis") == "rasters/dem.tif"
    assert normal_path("/mnt/share/dem.tif", "/srv/gis") == "/mnt/share/dem.tif"


def test_validate_takes_absolute_paths_and_folders_with_spaces_but_not_dot_dot(tmp_path):
    def errors(path: str, files: str = "") -> list[str]:
        (tmp_path / "project.yaml").write_text(f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: t }}
spec:
  geoserver: {{ url: "http://localhost:8080/geoserver", credentials: {{ secret: gs }}{', files: "' + files + '"' if files else ''} }}
  workspace: w
---
apiVersion: configops/v1alpha1
kind: Store
metadata: {{ name: dem }}
spec: {{ type: geotiff, path: "{path}" }}
""")
        return [p.message for p in validate(load(tmp_path)) if p.severity == "error"]
    assert errors("/srv/gis/dem.tif") == []
    assert errors("D:/GIS Data/dem.tif") == []
    assert errors("dem.tif", files="D:/GIS") == []
    assert errors("../etc/dem.tif")
