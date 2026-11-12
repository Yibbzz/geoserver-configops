"""Gridset and BlobStore resources: GeoWebCache tile grids of your own (British National Grid),
and where tiles are kept (a folder on the data volume, S3, Azure or Google Cloud Storage).

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
# spec name -> GeoWebCache's, for each cloud type; what the files don't set is left out
FIELDS = {
    "s3": {"bucket": "bucket", "prefix": "prefix", "endpoint": "endpoint", "access": "access",
           "maxConnections": "maxConnections", "gzip": "useGzip"},
    "azure": {"container": "container", "prefix": "prefix", "endpoint": "serviceURL", "maxConnections": "maxConnections"},
    "gcs": {"bucket": "bucket", "prefix": "prefix", "project": "projectId", "endpoint": "endpointUrl",
            "defaultCredentials": "useDefaultCredentialsChain"},
}
TAGS = {"file": "FileBlobStore", "s3": "S3BlobStore", "azure": "AzureBlobStore", "gcs": "GoogleCloudStorageBlobStore"}
KEYS = {"s3": {"accessKey": "awsAccessKey", "secretKey": "awsSecretKey"},  # the Secret's keys -> GeoWebCache's
        "azure": {"accountName": "accountName", "accountKey": "accountKey"}, "gcs": {"apiKey": "apiKey"}}
SECRET_KEYS = ("secretKey", "accountKey", "apiKey")  # never shown


def xml_value(value) -> str:
    return escape(str(value).lower() if isinstance(value, bool) else str(value))


def wanted_blob_store(r, root) -> tuple[str, dict]:
    """(GeoWebCache's XML, the fields compared) for a BlobStore resource."""
    spec, name, kind = r.spec, r.spec.get("name") or r.name, r.spec["type"]
    head = f'default="{str(spec.get("default", False)).lower()}"'
    fields = {"enabled": spec.get("enabled", True), "default": spec.get("default", False)}
    body = f"<id>{escape(name)}</id><enabled>{str(fields['enabled']).lower()}</enabled>"
    if kind == "file":
        fields["path"] = f"{DATA}/{spec['path'].strip('/')}"
        body += f"<baseDirectory>{escape(fields['path'])}</baseDirectory><fileSystemBlockSize>{spec.get('blockSize', 4096)}</fileSystemBlockSize>"
        return f"<FileBlobStore {head}>{body}</FileBlobStore>", fields
    ref = (spec.get("credentials") or {}).get("secret")
    keys = {}
    if ref:
        keys = {k: secrets.lookup(ref, k, root) for k in KEYS[kind]}
        if None in keys.values():
            raise MissingCredentials(f"blob store {r.name}: secret {ref} needs keys {' and '.join(KEYS[kind])}")
    values = {k: spec[k] for k in FIELDS[kind] if spec.get(k) not in ("", None)}
    if kind == "s3":
        values = {"prefix": "", "endpoint": "", "maxConnections": 50, "gzip": True, **values,
                  "access": spec.get("access", "private").upper()}
    fields.update({k: v for k, v in values.items() if v not in ("", None)})
    fields.update(keys)
    for k, tag in FIELDS[kind].items():
        if values.get(k) not in ("", None):
            body += f"<{tag}>{xml_value(values[k])}</{tag}>"
    body += "".join(f"<{KEYS[kind][k]}>{escape(v)}</{KEYS[kind][k]}>" for k, v in keys.items())
    if kind in ("s3", "azure"):
        body += f"<useHTTPS>{str(not str(values.get('endpoint', '')).startswith('http://')).lower()}</useHTTPS>"
    return f"<{TAGS[kind]} {head}>{body}</{TAGS[kind]}>", fields


def live_blob_store(xml: str) -> dict:
    root = ET.fromstring(xml)
    text = lambda tag: (root.findtext(tag) or "")
    out = {"enabled": text("enabled") == "true", "default": root.get("default") == "true"}
    kind = next((k for k, tag in TAGS.items() if tag == root.tag), None)
    out["type"] = kind
    if kind == "file":
        out["path"] = text("baseDirectory")
        return out
    for k, tag in FIELDS.get(kind, {}).items():
        value = text(tag)
        if value:
            out[k] = value == "true" if value in ("true", "false") else (int(value) if k == "maxConnections" else value)
    for k, tag in KEYS.get(kind, {}).items():
        if text(tag):
            out[k] = text(tag)
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
            planner.add("+", "blobstore", name, note=f"{r.spec['type']}: {want.get('path') or want.get('bucket') or want.get('container')}", run=run)
            continue
        have = live_blob_store(live.text)
        changed = [(k, "(differs)" if k in SECRET_KEYS else have.get(k), "(from the Secret)" if k in SECRET_KEYS else v)
                   for k, v in want.items() if have.get(k) != v]
        if have["type"] != r.spec["type"]:
            changed.insert(0, ("type", have["type"], r.spec["type"]))
        if changed:
            planner.add("~", "blobstore", name, changed, run=run)
