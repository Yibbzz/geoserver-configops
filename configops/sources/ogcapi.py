"""OGC API Features (the JSON successor to WFS). Each feature collection is a product.

Collections come from /collections; a collection's details from the
collection itself and one item: `numberMatched` for the count, the first
feature for the geometry type. The layer name is the collection ID, as
GDAL's OAPIF driver (the loader) names it.
"""

from __future__ import annotations

import requests

from .. import crs, remote
from . import Download, Listing, NotFound, Product, SourceError, http_session, matches
from .formats import service_address

TIMEOUT = 60
FORMAT = "OGC API Features"
MAX_PAGES = 50  # of /collections, for servers that page it
JSON = "application/json"
GEOJSON = "application/geo+json, application/json;q=0.9"


def is_features(collection: dict) -> bool:
    """A feature collection, not coverages, maps or records. Some servers leave itemType out."""
    item_type = collection.get("itemType")
    if item_type is not None:
        return item_type == "feature"
    return any(link.get("rel") == "items" for link in collection.get("links") or [])


class OGCAPIFeatures:
    name = "OGC API Features"
    formats = [FORMAT]

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        if not url:
            raise SourceError("an ogc-api-features source needs the API's address: spec.url in the Source, or --url")
        # A collection's address (…/collections/roads) still works
        self.url = service_address(FORMAT, url)
        self.session = session or http_session()
        self._collections: dict[str, dict] = {}

    def _json(self, url: str, what: str, accept: str = JSON, params: dict | None = None):
        try:
            r = self.session.get(url, params=params, headers={"Accept": accept}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise SourceError(f"can't reach {url}: {e}") from None
        if r.status_code == 404:
            raise NotFound(what)
        if r.status_code in (401, 403):
            raise SourceError(f"{url} refused the request (HTTP {r.status_code})")
        if r.status_code != 200:
            raise SourceError(f"{url} answered HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise SourceError(f"{url} didn't answer with JSON; is it an OGC API address?") from None

    def _product(self, c: dict) -> Product:
        return Product(
            id=c["id"], name=c.get("title") or c["id"], version=None, formats=[FORMAT], loads_as=FORMAT,
            description=c.get("description") or "",
        )

    def collections(self) -> list[dict]:
        found, url, params = [], f"{self.url}/collections", None
        for _ in range(MAX_PAGES):
            data = self._json(url, "collections", params=params)
            if not isinstance(data, dict) or "collections" not in data:
                raise SourceError(f"{url} has no collections list; is it an OGC API Features address?")
            found += [c for c in data["collections"] if isinstance(c, dict) and c.get("id")]
            nxt = next((link["href"] for link in data.get("links") or [] if link.get("rel") == "next" and link.get("href")), None)
            if not nxt or nxt == url:
                break
            url, params = nxt, None
        return found

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        found = [p for p in (self._product(c) for c in self.collections() if is_features(c)) if matches(p, search)]
        return Listing(found[:limit] if limit else found, len(found))

    def product(self, collection_id: str, resource: str | None = None) -> Product:
        if resource:
            raise SourceError("a collection is one layer; there are no resources to pick")
        c = self._json(f"{self.url}/collections/{collection_id}", collection_id)
        if not is_features(c):
            raise SourceError(f"collection {collection_id} holds {c.get('itemType') or 'something other than features'}, not features")
        self._collections[collection_id] = c
        p = self._product(c)
        download = Download(format=FORMAT, url=f"{self.url}/collections/{collection_id}/items", name=p.name, service=True)
        p.downloads, p.chosen = [download], download
        return p

    def layers(self, product: Product) -> list[remote.Layer]:
        c = self._collections.get(product.id) or self._json(f"{self.url}/collections/{product.id}", product.id)
        items = self._json(product.chosen.url, product.id, accept=GEOJSON, params={"limit": 1})
        features = items.get("features") or [] if isinstance(items, dict) else []
        geometry = ((features[0] or {}).get("geometry") or {}).get("type") if features else None
        matched = items.get("numberMatched") if isinstance(items, dict) else None
        return [remote.Layer(
            name=product.id,
            file="",
            geometry=geometry,
            # the CRS the data is kept in; GeoJSON responses default to CRS84
            crs=crs.short(c.get("storageCrs")) or "CRS84",
            features=matched if isinstance(matched, int) else None,
        )]

    def gdal_source(self, product: Product) -> tuple[str, str]:
        """(GDAL connection, layer) that loads this collection."""
        return f"OAPIF:{self.url}", product.id

    def download_url(self, download: Download) -> str:
        return download.url
