"""A small client for the GeoServer REST API: just what `plan` and `apply` use.

Plain dicts in and out, the JSON GeoServer itself speaks, so `plan` can
compare what's live with what the files say without a model layer between.
Each catalog object is one REST path; `upsert` creates or updates it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

import requests

from . import secrets
from .sources import http_session

TIMEOUT = 60

STYLE_TYPES = {  # a style file's suffix -> the content type GeoServer reads it as
    ".sld": "application/vnd.ogc.sld+xml",
    ".mbstyle": "application/vnd.geoserver.mbstyle+json",
    ".css": "application/vnd.geoserver.geocss+css",
}


# The spec's layer group modes, as GeoServer names them
GROUP_MODES = {"single": "SINGLE", "named": "NAMED", "container": "CONTAINER", "opaque": "OPAQUE_CONTAINER"}


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
    # GISDATA_GWC_URL: GeoWebCache's REST address, for a GeoServer Cloud REST service reached without its gateway
    return GeoServer(url or spec["url"], creds["username"], creds["password"], gwc_url=os.environ.get("GISDATA_GWC_URL"))


def q(name: str) -> str:
    """A name as one path segment."""
    return quote(name, safe="")


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
                           enabled: bool = True, advertised: bool = True) -> str:
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
        self.request("PUT", path, {"featureType": {"name": current["featureType"]["name"], "enabled": True}},
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
                          title: str | None = None, abstract: str | None = None, mode: str = "single") -> str:
        """`layers`: (kind "layer" or "layerGroup", qualified name, style or None), first drawn at the bottom."""
        base = f"workspaces/{q(ws)}/layergroups" if ws else "layergroups"
        group: dict = {
            "name": name, "mode": GROUP_MODES[mode.lower()], "title": title or name,
            "publishables": {"published": [{"@type": kind, "name": n} for kind, n, _ in layers]},
            "styles": {"style": [{"name": s} if s else "" for _, _, s in layers]},
        }
        if abstract is not None:
            group["abstractTxt"] = abstract
        if ws:
            group["workspace"] = {"name": ws}
        return self.upsert(f"{base}/{q(name)}", base, {"layerGroup": group})

    def delete_layergroup(self, name: str, ws: str | None = None) -> bool:
        base = f"workspaces/{q(ws)}/layergroups" if ws else "layergroups"
        return self.delete(f"{base}/{q(name)}")

    # --- tile cache (GeoWebCache). Its REST API reads JSON well but writes XML only.

    def gwc_request(self, method: str, path: str, body: str | None = None) -> requests.Response:
        url = f"{self.gwc}/{path}"
        headers = {"Accept": "application/json"}
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
        if "metaTiles" in options:
            extra += f"<metaWidthHeight><int>{int(options['metaTiles'][0])}</int><int>{int(options['metaTiles'][1])}</int></metaWidthHeight>"
        for key in ("gutter", "expireCache", "expireClients"):
            if key in options:
                extra += f"<{key}>{int(options[key])}</{key}>"
        xml = (f"<GeoServerLayer><name>{escape(name)}</name><enabled>true</enabled>{extra}"
               f"<mimeFormats>{''.join(f'<string>{escape(f)}</string>' for f in formats)}</mimeFormats>"
               f"<gridSubsets>{''.join(f'<gridSubset><gridSetName>{escape(g)}</gridSetName></gridSubset>' for g in gridsets)}"
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
        if r.status_code in (400, 404):
            return None
        raise GeoServerError("GET", f"gwc/gridsets/{name}", r.status_code, r.text)

    def put_gridset(self, name: str, epsg: int, extent: list[float], resolutions: list[float], tile_size: int,
                    align_top_left: bool, meters_per_unit: float) -> None:
        doubles = lambda values: "".join(f"<double>{v}</double>" for v in values)
        xml = (f"<gridSet><name>{escape(name)}</name><srs><number>{epsg}</number></srs>"
               f"<extent><coords>{doubles(extent)}</coords></extent><alignTopLeft>{str(align_top_left).lower()}</alignTopLeft>"
               f"<resolutions>{doubles(resolutions)}</resolutions><metersPerUnit>{meters_per_unit}</metersPerUnit>"
               f"<pixelSize>2.8E-4</pixelSize><tileHeight>{tile_size}</tileHeight><tileWidth>{tile_size}</tileWidth>"
               "<yCoordinateFirst>false</yCoordinateFirst></gridSet>")
        r = self.gwc_request("PUT", f"gridsets/{q(name)}.xml", xml)
        if r.status_code not in (200, 201):
            raise GeoServerError("PUT", f"gwc/gridsets/{name}", r.status_code, r.text)

    def truncate(self, name: str) -> None:
        """Throw away `name`'s cached tiles, so they are drawn again from the new data."""
        r = self.gwc_request("POST", "masstruncate", f"<truncateLayer><layerName>{escape(name)}</layerName></truncateLayer>")
        if r.status_code != 200:
            raise GeoServerError("POST", "gwc/masstruncate", r.status_code, r.text)
