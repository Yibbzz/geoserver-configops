"""Plain file links: no product list, the product is the link itself."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import requests

from . import Download, Listing, NotFound, Product, SourceError, http_session
from .formats import FILE_LABELS, classify

TIMEOUT = 30


@dataclass
class Probe:
    """What one small request says about a link."""

    final_url: str  # after redirects
    size: int | None
    version: str | None  # ETag or Last-Modified: changes when the file does
    file_name: str
    content_type: str


def probe(session: requests.Session, url: str, headers: dict | None = None) -> Probe:
    """Ask for the first byte only, so a large file isn't pulled down."""
    try:
        r = session.get(url, headers={**(headers or {}), "Range": "bytes=0-0"}, stream=True, timeout=TIMEOUT, allow_redirects=True)
        r.close()
    except requests.RequestException as e:
        raise SourceError(f"can't reach {url}: {e}") from None
    if r.status_code == 404:
        raise NotFound(url)
    if r.status_code in (401, 403):
        raise SourceError(f"{url} refused the request (HTTP {r.status_code})")
    if r.status_code not in (200, 206):
        raise SourceError(f"the file link answered HTTP {r.status_code}; it may be broken at the source: {url}")
    size = None
    if "Content-Range" in r.headers:
        total = r.headers["Content-Range"].rsplit("/", 1)[-1]
        size = int(total) if total.isdigit() else None
    elif r.status_code == 200 and r.headers.get("Content-Length", "").isdigit():
        size = int(r.headers["Content-Length"])
    disposition = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', r.headers.get("Content-Disposition", ""))
    file_name = unquote(disposition.group(1)) if disposition else unquote(urlparse(r.url).path.rsplit("/", 1)[-1])
    return Probe(
        final_url=r.url,
        size=size,
        version=(r.headers.get("ETag") or "").removeprefix("W/").strip('"') or r.headers.get("Last-Modified"),
        file_name=file_name,
        content_type=r.headers.get("Content-Type", "").split(";")[0].strip().lower(),
    )


def resolve(session: requests.Session, url: str, headers: dict | None = None) -> str:
    """Direct link to the file, after redirects (signed links can expire, so this is asked fresh)."""
    return probe(session, url, headers).final_url


class URL:
    name = "url source"
    formats = FILE_LABELS

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        self.session = session or http_session()

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        raise SourceError("a url source has no list of products; give the file's address as the product, e.g. https://example.org/data.zip")

    def product(self, link: str, resource: str | None = None) -> Product:
        if not link.startswith(("http://", "https://")):
            raise SourceError(f"'{link}' isn't a link; give the file's full https:// address")
        if resource:
            raise SourceError("a link is one file; there are no resources to pick")
        p = probe(self.session, link)
        fmt = classify(None, p.final_url, p.content_type) or classify(None, p.file_name)
        web_page = fmt is None and p.content_type in ("text/html", "application/xhtml+xml")
        download = Download(
            format=fmt or (p.content_type or "unknown"),
            url=link,
            file_name=p.file_name,
            size=p.size,
            loadable=not web_page,
        )
        return Product(
            id=link,
            name=p.file_name or link,
            version=p.version,
            formats=[download.format],
            downloads=[download],
            loads_as=download.format if download.loadable else None,
            chosen=download if download.loadable else None,
        )

    def download_url(self, download: Download) -> str:
        return resolve(self.session, download.url)
