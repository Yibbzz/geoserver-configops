"""Coordinate reference system names, in the short form the spec uses (EPSG:27700)."""

from __future__ import annotations

import re

# urn:ogc:def:crs:EPSG::27700, urn:x-ogc:def:crs:EPSG:27700, EPSG:27700,
# http://www.opengis.net/def/crs/EPSG/0/27700, http://www.opengis.net/gml/srs/epsg.xml#27700
EPSG = re.compile(r"(?:EPSG(?::[\d.]*)?:|/EPSG/[\d.]+/|epsg\.xml#)(\d+)$", re.IGNORECASE)
CRS84 = re.compile(r"(?:OGC/1\.3/|OGC:(?:1\.3:)?)CRS84$", re.IGNORECASE)


def short(name: str | None) -> str | None:
    """EPSG:<code> or CRS84 when recognised, else the name as given."""
    if not name:
        return None
    name = name.strip()
    if CRS84.search(name):
        return "CRS84"
    m = EPSG.search(name)
    return f"EPSG:{m.group(1)}" if m else name
