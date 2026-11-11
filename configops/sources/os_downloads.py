"""OS Downloads API (https://api.os.uk/downloads/v1). OpenData products need no key."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import requests

from . import Download, Listing, NotFound, Product, SourceError, http_session, matches

BASE_API = "https://api.os.uk/downloads/v1/products"
TIMEOUT = 30

# Formats the loader can read, best first; raster, vector tiles and CSV are left out.
FORMAT_PREFERENCE = [
    "GeoPackage",
    "ESRI® Shapefile",
    "GML",
    "MapInfo® TAB",
    "MapInfo",
]

# Raster formats, best first, for a Dataset with `raster:`
RASTER_PREFERENCE = ["GeoTIFF", "ASCII Grid and GML (Grid)", "ASCII Grid"]

# Area OS uses for a single download covering all of Great Britain
WHOLE_AREA = "GB"


def choose(downloads: list[Download], preference: list[str] = FORMAT_PREFERENCE) -> Download | None:
    """
    The download sync would use: the most preferred format available as one
    file. A format offered only as many grid-square tiles is skipped, since
    each tile would become its own table.
    """
    for fmt in preference:
        candidates = [d for d in downloads if d.format == fmt]
        if len(candidates) == 1:
            return candidates[0]
        whole = [d for d in candidates if d.area == WHOLE_AREA]
        if whole:
            return whole[0]
    return None


def loadable_format(formats: list[str]) -> str | None:
    """Best loadable format from a product's format list (no area information)."""
    return next((f for f in FORMAT_PREFERENCE if f in formats), None)


class OSDownloads:
    name = "OS Downloads API"
    formats = FORMAT_PREFERENCE

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        self.api_key = api_key
        self.session = session or http_session()
        self.base = (url or BASE_API).rstrip("/")

    def _get(self, url: str, **kwargs) -> requests.Response:
        params = {"key": self.api_key} if self.api_key else {}
        try:
            return self.session.get(url, params=params, timeout=TIMEOUT, **kwargs)
        except requests.RequestException as e:
            raise SourceError(f"can't reach the {self.name}: {e}") from None

    def _json(self, url: str, what: str):
        r = self._get(url)
        if r.status_code == 404:
            raise NotFound(what)
        if r.status_code in (401, 403):
            raise SourceError(f"the {self.name} refused the request (HTTP {r.status_code}); check the API key")
        if r.status_code != 200:
            raise SourceError(f"the {self.name} answered HTTP {r.status_code} for {url}")
        try:
            return r.json()
        except ValueError:
            raise SourceError(f"the {self.name} sent something that isn't JSON for {url}") from None

    def _product(self, data: dict) -> Product:
        formats = [f["format"] for f in data.get("formats", []) if f.get("format")]
        description = data.get("description") or ""
        if isinstance(description, list):
            description = " ".join(description)
        return Product(
            id=data["id"],
            name=data.get("name") or data["id"],
            version=data.get("version"),
            formats=formats,
            loads_as=loadable_format(formats),
            description=description,
        )

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        listing = [p for p in self._json(self.base, "product list") if matches(self._product(p), search)]
        total = len(listing)
        listing = listing[:limit] if limit else listing
        # The list has no formats; each product's own record does
        with ThreadPoolExecutor(max_workers=8) as pool:
            details = list(pool.map(lambda p: self._json(f"{self.base}/{p['id']}", p["id"]), listing))
        return Listing(sorted((self._product(d) for d in details), key=lambda p: p.id.lower()), total)

    def product(self, product_id: str, resource: str | None = None, raster: bool = False) -> Product:
        if resource:
            raise SourceError("OS products have no resources to pick; sync uses the download marked *")
        product = self._product(self._json(f"{self.base}/{product_id}", product_id))
        data = self._json(f"{self.base}/{product_id}/downloads", product_id)
        items = data if isinstance(data, list) else data.get("downloads", [])
        product.downloads = [
            Download(
                format=d.get("format", ""),
                subformat=d.get("subformat") or "",
                url=d.get("url", ""),
                area=d.get("area") or "",
                file_name=d.get("fileName") or "",
                size=d.get("size"),
                md5=d.get("md5"),
            )
            for d in items
            if d.get("format") and d.get("url")
        ]
        product.chosen = choose(product.downloads, RASTER_PREFERENCE if raster else FORMAT_PREFERENCE)
        product.loads_as = product.chosen.format if product.chosen else None
        return product

    def download_url(self, download: Download) -> str:
        """Direct link to the file: OS answers with a redirect to signed storage, valid about an hour."""
        r = self._get(download.url, allow_redirects=False, stream=True)  # stream: never pull the file itself
        r.close()
        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            return r.headers["Location"]
        if r.status_code == 200:
            return download.url
        raise SourceError(f"the {self.name} answered HTTP {r.status_code} for {download.file_name or download.url}")
