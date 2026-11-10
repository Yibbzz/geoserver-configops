"""Read what's inside a download without downloading it.

A zip's file list sits at its end, so a few HTTP range requests are enough
to read it. GDAL then opens each data file in place (/vsizip/ over
/vsicurl/) and reports its layers. Opening a GeoPackage inside a compressed
zip still means reading through much of the archive, so a large product can
take a minute or two; callers cache the result. A server that can't send
parts of a file has it downloaded to a temporary folder instead, up to
MAX_DOWNLOAD bytes.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

# Data files GDAL can list layers from, by extension. Shapefile parts other
# than .shp, and docs, are left out.
DATA_EXTENSIONS = (".gpkg", ".shp", ".gml", ".tab", ".geojson", ".json", ".csv", ".kml", ".fgb")

# Formats whose feature count means reading the whole file
NO_COUNT_EXTENSIONS = (".gml", ".csv", ".geojson", ".json", ".kml")

TIMEOUT = 60

# Largest file downloaded whole when the server can't send parts of it
MAX_DOWNLOAD = 300_000_000

# Larger HTTP reads: GDAL reads a compressed archive front to back, and its
# default 16 KB requests make that slow. Retries: one 504 from a busy server
# (seen on the EA's OGC API) would otherwise fail a load of hundreds of pages.
# Overridable from the environment.
GDAL_ENV = {"CPL_VSIL_CURL_CHUNK_SIZE": "2097152", "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "GDAL_HTTP_MAX_RETRY": "5", "GDAL_HTTP_RETRY_DELAY": "10"}


class RemoteError(Exception):
    """A download couldn't be read; the message is for the user."""


class NoRanges(RemoteError):
    """The server sends whole files only."""

    def __init__(self, status: int, size: int | None):
        super().__init__(f"the server doesn't allow partial downloads (HTTP {status})")
        self.size = size


@dataclass
class Layer:
    name: str
    file: str  # path inside the archive ("" when the download isn't an archive)
    geometry: str | None  # e.g. "LineString"; None for a table without geometry
    crs: str | None  # e.g. "EPSG:27700"
    features: int | None  # None when counting would mean reading the whole file

    def to_dict(self) -> dict:
        return {"name": self.name, "file": self.file, "geometry": self.geometry, "crs": self.crs, "features": self.features}

    @classmethod
    def from_dict(cls, d: dict) -> "Layer":
        return cls(d["name"], d.get("file", ""), d.get("geometry"), d.get("crs"), d.get("features"))


class HttpFile(io.RawIOBase):
    """A read-only, seekable file over HTTP range requests."""

    def __init__(self, url: str, session: requests.Session | None = None):
        self.url = url
        self.session = session or requests.Session()
        self.pos = 0
        r = self.session.get(url, headers={"Range": "bytes=0-0"}, timeout=TIMEOUT, stream=True)
        r.close()
        if r.status_code != 206 or "Content-Range" not in r.headers:
            length = r.headers.get("Content-Length", "")
            raise NoRanges(r.status_code, int(length) if r.status_code == 200 and length.isdigit() else None)
        self.size = int(r.headers["Content-Range"].rsplit("/", 1)[1])

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = max(0, base + offset)
        return self.pos

    def readinto(self, buffer) -> int:
        if self.pos >= self.size or len(buffer) == 0:
            return 0
        end = min(self.pos + len(buffer), self.size) - 1
        r = self.session.get(self.url, headers={"Range": f"bytes={self.pos}-{end}"}, timeout=TIMEOUT)
        r.raise_for_status()
        n = len(r.content)
        buffer[:n] = r.content
        self.pos += n
        return n


def zip_entries(fileobj) -> list[str] | None:
    """Paths of the files in a zip, or None if it isn't a zip."""
    try:
        with zipfile.ZipFile(fileobj) as z:
            return [i.filename for i in z.infolist() if not i.is_dir()]
    except zipfile.BadZipFile:
        return None


def data_files(entries: list[str]) -> list[str]:
    """Files GDAL can read, plus File Geodatabase folders (`x.gdb/`), which GDAL opens whole."""
    files = {e for e in entries if e.lower().endswith(DATA_EXTENSIONS) and not e.split("/")[-1].startswith(".")}
    for e in entries:
        parts = e.split("/")
        for i, part in enumerate(parts[:-1]):
            if part.lower().endswith(".gdb"):
                files.add("/".join(parts[: i + 1]))
                break
    return sorted(files)


