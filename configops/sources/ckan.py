"""CKAN portals (data.gov.uk and many city, regional and national portals).

A CKAN package is a product; its resources are the downloads. Packages
often hold several files, so a Dataset can name one with `resource`;
otherwise sync uses the best loadable format. Public data needs no key; a
key (CKAN API token) is sent only to the portal itself, never to the
servers its files live on.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse

import requests

from . import Download, Listing, NotFound, Product, SourceError, http_session
from .formats import FILE_LABELS, classify, rank, service
from .url import resolve

TIMEOUT = 30
DEFAULT_LIMIT = 20


def text(value) -> str:
    """CKAN text, which some portals give per language ({"en": ...})."""
    if isinstance(value, dict):
        return str(value.get("en") or next(iter(value.values()), ""))
    return str(value or "")


def action_base(url: str) -> str:
    """The API's action URL from a portal address; a URL already inside /api/ is used as given."""
    url = url.rstrip("/")
    return url if "/api/" in url + "/" else f"{url}/api/3/action"


def choose(downloads: list[Download]) -> Download | None:
    loadable = [d for d in downloads if d.loadable]
    return min(loadable, key=lambda d: rank(d.format), default=None)


class CKAN:
    name = "CKAN portal"
    formats = FILE_LABELS

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        if not url:
            raise SourceError("a ckan source needs the portal's address: spec.url in the Source, or --url")
        self.portal = url.rstrip("/")
        self.base = action_base(url)
        self.api_key = api_key
        self.session = session or http_session()

    def _auth(self, url: str) -> dict:
        same_host = urlparse(url).netloc == urlparse(self.base).netloc
        return {"Authorization": self.api_key} if self.api_key and same_host else {}

    def _action(self, action: str, params: dict, what: str):
        url = f"{self.base}/{action}"
        try:
            r = self.session.get(url, params=params, headers=self._auth(url), timeout=TIMEOUT)
        except requests.RequestException as e:
            raise SourceError(f"can't reach {self.portal}: {e}") from None
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code == 404 and (body or {}).get("error", {}).get("__type") == "Not Found Error":
            raise NotFound(what)
        if r.status_code in (401, 403):
            raise SourceError(f"{self.portal} refused the request (HTTP {r.status_code}); check the API key")
        if not isinstance(body, dict):
            hint = "; is it a CKAN portal?" if r.status_code in (200, 404) else ""
            raise SourceError(f"{self.portal} answered HTTP {r.status_code} without CKAN JSON at {url}{hint}")
        if not body.get("success"):
            error = body.get("error") or {}
            if error.get("__type") == "Not Found Error":
                raise NotFound(what)
            raise SourceError(f"{self.portal}: {error.get('message') or error.get('__type') or 'request failed'}")
        return body["result"]

    def _download(self, r: dict) -> Download:
        url = r.get("url") or ""
        label = classify(r.get("format"), url, r.get("mimetype"), text(r.get("name")))
        raw = text(r.get("format")).strip()
        size = r.get("size")
        return Download(
            format=label or service(raw, url, text(r.get("name"))) or raw.removeprefix("https://www.iana.org/assignments/media-types/") or "unknown",
            url=url,
            file_name=unquote(urlparse(url).path.rsplit("/", 1)[-1]),
            size=size if isinstance(size, int) else None,
            md5=r.get("hash") or None,
            id=r.get("id") or "",
            name=text(r.get("name")).strip(),
            loadable=label is not None and url.startswith(("http://", "https://")),
        )

    def _product(self, pkg: dict) -> Product:
        downloads = [self._download(r) for r in pkg.get("resources") or []]
        chosen = choose(downloads)
        formats = list(dict.fromkeys(d.format for d in downloads))
        return Product(
            id=pkg.get("name") or pkg["id"],
            name=text(pkg.get("title")) or pkg.get("name") or pkg["id"],
            version=pkg.get("metadata_modified"),
            formats=formats,
            downloads=downloads,
            loads_as=chosen.format if chosen else None,
            chosen=chosen,
            description=text(pkg.get("notes")),
        )

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        params = {"q": search or "", "rows": limit or DEFAULT_LIMIT}
        if not search:
            params["sort"] = "metadata_modified desc"
        result = self._action("package_search", params, "search")
        return Listing([self._product(p) for p in result.get("results", [])], result.get("count"))

    def product(self, package: str, resource: str | None = None) -> Product:
        product = self._product(self._action("package_show", {"id": package}, package))
        if resource:
            wanted = resource.lower()
            picked = [d for d in product.downloads if wanted in (d.id.lower(), d.name.lower())]
            if not picked:
                names = ", ".join(d.name or d.id for d in product.downloads)
                raise SourceError(f"{product.id} has no resource '{resource}' (its resources: {names})")
            if not picked[0].loadable:
                raise SourceError(f"resource '{resource}' is {picked[0].format}, not a file configops can load")
            product.chosen = picked[0]
            product.loads_as = picked[0].format
        return product

    def download_url(self, download: Download) -> str:
        return resolve(self.session, download.url, self._auth(download.url))
