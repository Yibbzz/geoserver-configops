"""A small client for the GeoServer REST API: just what `plan` and `apply` use.

Plain dicts in and out, the JSON GeoServer itself speaks, so `plan` can
compare what's live with what the files say without a model layer between.
Each catalog object is one REST path; `upsert` creates or updates it.
"""

from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import secrets

TIMEOUT = 60

STYLE_TYPES = {  # a style file's suffix -> the content type GeoServer reads it as
    ".sld": "application/vnd.ogc.sld+xml",
    ".mbstyle": "application/vnd.geoserver.mbstyle+json",
    ".css": "application/vnd.geoserver.geocss+css",
}


def http_session() -> requests.Session:
    """A session that tries again, twice, when a server drops the connection or is briefly unavailable."""
    session = requests.Session()
    retry = Retry(total=2, connect=2, read=2, status=2, backoff_factor=0.5,
                  status_forcelist=(502, 503, 504), allowed_methods=("GET", "HEAD"), raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# The spec's layer group modes, as GeoServer names them
GROUP_MODES = {"single": "SINGLE", "named": "NAMED", "container": "CONTAINER", "opaque": "OPAQUE_CONTAINER", "eo": "EO"}


class GeoServerError(Exception):
    """GeoServer refused or failed a request; the message says which and why."""

    def __init__(self, method: str, path: str, status: int, text: str):
        detail = " ".join(text.split())[:300] or "no details"
        super().__init__(f"GeoServer answered {method} {path} with HTTP {status}: {detail}")
        self.status = status


class MissingCredentials(Exception):
    """The Project's GeoServer secret isn't set; the message says how to set it."""


def for_project(project, root: Path, url: str | None = None) -> "GeoServer":
    """The client for a Project resource: its address (or `url`) and its credentials secret."""
    spec = project.spec["geoserver"]
    secret = spec["credentials"]["secret"]
    creds = {key: secrets.lookup(secret, key, root) for key in ("username", "password")}
    missing = [k for k, v in creds.items() if v is None]
    if missing:
        names = ", ".join(f"${secrets.env_name(secret, k)}" for k in missing)
        raise MissingCredentials(f"the GeoServer secret {secret} is missing {', '.join(missing)}: set {names}, or add them to secrets.yaml")
    # CONFIGOPS_GWC_URL: GeoWebCache's REST address, for a GeoServer Cloud REST service reached without its gateway
    return GeoServer(url or spec["url"], creds["username"], creds["password"], gwc_url=os.environ.get("CONFIGOPS_GWC_URL"))


def q(name: str) -> str:
    """A name as one path segment."""
    return quote(name, safe="")


FILTER_TYPES = {"styleParameterFilter": "styles", "stringParameterFilter": "values", "regexParameterFilter": "regex",
                "floatParameterFilter": "numbers", "integerParameterFilter": "integers"}


def filter_xml(key: str, f: dict) -> str:
    """One parameter filter, as GeoWebCache's XML."""
    tag = next(t for t, kind in FILTER_TYPES.items() if kind == f["type"])
    item = {"numbers": "float", "integers": "int"}.get(f["type"], "string")
    out = f"<key>{escape(key)}</key><defaultValue>{escape(str(f.get('default', '')))}</defaultValue>"
    if f.get("values"):
        values = "".join(f"<{item}>{escape(str(v))}</{item}>" for v in f["values"])
        out += f"<allowedStyles>{values}</allowedStyles>" if f["type"] == "styles" else f"<values>{values}</values>"
    if f["type"] == "regex":
        out += f"<regex>{escape(f.get('regex', '.*'))}</regex>"
    if "threshold" in f:
        out += f"<threshold>{f['threshold']}</threshold>"
    if "case" in f:
        locale = f"<locale>{escape(f['locale'])}</locale>" if f.get("locale") else ""
        out += f"<normalize><case>{f['case'].upper()}</case>{locale}</normalize>"
    return f"<{tag}>{out}</{tag}>"


def subset_xml(gridset: str, levels: dict) -> str:
    """One gridset of a tile layer: its bounds, published zoom levels and cached ones, where set."""
    out = f"<gridSetName>{escape(gridset)}</gridSetName>"
    if "bounds" in levels:
        out += f"<extent><coords>{''.join(f'<double>{float(v)}</double>' for v in levels['bounds'])}</coords></extent>"
    for key, (low, high) in (("published", ("zoomStart", "zoomStop")), ("cached", ("minCachedLevel", "maxCachedLevel"))):
        if key in levels:
            out += f"<{low}>{int(levels[key][0])}</{low}><{high}>{int(levels[key][1])}</{high}>"
    return f"<gridSubset>{out}</gridSubset>"


def tile_extras(xml: str) -> dict:
    """A tile layer's zoom limits per gridset, expiry by zoom level and warning skips, in the files' form."""
    root = ET.fromstring(xml)
    levels = {}
    for g in root.findall("gridSubsets/gridSubset"):
        entry: dict = {}
        coords = [float(d.text) for d in g.findall("extent/coords/double")]
        if coords:
            entry["bounds"] = coords
        for key, (low, high) in (("published", ("zoomStart", "zoomStop")), ("cached", ("minCachedLevel", "maxCachedLevel"))):
            if g.findtext(low) is not None and g.findtext(high) is not None:
                entry[key] = [int(g.findtext(low)), int(g.findtext(high))]
        if entry:
            levels[g.findtext("gridSetName")] = entry
    expire = {int(r.get("minZoom")): int(r.get("expiration")) for r in root.findall("expireCacheList/expirationRule")}
    skips = sorted(w.text for w in root.findall("cacheWarningSkips/warning") if w.text)
    return {k: v for k, v in (("levels", levels), ("expireByZoom", expire), ("warningSkips", skips)) if v}


def tile_name(name: str) -> str:
    """A ws:name for GeoWebCache's paths, which need the colon as it is."""
    return quote(name, safe=":")


class GeoServer:
    def __init__(self, url: str, username: str, password: str, session: requests.Session | None = None,
                 gwc_url: str | None = None):
        self.url = url.rstrip("/")
        self.rest = f"{self.url}/rest"
        # GeoWebCache's REST API: <url>/gwc/rest through GeoServer Cloud's gateway
        self.gwc = (gwc_url or f"{self.url}/gwc/rest").rstrip("/")
        self.session = session or http_session()
        self.session.auth = (username, password)

    # --- generic

    def request(self, method: str, path: str, body=None, content_type: str = "application/json",
                params: dict | None = None, ok=(200, 201)) -> requests.Response:
        url = f"{self.rest}/{path}"
        kwargs: dict = {"params": params, "timeout": TIMEOUT, "headers": {"Accept": "application/json"}}
        if body is not None:
            kwargs["headers"]["Content-Type"] = content_type
            kwargs["json" if isinstance(body, (dict, list)) else "data"] = body
        try:
            r = self.session.request(method, url, **kwargs)
        except requests.RequestException as e:
            raise GeoServerError(method, path, 0, f"can't reach {self.url}: {e}") from None
        if r.status_code not in ok:
            raise GeoServerError(method, path, r.status_code, r.text)
        return r

    def get(self, path: str, params: dict | None = None) -> dict | None:
        """The object at `path`, or None if there is none."""
        r = self.request("GET", f"{path}.json", params=params, ok=(200, 404))
        return None if r.status_code == 404 else r.json()

    def exists(self, path: str) -> bool:
        return self.request("GET", f"{path}.json", ok=(200, 404)).status_code == 200

    def upsert(self, path: str, collection: str, body, content_type: str = "application/json",
               params: dict | None = None) -> str:
        """
        Update `path` if it exists, else create it in `collection`; returns
        "updated" or "created". GeoServer Cloud answers a PUT on a missing
        object with an error, hence the look first.
        """
        if self.exists(path):
            self.request("PUT", path, body, content_type, params)
            return "updated"
        self.request("POST", collection, body, content_type, params)
        return "created"

    def delete(self, path: str, params: dict | None = None) -> bool:
        """Delete `path`; False if it was already gone."""
        return self.request("DELETE", path, params=params, ok=(200, 404)).status_code == 200

    def names(self, path: str, key: str) -> list[str]:
        """Names in a collection, e.g. names("workspaces", "workspace")."""
        data = self.get(path) or {}
        outer = data.get(path.rsplit("/", 1)[-1]) or data.get(f"{key}s") or {}
        items = outer.get(key, []) if isinstance(outer, dict) else []
        return [i["name"] for i in (items if isinstance(items, list) else [items])]

    # --- files GeoServer keeps beside its catalog (style images, fonts, templates)

    def read(self, path: str) -> bytes | None:
        """A file as GeoServer serves it (resource/..., .../templates/x.ftl), or None if there is none."""
        r = self.request("GET", path, ok=(200, 404))
        return None if r.status_code == 404 else r.content

    def files(self, folder: str) -> list[str]:
        """Every file under one of GeoServer's resource folders, as resource paths (folder/...)."""
        r = self.request("GET", f"resource/{folder}", params={"format": "json"}, ok=(200, 404))
        if r.status_code == 404:
            return []
        children = ((r.json().get("ResourceDirectory") or {}).get("children") or {}).get("child")
        out = []
        for child in children if isinstance(children, list) else [children] if children else []:
            path = f"{folder}/{child['name']}"
            if (child.get("link") or {}).get("type") == "application/json" and not child["name"].endswith(".json"):
                out += self.files(path)  # a folder
            else:
                out.append(path)
        return out

    def wait(self, seconds: float, every: float = 5) -> None:
        """
        Wait up to `seconds` for GeoServer's REST API, and GeoWebCache's, to
        answer. After a fresh install they start one by one: in GeoServer
        Cloud the tile cache is a service of its own.
        """
        deadline = time.monotonic() + seconds
        while True:
            try:
                self.request("GET", "about/version.json")
                r = self.gwc_request("GET", "layers")
                if r.status_code != 200:
                    raise GeoServerError("GET", "gwc/layers", r.status_code, r.text)
                return
            except GeoServerError as e:
                if e.status in (401, 403) or time.monotonic() + every > deadline:
                    raise
            time.sleep(every)

    # --- workspaces

    def workspace(self, name: str) -> dict | None:
        return self.get(f"workspaces/{q(name)}")

    def upsert_workspace(self, name: str) -> str:
        return self.upsert(f"workspaces/{q(name)}", "workspaces", {"workspace": {"name": name}})

    def delete_workspace(self, name: str) -> bool:
        return self.delete(f"workspaces/{q(name)}", {"recurse": "true"})

    # --- stores

    def datastore(self, ws: str, name: str) -> dict | None:
        return self.get(f"workspaces/{q(ws)}/datastores/{q(name)}")

    def upsert_jndi_datastore(self, ws: str, name: str, jndi: str, schema: str) -> str:
        """A PostGIS store that reaches the database through GeoServer's JNDI datasource: no password in the catalog."""
        entries = {"dbtype": "postgis", "jndiReferenceName": jndi, "schema": schema, "Expose primary keys": "true"}
        body = {"dataStore": {
            "name": name, "type": "PostGIS (JNDI)", "enabled": True,
            "connectionParameters": {"entry": [{"@key": k, "$": v} for k, v in entries.items()]},
        }}
        return self.upsert(f"workspaces/{q(ws)}/datastores/{q(name)}", f"workspaces/{q(ws)}/datastores", body)

    def delete_datastore(self, ws: str, name: str) -> bool:
        return self.delete(f"workspaces/{q(ws)}/datastores/{q(name)}", {"recurse": "true"})

    # --- feature types (a table published from a store)

    def featuretype(self, ws: str, store: str, name: str) -> dict | None:
        return self.get(f"workspaces/{q(ws)}/datastores/{q(store)}/featuretypes/{q(name)}")

    def upsert_featuretype(self, ws: str, store: str, name: str, table: str, title: str | None = None,
                           abstract: str | None = None, keywords: list[str] | None = None, srs: str | None = None,
                           enabled: bool = True, advertised: bool = True, extra: dict | None = None) -> str:
        """
        Publish `table` as `name`. GeoServer works out the attributes, and the
        bounding boxes are recalculated from the data each time.
        """
        ft: dict = {"name": name, "nativeName": table, "title": title or name, "enabled": enabled, "advertised": advertised}
        if abstract is not None:
            ft["abstract"] = abstract
        if keywords is not None:
            ft["keywords"] = {"string": keywords}
        if srs:
            ft["srs"] = srs
            ft["projectionPolicy"] = "FORCE_DECLARED"
        ft.update(extra or {})
        path = f"workspaces/{q(ws)}/datastores/{q(store)}/featuretypes/{q(name)}"
        result = self.upsert(path, f"workspaces/{q(ws)}/datastores/{q(store)}/featuretypes", {"featureType": ft})
        self.recalculate(ws, store, name)
        return result

    def recalculate(self, ws: str, store: str, name: str) -> None:
        """Work the bounding boxes out again from the table, after new data is loaded."""
        path = f"workspaces/{q(ws)}/datastores/{q(store)}/featuretypes/{q(name)}"
        current = self.get(path)
        if current is None:
            return
        # `enabled` as it is: a layer the files turn off stays off
        self.request("PUT", path, {"featureType": {"name": current["featureType"]["name"],
                                                   "enabled": current["featureType"].get("enabled", True)}},
                     params={"recalculate": "nativebbox,latlonbbox"})

    def delete_featuretype(self, ws: str, store: str, name: str) -> bool:
        return self.delete(f"workspaces/{q(ws)}/datastores/{q(store)}/featuretypes/{q(name)}", {"recurse": "true"})

    # --- layers (how a feature type is drawn and offered)

    def layer(self, ws: str, name: str) -> dict | None:
        return self.get(f"workspaces/{q(ws)}/layers/{q(name)}")

    def update_layer(self, ws: str, name: str, style: str | None = None, styles: list[str] | None = None,
                     queryable: bool | None = None, enabled: bool | None = None, attribution: str | None = None) -> None:
        """Set a layer's styles and settings; anything left None is left as it is."""
        layer: dict = {}
        if attribution is not None:
            layer["attribution"] = {"title": attribution}
        if style is not None:
            layer["defaultStyle"] = {"name": style}
        if styles is not None:
            layer["styles"] = {"style": [{"name": s} for s in styles]}
        if queryable is not None:
            layer["queryable"] = queryable
        if enabled is not None:
            layer["enabled"] = enabled
        if layer:
            self.request("PUT", f"workspaces/{q(ws)}/layers/{q(name)}", {"layer": layer})

    # --- styles

    def style_path(self, name: str, ws: str | None = None) -> tuple[str, str]:
        base = f"workspaces/{q(ws)}/styles" if ws else "styles"
        return f"{base}/{q(name)}", base

    def style(self, name: str, ws: str | None = None) -> dict | None:
        return self.get(self.style_path(name, ws)[0])

    def style_body(self, name: str, ws: str | None = None) -> str | None:
        """The style's own file (SLD, MBStyle or CSS), as GeoServer holds it."""
        info = self.style(name, ws)
        if info is None:
            return None
        path = self.style_path(name, ws)[0]
        suffix = {"sld": "sld", "mbstyle": "mbstyle", "css": "css"}.get(info["style"].get("format", "sld"), "sld")
        return self.request("GET", f"{path}.{suffix}").text

    def upsert_style(self, name: str, body: str, suffix: str, ws: str | None = None) -> str:
        """Create or replace a style from its file's text; `suffix` (.sld, .mbstyle, .css) says which language."""
        content_type = STYLE_TYPES[suffix]
        path, collection = self.style_path(name, ws)
        data = body.encode("utf-8")
        if self.exists(path):
            self.request("PUT", path, data, content_type, params={"raw": "true"})
            return "updated"
        # GeoServer creates a style from its file, posted with the name it should have
        self.request("POST", collection, data, content_type, params={"name": name, "raw": "true"})
        return "created"

    def delete_style(self, name: str, ws: str | None = None) -> bool:
        return self.delete(self.style_path(name, ws)[0], {"purge": "true", "recurse": "true"})

    # --- layer groups

    def layergroup(self, name: str, ws: str | None = None) -> dict | None:
        base = f"workspaces/{q(ws)}/layergroups" if ws else "layergroups"
        return self.get(f"{base}/{q(name)}")

    def upsert_layergroup(self, name: str, layers: list[tuple[str, str, str | None]], ws: str | None = None,
                          title: str | None = None, abstract: str | None = None, mode: str = "single",
                          root: tuple[str, str | None] | None = None, keywords: list[str] | None = None) -> str:
        """`layers`: (kind "layer" or "layerGroup", qualified name, style or None), first drawn at the bottom."""
        base = f"workspaces/{q(ws)}/layergroups" if ws else "layergroups"
        group: dict = {
            "name": name, "mode": GROUP_MODES[mode.lower()], "title": title or name,
            "publishables": {"published": [{"@type": kind, "name": n} for kind, n, _ in layers]},
            "styles": {"style": [{"name": s} if s else "" for _, _, s in layers]},
        }
        if abstract is not None:
            group["abstractTxt"] = abstract
        if keywords is not None:
            group["keywords"] = {"string": keywords}
        if root:  # an EO group's root layer (qualified name) and its style
            group["rootLayer"] = {"name": root[0]}
            group["rootLayerStyle"] = {"name": root[1]} if root[1] else None
        if ws:
            group["workspace"] = {"name": ws}
        return self.upsert(f"{base}/{q(name)}", base, {"layerGroup": group})

    def delete_layergroup(self, name: str, ws: str | None = None) -> bool:
        base = f"workspaces/{q(ws)}/layergroups" if ws else "layergroups"
        return self.delete(f"{base}/{q(name)}")

    # --- tile cache (GeoWebCache). Its REST API reads JSON well but writes XML only.

    def gwc_request(self, method: str, path: str, body: str | None = None, accept: str = "application/json") -> requests.Response:
        url = f"{self.gwc}/{path}"
        headers = {"Accept": accept}
        if body is not None:
            headers["Content-Type"] = "text/xml"
        try:
            return self.session.request(method, url, data=body.encode("utf-8") if body else None,
                                        headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise GeoServerError(method, f"gwc/{path}", 0, f"can't reach {self.gwc}: {e}") from None

    def tile_layer(self, name: str) -> dict | None:
        """A layer's or group's tile cache settings (name as ws:name), or None if it isn't cached."""
        r = self.gwc_request("GET", f"layers/{tile_name(name)}")
        if r.status_code == 200:
            return r.json().get("GeoServerLayer")
        if r.status_code in (404, 500) and "Unknown layer" in r.text:
            return None
        raise GeoServerError("GET", f"gwc/layers/{name}", r.status_code, r.text)

    def put_tile_layer(self, name: str, gridsets: list[str], formats: list[str], options: dict | None = None) -> None:
        """
        Cache `name` in these gridsets and image formats (creating or replacing its settings).
        `options`: metaTiles [w, h], gutter (pixels), expireCache and expireClients (seconds).
        """
        options = options or {}
        extra = ""
        # Always sent: standard GeoServer stores 0 x 0 when it's left out (GeoServer Cloud fills in the default), and seeding
        # then divides by it
        meta = options.get("metaTiles") or self.default_meta_tiles()
        extra += f"<metaWidthHeight><int>{int(meta[0])}</int><int>{int(meta[1])}</int></metaWidthHeight>"
        for key in ("gutter", "expireCache", "expireClients"):
            if key in options:
                extra += f"<{key}>{int(options[key])}</{key}>"
        if options.get("blobStore"):
            extra += f"<blobStoreId>{escape(options['blobStore'])}</blobStoreId>"
        if options.get("parameterFilters"):
            extra += f"<parameterFilters>{''.join(filter_xml(k, f) for k, f in options['parameterFilters'].items())}</parameterFilters>"
        if "expireByZoom" in options:
            rules = "".join(f'<expirationRule minZoom="{int(z)}" expiration="{int(s)}"/>' for z, s in sorted(options["expireByZoom"].items()))
            extra += f"<expireCacheList>{rules}</expireCacheList>"
        if "warningSkips" in options:
            extra += f"<cacheWarningSkips>{''.join(f'<warning>{escape(w)}</warning>' for w in options['warningSkips'])}</cacheWarningSkips>"
        levels = options.get("levels") or {}
        xml = (f"<GeoServerLayer><name>{escape(name)}</name><enabled>true</enabled>{extra}"
               f"<mimeFormats>{''.join(f'<string>{escape(f)}</string>' for f in formats)}</mimeFormats>"
               f"<gridSubsets>{''.join(subset_xml(g, levels.get(g) or {}) for g in gridsets)}"
               "</gridSubsets></GeoServerLayer>")
        r = self.gwc_request("PUT", f"layers/{tile_name(name)}", xml)
        if r.status_code != 200:
            raise GeoServerError("PUT", f"gwc/layers/{name}", r.status_code, r.text)

    def delete_tile_layer(self, name: str) -> bool:
        """Stop caching `name` and drop its tiles; False if it wasn't cached."""
        r = self.gwc_request("DELETE", f"layers/{tile_name(name)}")
        if r.status_code == 200:
            return True
        if "Unknown layer" in r.text:
            return False
        raise GeoServerError("DELETE", f"gwc/layers/{name}", r.status_code, r.text)

    def gridset(self, name: str) -> dict | None:
        r = self.gwc_request("GET", f"gridsets/{q(name)}")
        if r.status_code == 200:
            return r.json().get("gridSet")
        if r.status_code in (400, 404) or "does not exist" in r.text:  # GeoServer Cloud's REST service answers 500
            return None
        raise GeoServerError("GET", f"gwc/gridsets/{name}", r.status_code, r.text)

    def put_gridset(self, name: str, epsg: int, extent: list[float], resolutions: list[float], tile_size: int,
                    align_top_left: bool, meters_per_unit: float, extra: dict | None = None) -> None:
        """`extra`: description, scaleNames, pixelSize, yCoordinateFirst, scaleDenominators (instead of resolutions)."""
        extra = extra or {}
        doubles = lambda values: "".join(f"<double>{v}</double>" for v in values)
        description = f"<description>{escape(extra['description'])}</description>" if extra.get("description") else ""
        names = "".join(f"<string>{escape(n)}</string>" for n in extra.get("scaleNames") or [])
        xml = (f"<gridSet><name>{escape(name)}</name>{description}<srs><number>{epsg}</number></srs>"
               f"<extent><coords>{doubles(extent)}</coords></extent><alignTopLeft>{str(align_top_left).lower()}</alignTopLeft>"
               + (f"<scaleDenominators>{doubles(extra['scaleDenominators'])}</scaleDenominators>" if extra.get("scaleDenominators")
                  else f"<resolutions>{doubles(resolutions)}</resolutions>") +
               f"<metersPerUnit>{meters_per_unit}</metersPerUnit>"
               f"<pixelSize>{extra.get('pixelSize', 2.8e-4)}</pixelSize>" + (f"<scaleNames>{names}</scaleNames>" if names else "") +
               f"<tileHeight>{tile_size}</tileHeight><tileWidth>{tile_size}</tileWidth>"
               f"<yCoordinateFirst>{str(extra.get('yCoordinateFirst', False)).lower()}</yCoordinateFirst></gridSet>")
        r = self.gwc_request("PUT", f"gridsets/{q(name)}.xml", xml)
        if r.status_code not in (200, 201):
            raise GeoServerError("PUT", f"gwc/gridsets/{name}", r.status_code, r.text)

    @property
    def version(self) -> str:
        """GeoServer's version, as /rest/about/version says (`3.0.2`, `3.0.2-CLOUD`, `2.28.6`); read once.
        A server that won't say (security rules can refuse /rest/about) gives "" and counts as the newest."""
        if getattr(self, "_version", None) is None:
            r = self.request("GET", "about/version.json", ok=(200, 401, 403, 404))
            about = (r.json() if r.status_code == 200 else {}).get("about") or {}
            resources = about.get("resource") or []
            self._version = next((str(r.get("Version", "")) for r in (resources if isinstance(resources, list) else [resources])
                                  if isinstance(r, dict) and r.get("@name") == "GeoServer"), "")
        return self._version

    @property
    def cloud(self) -> bool:
        """Whether this is GeoServer Cloud (its version ends in -CLOUD) rather than standard GeoServer."""
        return "CLOUD" in self.version.upper()

    @property
    def series(self) -> tuple[int, ...] | None:
        """(major, minor) of GeoServer's version, or None if it won't say."""
        numbers = re.findall(r"\d+", self.version)
        return tuple(int(n) for n in numbers[:2]) if len(numbers) >= 2 else None

    def default_meta_tiles(self) -> list[int]:
        """The caching defaults' metatiling (gwc-gs.xml), read once; 4 x 4 if it can't be read."""
        if getattr(self, "_meta_tiles", None) is None:
            r = self.request("GET", "resource/gwc-gs.xml", ok=(200, 404))
            try:
                root = ET.fromstring(r.text) if r.status_code == 200 else None
                self._meta_tiles = [int(root.findtext("metaTilingX")), int(root.findtext("metaTilingY"))] if root is not None else [4, 4]
            except (ET.ParseError, TypeError, ValueError):
                self._meta_tiles = [4, 4]
        return self._meta_tiles

    def tile_layer_extras(self, name: str) -> dict:
        """A tile layer's zoom limits, expiry by zoom and warning skips (read as XML, like its filters)."""
        r = self.gwc_request("GET", f"layers/{tile_name(name)}", accept="application/xml")
        return tile_extras(r.text) if r.status_code == 200 else {}

    def parameter_filters(self, name: str) -> dict[str, dict]:
        """A tile layer's parameter filters, by parameter, in the files' form (read as XML: its JSON leaves out their types)."""
        r = self.gwc_request("GET", f"layers/{tile_name(name)}", accept="application/xml")
        if r.status_code != 200:
            return {}
        out = {}
        for f in ET.fromstring(r.text).findall("parameterFilters/*"):
            kind = FILTER_TYPES.get(f.tag)
            if kind is None:
                continue
            entry: dict = {"type": kind}
            values = [v.text or "" for v in f.findall("values/*") + f.findall("allowedStyles/*")]
            if values:
                entry["values"] = [float(v) if kind == "numbers" else int(v) if kind == "integers" else v for v in values]
            if f.findtext("regex") is not None:
                entry["regex"] = f.findtext("regex")
            if f.findtext("defaultValue"):
                entry["default"] = f.findtext("defaultValue")
            if f.findtext("threshold") is not None:
                threshold = float(f.findtext("threshold"))
                entry["threshold"] = int(threshold) if kind == "integers" else threshold
            case = f.findtext("normalize/case")
            if case and case != "NONE":
                entry["case"] = case.lower()
            if f.findtext("normalize/locale"):
                entry["locale"] = f.findtext("normalize/locale")
            out[f.findtext("key")] = entry
        return out

    def seed(self, name: str, gridset: str, fmt: str, zoom: list[int], bounds: list[float] | None = None, threads: int = 1) -> None:
        """Start drawing `name`'s tiles for these zoom levels (GeoWebCache does it in the background)."""
        box = ("<bounds><coords>" + "".join(f"<double>{v}</double>" for v in bounds) + "</coords></bounds>") if bounds else ""
        xml = (f"<seedRequest><name>{escape(name)}</name>{box}<gridSetId>{escape(gridset)}</gridSetId>"
               f"<zoomStart>{int(zoom[0])}</zoomStart><zoomStop>{int(zoom[1])}</zoomStop><format>{escape(fmt)}</format>"
               f"<type>seed</type><threadCount>{int(threads)}</threadCount></seedRequest>")
        r = self.gwc_request("POST", f"seed/{tile_name(name)}.xml", xml)
        if r.status_code != 200:
            raise GeoServerError("POST", f"gwc/seed/{name}", r.status_code, r.text)

    def truncate(self, name: str) -> None:
        """Throw away `name`'s cached tiles, so they are drawn again from the new data."""
        r = self.gwc_request("POST", "masstruncate", f"<truncateLayer><layerName>{escape(name)}</layerName></truncateLayer>")
        if r.status_code != 200:
            raise GeoServerError("POST", "gwc/masstruncate", r.status_code, r.text)