def ogrinfo(path: str, layer: str | None = None) -> list[dict]:
    """
    Layer summaries GDAL gives for a dataset (a path, any GDAL virtual path,
    or a connection string such as WFS:<url>): every layer, or just `layer`.
    """
    if shutil.which("ogrinfo") is None:
        raise RemoteError("reading layers needs GDAL's ogrinfo (3.7 or later) on the PATH")
    args = ["ogrinfo", "-ro", "-so", "-json", "-noextent"]
    if path.lower().endswith(NO_COUNT_EXTENSIONS):
        args.append("-nocount")
    env = {**GDAL_ENV, **os.environ}
    target = [path, layer] if layer else ["-al", path]
    result = subprocess.run(args + target, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        raise RemoteError(f"GDAL couldn't read {path.rsplit('/', 1)[-1]}: {detail[-1] if detail else 'no details'}")
    try:
        return json.loads(result.stdout).get("layers", [])
    except json.JSONDecodeError:
        raise RemoteError("ogrinfo gave no JSON output; GDAL 3.7 or later is needed") from None


def crs_name(coordinate_system: dict | None) -> str | None:
    if not coordinate_system:
        return None
    ident = (coordinate_system.get("projjson") or {}).get("id") or {}
    if ident.get("authority") and ident.get("code"):
        return f"{ident['authority']}:{ident['code']}"
    return (coordinate_system.get("projjson") or {}).get("name")


def to_layers(info: list[dict], file: str) -> list[Layer]:
    layers = []
    for layer in info:
        geom = (layer.get("geometryFields") or [None])[0]
        layers.append(
            Layer(
                name=layer["name"],
                file=file,
                geometry=geom.get("type") if geom else None,
                crs=crs_name(geom.get("coordinateSystem")) if geom else None,
                features=layer.get("featureCount"),
            )
        )
    return layers


def archive_layers(fileobj, gdal_path: str) -> list[Layer]:
    """
    Layers of every data file in a download. `fileobj` reads the download;
    `gdal_path` is how GDAL reaches the same bytes (a local path, or
    /vsicurl/<url>).
    """
    entries = zip_entries(fileobj)
    if entries is None:
        return to_layers(ogrinfo(gdal_path), "")
    files = data_files(entries)
    if not files:
        raise RemoteError("the download holds no data files GDAL can read")
    layers = []
    for f in files:
        layers += to_layers(ogrinfo(f"/vsizip/{{{gdal_path}}}/{f}"), f)
    return layers


def url_layers(url: str, session: requests.Session | None = None, file_name: str = "") -> list[Layer]:
    """
    Layers of the download at `url` (a direct link, redirects already
    followed). `file_name` keeps the extension GDAL may need if the file has
    to be fetched whole.
    """
    session = session or requests.Session()
    try:
        try:
            with io.BufferedReader(HttpFile(url, session), buffer_size=64 * 1024) as f:
                return archive_layers(f, f"/vsicurl/{url}")
        except NoRanges as e:
            if e.size is not None and e.size > MAX_DOWNLOAD:
                raise RemoteError(f"{e}, and the file is too large to fetch whole for a look ({e.size:,} bytes)") from None
            print("The server sends whole files only; downloading it to a temporary folder to read it...", file=sys.stderr)
            with tempfile.TemporaryDirectory(prefix="gisdata-") as tmp:
                suffix = Path(file_name or urlparse(url).path).suffix
                path = fetch(url, session, Path(tmp) / f"download{suffix}")
                with open(path, "rb") as f:
                    return archive_layers(f, str(path))
    except requests.RequestException as e:
        raise RemoteError(f"couldn't read the download: {e}") from None


def fetch(url: str, session: requests.Session, dest: Path, limit: int | None = None) -> Path:
    """Download `url` to `dest`, stopping past `limit` bytes (MAX_DOWNLOAD when not given)."""
    limit = MAX_DOWNLOAD if limit is None else limit
    with session.get(url, stream=True, timeout=TIMEOUT) as r:
        r.raise_for_status()
        written = 0
        with open(dest, "wb") as out:
            for chunk in r.iter_content(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise RemoteError(f"the file is larger than {limit:,} bytes, too large to fetch whole for a look")
                out.write(chunk)
    return dest
