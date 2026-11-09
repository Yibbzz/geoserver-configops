"""Pipeline helpers that don't need a database."""

import hashlib
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


def test_load_staging_rejects_unknown_format(tmp_path):
    with pytest.raises(ValueError, match="No file extension mapped"):
        pipeline.load_staging("x", tmp_path, "Vector Tiles", conn=None)
