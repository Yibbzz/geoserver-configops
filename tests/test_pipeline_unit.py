"""Pipeline helpers that don't need a database."""

import hashlib
import shutil
import subprocess
import zipfile

import pytest

import pipeline


def test_verify_returns_sha256_and_checks_md5(tmp_path):
    f = tmp_path / "data.zip"
    f.write_bytes(b"hello" * 100_000)
    data = f.read_bytes()
    md5 = hashlib.md5(data).hexdigest()

    assert pipeline.verify(f, None) == hashlib.sha256(data).hexdigest()
    assert pipeline.verify(f, md5.upper()) == hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError, match="MD5 mismatch"):
        pipeline.verify(f, "0" * 32)


def test_extract_unzips_nested_files(tmp_path):
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("data/sub/roads.gpkg", b"x")
    out = pipeline.extract(archive, tmp_path / "out")
    assert pipeline.find_file(out, ".gpkg").name == "roads.gpkg"
    assert pipeline.find_file(out, ".shp") is None


def test_extract_rejects_non_zip(tmp_path):
    f = tmp_path / "a.tar.gz"
    f.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="Unsupported archive"):
        pipeline.extract(f, tmp_path / "out")


def test_source_layers_rejects_unknown_format(tmp_path):
    with pytest.raises(ValueError, match="No file extension mapped"):
        pipeline.source_layers(tmp_path, "Vector Tiles")


def test_source_layers_needs_a_data_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        pipeline.source_layers(tmp_path, "GeoPackage")


@pytest.mark.parametrize("parts, expected", [
    (("OpenRoads",), "openroads"),
    (("OpenRoads", "road_link"), "openroads_road_link"),
    (("Boundary-Line", "District Borough Unitary"), "boundary_line_district_borough_unitary"),
    (("2026 data",), "t_2026_data"),
    (("x" * 80,), "x" * 63),
])
def test_table_name(parts, expected):
    assert pipeline.table_name(*parts) == expected


def test_single_layer_product_keeps_its_table_name(tmp_path):
    f = tmp_path / "roads.gpkg"
    assert pipeline.plan_tables("OpenRoads", [(f, "road_node")]) == [(f, "road_node", "openroads")]


def test_multi_layer_product_gets_a_table_per_layer(tmp_path):
    f = tmp_path / "roads.gpkg"
    planned = pipeline.plan_tables("OpenRoads", [(f, "road_link"), (f, "road_node")])
    assert [t for _, _, t in planned] == ["openroads_road_link", "openroads_road_node"]


def test_layers_that_collide_are_refused(tmp_path):
    with pytest.raises(ValueError, match="same table name"):
        pipeline.plan_tables("x", [(tmp_path / "a.shp", "Roads"), (tmp_path / "b.shp", "roads")])


@pytest.mark.skipif(shutil.which("ogr2ogr") is None, reason="ogr2ogr not installed")
def test_list_layers_reads_every_layer_of_a_geopackage(tmp_path):
    geojson = tmp_path / "f.geojson"
    geojson.write_text('{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},'
                       '"geometry":{"type":"Point","coordinates":[1,2]}}]}')
    gpkg = tmp_path / "two.gpkg"
    for layer in ("road_link", "road_node"):
        subprocess.run(["ogr2ogr", "-f", "GPKG", "-update", "-append", str(gpkg), str(geojson), "-nln", layer],
                       check=True, capture_output=True)
    assert pipeline.list_layers(gpkg) == ["road_link", "road_node"]
