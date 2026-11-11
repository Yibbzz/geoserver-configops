"""Gridset and BlobStore resources: GeoWebCache tile grids of your own (British National Grid),
and where tiles are kept (a folder on the data volume, or S3).

GeoWebCache has many gridsets of its own (EPSG:4326, EPSG:900913, UTM zones);
only the ones the files name are compared, and none is ever removed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from . import secrets
from .geoserver import GeoServerError, MissingCredentials




def wanted(spec: dict) -> dict:
    return {"crs": spec["crs"], "extent": [float(v) for v in spec["extent"]], "resolutions": [float(r) for r in spec["resolutions"]],
            "tileSize": spec.get("tileSize", 256), "alignTopLeft": spec.get("alignTopLeft", False),
            "metersPerUnit": float(spec.get("metersPerUnit", 1))}


def live(gridset: dict) -> dict:
    tile = gridset.get("tileWidth")
    return {"crs": f"EPSG:{(gridset.get('srs') or {}).get('number')}", "extent": [float(v) for v in (gridset.get("extent") or {}).get("coords", [])],
            "resolutions": [float(r) for r in gridset.get("resolutions") or []], "tileSize": tile,
            "alignTopLeft": gridset.get("alignTopLeft", False), "metersPerUnit": float(gridset.get("metersPerUnit") or 1)}


def plan(planner) -> None:
    plan_blob_stores(planner)
    gs = planner.gs
    for r in planner.files.of_kind("Gridset"):
        name = r.spec.get("name") or r.name
        want = wanted(r.spec)

        def run(name=name, want=want):
            gs.put_gridset(name, int(want["crs"].split(":", 1)[1]), want["extent"], want["resolutions"], want["tileSize"],
                           want["alignTopLeft"], want["metersPerUnit"])
        current = gs.gridset(name)
        if current is None:
            planner.add("+", "gridset", name, note=f"{want['crs']}, {len(want['resolutions'])} levels", run=run)
            continue
        have = live(current)
        changed = [(k, have[k], v) for k, v in want.items() if have[k] != v]
        if changed:
            planner.add("~", "gridset", name, changed, run=run)





# --- blob stores

DATA = "/data"
S3_FIELDS = {"bucket": "bucket", "prefix": "prefix", "endpoint": "endpoint", "region": "awsRegion", "access": "access",
             "maxConnections": "maxConnections", "gzip": "useGzip"}


def wanted_blob_store(r, root) -> tuple[str, dict]:
    """(GeoWebCache's XML, the fields compared) for a BlobStore resource."""
    spec, name = r.spec, r.spec.get("name") or r.name
    head = f'default="{str(spec.get("default", False)).lower()}"'
    fields = {"enabled": spec.get("enabled", True), "default": spec.get("default", False)}
    if spec["type"] == "file":
        fields["path"] = f"{DATA}/{spec['path'].strip('/')}"
        body = (f"<id>{escape(name)}</id><enabled>{str(fields['enabled']).lower()}</enabled>"
                f"<baseDirectory>{escape(fields['path'])}</baseDirectory><fileSystemBlockSize>{spec.get('blockSize', 4096)}</fileSystemBlockSize>")
        return f"<FileBlobStore {head}>{body}</FileBlobStore>", fields
    ref = (spec.get("credentials") or {}).get("secret")
    key, secret = (secrets.lookup(ref, "accessKey", root), secrets.lookup(ref, "secretKey", root)) if ref else (None, None)
    if ref and (key is None or secret is None):
        raise MissingCredentials(f"blob store {r.name}: secret {ref} needs keys accessKey and secretKey")
    values = {"bucket": spec["bucket"], "prefix": spec.get("prefix", ""), "endpoint": spec.get("endpoint", ""),
              "region": spec.get("region", ""), "access": spec.get("access", "private").upper().replace("-", "_"),
              "maxConnections": spec.get("maxConnections", 50), "gzip": spec.get("gzip", True)}
    fields.update({k: v for k, v in values.items() if v not in ("", None)})
    if key is not None:
        fields["accessKey"], fields["secretKey"] = key, secret
    body = f"<id>{escape(name)}</id><enabled>{str(fields['enabled']).lower()}</enabled>"
    for k, tag in S3_FIELDS.items():
        if values[k] not in ("", None):
            body += f"<{tag}>{escape(str(values[k]).lower() if isinstance(values[k], bool) else str(values[k]))}</{tag}>"
    if key is not None:
        body += f"<awsAccessKey>{escape(key)}</awsAccessKey><awsSecretKey>{escape(secret)}</awsSecretKey>"
    body += f"<useHTTPS>{str(not values['endpoint'].startswith('http://')).lower()}</useHTTPS>"
    return f"<S3BlobStore {head}>{body}</S3BlobStore>", fields


def live_blob_store(xml: str) -> dict:
    root = ET.fromstring(xml)
    text = lambda tag: (root.findtext(tag) or "")
    out = {"enabled": text("enabled") == "true", "default": root.get("default") == "true"}
    if root.tag == "FileBlobStore":
        out["path"] = text("baseDirectory")
        return out
    for k, tag in S3_FIELDS.items():
        value = text(tag)
        if value:
            out[k] = value == "true" if k == "gzip" else (int(value) if k == "maxConnections" else value)
    if text("awsAccessKey"):
        out["accessKey"], out["secretKey"] = text("awsAccessKey"), text("awsSecretKey")
    return out


def plan_blob_stores(planner) -> None:
    gs = planner.gs
    for r in planner.files.of_kind("BlobStore"):
        name = r.spec.get("name") or r.name
        xml, want = wanted_blob_store(r, planner.root)

        def run(name=name, xml=xml):
            response = gs.gwc_request("PUT", f"blobstores/{name}", xml)
            if response.status_code not in (200, 201):
                raise GeoServerError("PUT", f"gwc/blobstores/{name}", response.status_code, response.text)
        live = gs.gwc_request("GET", f"blobstores/{name}", accept="application/xml")  # no .xml: the REST service reads it as the name
        if live.status_code != 200:
            planner.add("+", "blobstore", name, note=f"{r.spec['type']}: {want.get('path') or want.get('bucket')}", run=run)
            continue
        have = live_blob_store(live.text)
        changed = [(k, "(differs)" if k == "secretKey" else have.get(k), "(from the Secret)" if k == "secretKey" else v)
                   for k, v in want.items() if have.get(k) != v]
        if changed:
            planner.add("~", "blobstore", name, changed, run=run)
