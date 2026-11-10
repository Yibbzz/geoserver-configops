"""Telling what kind of file a link is, from a declared format, media type or extension.

Portals label formats loosely ("SHP", "Shapefile", "ZIP", an IANA media-type
URL, or nothing), so all three are tried.
"""

from __future__ import annotations

from urllib.parse import urlparse

# Loadable file formats, best first: (label, declared names and media types, extensions)
FILE_FORMATS = [
    ("GeoPackage", {"gpkg", "geopackage", "application/geopackage+sqlite3"}, (".gpkg",)),
    ("Shapefile", {"shp", "shapefile", "esri shapefile", "esri® shapefile", "zipped shapefile", "shapefile (zip)"}, (".shp",)),
    ("GeoJSON", {"geojson", "application/geo+json", "application/vnd.geo+json"}, (".geojson",)),
    ("GML", {"gml", "application/gml+xml"}, (".gml",)),
    ("File Geodatabase", {"filegdb", "file geodatabase", "esri file geodatabase", "gdb"}, (".gdb",)),
    ("KML", {"kml", "kmz", "application/vnd.google-earth.kml+xml", "application/vnd.google-earth.kmz"}, (".kml", ".kmz")),
    ("FlatGeobuf", {"fgb", "flatgeobuf"}, (".fgb",)),
    # A zip could hold anything; reading it tells
    ("ZIP", {"zip", "application/zip", "application/x-zip-compressed"}, (".zip",)),
]
FILE_LABELS = [label for label, _, _ in FILE_FORMATS]

# Web services: not files; later source types (wfs, ogc-api-features) cover them
SERVICES = {
    "wfs": "WFS", "ogc wfs": "WFS", "wms": "WMS", "ogc wms": "WMS", "wmts": "WMTS",
    "ogc api - features": "OGC API Features", "arcgis geoservices rest api": "ArcGIS REST",
    "esri rest": "ArcGIS REST", "feature server": "ArcGIS REST",
}

MEDIA_TYPE_PREFIX = "https://www.iana.org/assignments/media-types/"


def declared(fmt: str | None) -> str:
    fmt = (fmt or "").strip().lower()
    return fmt.removeprefix(MEDIA_TYPE_PREFIX).lstrip(".")


def by_extension(name: str) -> str | None:
    """Format from a file name; a zip named like `roads.gpkg.zip` counts as what it holds."""
    name = name.lower().strip()
    inner = name.removesuffix(".zip")
    for path in ([inner, name] if inner != name else [name]):
        for label, _, extensions in FILE_FORMATS:
            if path.endswith(extensions):
                return label
    return None


def classify(fmt: str | None = None, url: str = "", mimetype: str | None = None, name: str = "") -> str | None:
    """The file format label for a link, or None if it isn't a loadable file."""
    from_names = by_extension(urlparse(url).path) or by_extension(name)
    known_names = {declared(fmt), declared(mimetype)}
    declared_label = next((label for label, known, _ in FILE_FORMATS if known_names & known), None)
    if declared_label == "ZIP" and from_names:
        return from_names  # "ZIP" says little; the name says what's inside
    return declared_label or from_names


def service(fmt: str | None) -> str | None:
    return SERVICES.get(declared(fmt))


def rank(label: str | None) -> int:
    return FILE_LABELS.index(label) if label in FILE_LABELS else len(FILE_LABELS)
