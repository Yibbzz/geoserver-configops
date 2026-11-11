"""WFS (OGC Web Feature Service, versions 1.0 to 2.0). Each feature type is a product.

The feature list comes from GetCapabilities; a feature type's details
(geometry, CRS, count) from GDAL's WFS driver, which is also what loads it,
so the layer name shown is the one the loader uses.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import requests

from .. import crs, remote
from . import Download, Listing, NotFound, Product, SourceError, http_session, matches
from .formats import service_address

TIMEOUT = 60
FORMAT = "WFS"


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(element: ET.Element, *names: str) -> str | None:
    for name in names:
        for c in element:
            if local(c.tag) == name and c.text and c.text.strip():
                return c.text.strip()
    return None


def parse_capabilities(xml: bytes) -> tuple[str | None, list[dict]]:
    """(version, feature types) from a GetCapabilities document."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise SourceError("the address didn't answer with a WFS capabilities document (not XML)") from None
    if local(root.tag) in ("ExceptionReport", "ServiceExceptionReport"):
        text = " ".join(t.strip() for t in root.itertext() if t.strip())
        raise SourceError(f"the WFS answered with an error: {text or 'no details'}")
    if local(root.tag) != "WFS_Capabilities":
        raise SourceError(f"the address answered with <{local(root.tag)}>, not a WFS capabilities document")
    types = []
    for ft in root.iter():
        if local(ft.tag) != "FeatureType":
            continue
        name = child_text(ft, "Name")
        if not name:
            continue
        types.append({
            "name": name,
            "title": child_text(ft, "Title") or name,
            "abstract": child_text(ft, "Abstract") or "",
            "crs": crs.short(child_text(ft, "DefaultCRS", "DefaultSRS", "SRS")),
        })
    return root.get("version"), types


class WFS:
    name = "WFS"
    formats = [FORMAT]

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        if not url:
            raise SourceError("a wfs source needs the service's address: spec.url in the Source, or --url")
        # An address copied with ?service=WFS&request=GetCapabilities still works
        self.url = service_address(FORMAT, url)
        self.session = session or http_session()
        self._capabilities: tuple[str | None, list[dict]] | None = None

    def capabilities(self) -> tuple[str | None, list[dict]]:
        if self._capabilities is None:
            try:
                r = self.session.get(self.url, params={"service": "WFS", "request": "GetCapabilities"}, timeout=TIMEOUT)
            except requests.RequestException as e:
                raise SourceError(f"can't reach {self.url}: {e}") from None
            if r.status_code in (401, 403):
                raise SourceError(f"{self.url} refused the request (HTTP {r.status_code})")
            if r.status_code != 200:
                raise SourceError(f"{self.url} answered HTTP {r.status_code}; is it a WFS address?")
            self._capabilities = parse_capabilities(r.content)
        return self._capabilities

    def _product(self, ft: dict) -> Product:
        return Product(
            id=ft["name"], name=ft["title"], version=None, formats=[FORMAT], loads_as=FORMAT,
            description=ft["abstract"],
        )

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        _, types = self.capabilities()
        found = [p for p in (self._product(ft) for ft in types) if matches(p, search)]
        return Listing(found[:limit] if limit else found, len(found))

    def product(self, name: str, resource: str | None = None) -> Product:
        if resource:
            raise SourceError("a WFS feature type is one layer; there are no resources to pick")
        version, types = self.capabilities()
        # Accept the name with or without its namespace prefix
        ft = next((t for t in types if t["name"] == name), None) or next(
            (t for t in types if t["name"].split(":")[-1] == name), None)
        if ft is None:
            raise NotFound(name)
        p = self._product(ft)
        download = Download(format=FORMAT, subformat=version or "", url=self.url, name=ft["title"], service=True)
        p.downloads, p.chosen = [download], download
        return p

    def layers(self, product: Product) -> list[remote.Layer]:
        layers = remote.to_layers(remote.ogrinfo(f"WFS:{self.url}", product.id), "")
        default_crs = next((t["crs"] for t in self.capabilities()[1] if t["name"] == product.id), None)
        for layer in layers:
            layer.crs = layer.crs or default_crs
        return layers

    def gdal_source(self, product: Product) -> tuple[str, str]:
        """(GDAL connection, layer) that loads this feature type."""
        return f"WFS:{self.url}", product.id

    def download_url(self, download: Download) -> str:
        return download.url
