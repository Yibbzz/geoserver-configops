"""Raster Datasets: a downloaded product made into one Cloud-Optimised GeoTIFF on the data volume.

Every raster in the download (inside zips, and zips inside those, as OS packs its
tiles) is mosaicked through a GDAL VRT and written as one deflate-compressed COG
with overviews. It's written beside the live file, checked, then renamed over it,
so GeoServer never reads half a file; the file it replaces goes to
.gisdata-backups/ beside the data, and the newest `backups` copies are kept.

The data volume is /data, as the chart mounts it; GISDATA_DATA_DIR points
elsewhere (a laptop, the e2e tests).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

from .ingest import LoadError, gdal_errors, stamp

DATA_ENV = "GISDATA_DATA_DIR"
RASTER_SUFFIXES = {".tif", ".tiff", ".asc", ".img", ".jp2", ".dem", ".bil", ".flt", ".vrt"}
BACKUPS = ".gisdata-backups"


def data_root() -> Path:
    return Path(os.environ.get(DATA_ENV, "/data"))


def live_path(path: str) -> Path:
    return data_root() / path.strip("/")


def unpack(download: Path, work: Path) -> list[Path]:
    """The raster files in a download: the file itself, or those in its zips (and zips in those)."""
    if download.suffix.lower() in RASTER_SUFFIXES:
        return [download]
    if not zipfile.is_zipfile(download):
        raise LoadError(f"{download.name} is neither a raster nor a zip of rasters")
    out = work / "unpacked"
    pending = [download]
    while pending:
        archive = pending.pop()
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                if name.endswith("/") or ".." in Path(name).parts:
                    continue
                target = out / archive.stem / name
                if name.lower().endswith(".zip"):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    pending.append(target)
                elif Path(name).suffix.lower() in RASTER_SUFFIXES | {".prj", ".aux", ".xml", ".tfw", ".hdr"}:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    rasters = sorted(p for p in out.rglob("*") if p.suffix.lower() in RASTER_SUFFIXES and not p.name.endswith(".aux.xml"))
    if not rasters:
        raise LoadError(f"no rasters in {download.name} ({', '.join(sorted(RASTER_SUFFIXES))})")
    return rasters


def gdal(*cmd: str) -> None:
    result = subprocess.run(list(cmd), capture_output=True, text=True)
    if result.returncode != 0:
        raise LoadError(f"{cmd[0]} failed: {gdal_errors(result.stderr)}")


def info(path: Path) -> dict:
    result = subprocess.run(["gdalinfo", "-json", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise LoadError(f"GDAL can't read {path.name}: {gdal_errors(result.stderr)}")
    return json.loads(result.stdout)


def build(rasters: list[Path], work: Path, out: Path, crs: str | None, fallback: str | None, nodata: float | None = None) -> dict:
    """
    Mosaic `rasters` into one COG at `out`. `crs` is assigned if given; `fallback` if the rasters
    have none. `nodata` marks where no raster covers (GDAL would write 0 there) and the rasters' own gaps.
    """
    listing = work / "rasters.txt"
    listing.write_text("".join(f"{p}\n" for p in rasters))
    vrt = work / "mosaic.vrt"
    has_crs = bool(info(rasters[0]).get("coordinateSystem", {}).get("wkt"))
    assign = crs or (None if has_crs else fallback)
    srs = ["-a_srs", assign] if assign else []
    empty = ["-vrtnodata", str(nodata), "-srcnodata", str(nodata)] if nodata is not None else []
    gdal("gdalbuildvrt", "-q", *srs, *empty, "-input_file_list", str(listing), str(vrt))
    gdal("gdal_translate", "-q", "-of", "COG", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=YES", "-co", "BIGTIFF=IF_SAFER",
         "-co", "NUM_THREADS=ALL_CPUS", "-co", "OVERVIEWS=AUTO", str(vrt), str(out))
    built = info(out)
    width, height = built.get("size", [0, 0])
    if not width or not height:
        raise LoadError("the COG came out empty")
    if not built.get("coordinateSystem", {}).get("wkt"):
        raise LoadError("the rasters have no CRS: set the dataset's raster.crs")
    return built


def install(new: Path, path: str, keep: int) -> None:
    """Rename `new` over the live file; the old one goes to the backups, the newest `keep` kept."""
    live = live_path(path)
    if live.exists():
        backups = data_root() / BACKUPS / Path(path).parent
        backups.mkdir(parents=True, exist_ok=True)
        os.replace(live, backups / f"{live.stem}.{stamp()}{live.suffix}")
        old = sorted(backups.glob(f"{live.stem}.*{live.suffix}"))
        for extra in old[:-keep] if keep else old:
            extra.unlink()
    os.replace(new, live)


def load(download: Path, work: Path, path: str, crs: str | None, fallback: str | None, keep: int, nodata: float | None = None) -> int:
    """Make the download into the COG at `path` on the data volume; returns its size in pixels."""
    if not data_root().is_dir():
        raise LoadError(f"no data volume at {data_root()}: deploy with values-data-volume.yaml (or set {DATA_ENV})")
    live = live_path(path)
    live.parent.mkdir(parents=True, exist_ok=True)
    rasters = unpack(download, work)
    new = live.with_name(f".{live.name}.{datetime.now():%Y%m%d%H%M%S}.new")  # beside the live file: the rename stays on one volume
    try:
        built = build(rasters, work, new, crs, fallback, nodata)
        install(new, path, keep)
    finally:
        new.unlink(missing_ok=True)
    width, height = built["size"]
    return width * height
