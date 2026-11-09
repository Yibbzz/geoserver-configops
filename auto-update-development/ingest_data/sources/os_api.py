"""
sources/os_api.py
=================
OS Downloads API source. Fetches available vector datasets and returns
them in the standard pipeline format consumed by pipeline.py.

Standard dataset dict:
    {
        "id":      str,   # unique stable ID (matches OS product ID)
        "name":    str,   # human readable label
        "version": str,   # e.g. "2026-04"
        "format":  str,   # e.g. "GeoPackage"
        "url":     str,   # direct download URL
        "size":    int,   # bytes (optional)
        "md5":     str,   # checksum (optional)
        "srs":     str,   # OS OpenData is British National Grid
        "headers": dict,  # API key header for the download
    }
"""

import os
import requests

BASE_API = "https://api.os.uk/downloads/v1/products"
API_KEY = os.environ.get("OS_API_KEY", "")

# Preference order — first match wins. Raster, vector tile and non-spatial
# formats are absent so they are skipped (the pipeline can't load them).
FORMAT_PREFERENCE = [
    "GeoPackage",
    "ESRI® Shapefile",
    "GML",
    "MapInfo® TAB",
    "MapInfo",
]

# Dataset IDs to never ingest
BLOCKLIST = [
    "OpenUPRN",
    "OpenUSRN",
]

# Dataset IDs to exclusively ingest (empty = process all)
ALLOWLIST = []


def get_datasets() -> list[dict]:
    """
    Fetch all OS vector datasets and return them in the standard pipeline format.
    Called by main.py — do not rename this function.
    """
    response = requests.get(
        BASE_API,
        params={"key": API_KEY} if API_KEY else {},
        timeout=30,
    )
    response.raise_for_status()
    products = response.json()

    if ALLOWLIST:
        products = [p for p in products if p["id"] in ALLOWLIST]

    if BLOCKLIST:
        products = [p for p in products if p["id"] not in BLOCKLIST]

    results = []
    for product in products:
        pid = product["id"]
        info = _discover(pid)
        if info is None:
            continue  # no usable vector format available

        results.append(
            {
                "id": pid,
                "name": product.get("name", pid),
                "version": product.get("version") or "unknown",
                "format": info["format"],
                "url": info["url"],
                "size": info.get("size"),
                "md5": info.get("md5"),
                "srs": "EPSG:27700",
                "headers": {"key": API_KEY} if API_KEY else {},
            }
        )

    return results


def _discover(product_id: str) -> dict | None:
    """Fetch download options for a single product and pick the best format."""
    r = requests.get(
        f"{BASE_API}/{product_id}/downloads",
        params={"key": API_KEY} if API_KEY else {},
        timeout=30,
    )
    if r.status_code != 200:
        return None

    data = r.json()
    downloads = data if isinstance(data, list) else data.get("downloads", [])
    by_format = {
        item.get("format", ""): item for item in downloads if item.get("format")
    }

    chosen_fmt = next((f for f in FORMAT_PREFERENCE if f in by_format), None)
    if chosen_fmt is None:
        return None

    item = by_format[chosen_fmt]
    return {
        "format": chosen_fmt,
        "url": item.get("url") or item.get("downloadURL"),
        "size": item.get("size"),
        "md5": item.get("md5"),
    }
