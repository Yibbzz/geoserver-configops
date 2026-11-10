"""Source types. Each lists what a provider offers and where to download it.

A source type is a class built as Type(api_key=None, url=None), with a
`formats` list (loadable formats, best first) and:
    products(search, limit) -> Listing   products, with the format sync would load
    product(id, resource) -> Product     one product, with all its downloads
    download_url(download) -> str        a direct link to the file (redirects followed)
    layers(product) -> list[Layer]       services only: read layers without a file
    gdal_source(product) -> (str, str)   services only: the GDAL connection and layer that load it

`url` is the Source's own address (a CKAN portal, say); `resource` picks
one download when a product has several. Fetching and loading come later,
with `sync`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class SourceError(Exception):
    """The provider couldn't answer; the message is for the user."""


class NotFound(SourceError):
    pass


@dataclass
class Download:
    format: str  # e.g. "GeoPackage"
    url: str  # may redirect
    area: str = ""  # e.g. "GB", or a grid square for tiled products
    file_name: str = ""
    size: int | None = None  # bytes
    md5: str | None = None  # or another checksum the provider gives
    subformat: str = ""
    id: str = ""  # the provider's ID for this download (a CKAN resource ID)
    name: str = ""  # the provider's title for it
    loadable: bool = True  # False for links gisdata can't load (web pages, map images)
    service: bool = False  # a feature service (WFS, OGC API Features), not a file

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "format": self.format, "subformat": self.subformat,
            "area": self.area, "fileName": self.file_name, "size": self.size, "md5": self.md5,
            "url": self.url, "loadable": self.loadable, "service": self.service,
        }


@dataclass
class Product:
    id: str
    name: str
    version: str | None
    formats: list[str] = field(default_factory=list)
    downloads: list[Download] = field(default_factory=list)  # filled by product(), not products()
    loads_as: str | None = None  # the format sync would load; None if none can be loaded
    chosen: Download | None = None  # the download sync would use (product() only)
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "version": self.version, "formats": self.formats,
            "loadsAs": self.loads_as, "description": self.description,
        }


@dataclass
class Listing:
    products: list[Product]
    total: int | None = None  # matches at the source, when more exist than were listed


def http_session() -> requests.Session:
    """A session that tries again, twice, when a server drops the connection or is briefly unavailable."""
    session = requests.Session()
    retry = Retry(total=2, connect=2, read=2, status=2, backoff_factor=0.5,
                  status_forcelist=(502, 503, 504), allowed_methods=("GET", "HEAD"), raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def matches(product: Product, search: str | None) -> bool:
    """Every word of `search` appears in the product's ID, name or description."""
    if not search:
        return True
    text = f"{product.id} {product.name} {product.description}".lower()
    return all(word in text for word in search.lower().split())


def source_types() -> dict[str, type]:
    from .ckan import CKAN
    from .ogcapi import OGCAPIFeatures
    from .os_downloads import OSDownloads
    from .url import URL
    from .wfs import WFS

    return {"os-downloads": OSDownloads, "ckan": CKAN, "url": URL, "wfs": WFS, "ogc-api-features": OGCAPIFeatures}
