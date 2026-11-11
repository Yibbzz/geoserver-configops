"""Layer details: the web UI's layer settings beyond title, styles and cache.

On the layer's resource (its feature type, coverage or remote layer): a CQL
filter, per-layer WFS settings, metadata links, HTTP cache headers and time
and elevation dimensions. On the layer itself: opaque, the rendering buffer,
and an attribution with a link and logo.

Some of these live in GeoServer's `metadata` entries (keyed by @key), which are
merged key by key, never replaced. Only the fields a file sets are compared.
"""

from __future__ import annotations

PRESENTATIONS = {"list": "LIST", "continuous": "CONTINUOUS_INTERVAL", "interval": "DISCRETE_INTERVAL"}


def as_list(value) -> list:
    if value in (None, ""):
        return []
    return value if isinstance(value, list) else [value]


def entries(obj: dict) -> dict[str, dict]:
    return {e["@key"]: e for e in as_list((obj.get("metadata") or {}).get("entry")) if isinstance(e, dict) and "@key" in e}


def with_entries(obj: dict, updates: dict[str, dict]) -> dict:
    """`obj` with these metadata entries set, the others kept."""
    current = entries(obj)
    current.update(updates)
    return {**obj, "metadata": {"entry": list(current.values())}}


def crs_code(value) -> str:
    text = str(value)
    return text if ":" in text else f"EPSG:{text}"


# --- the resource (feature type, coverage, remote layer)


def wanted_resource(spec: dict) -> dict[str, object]:
    """Field name -> wanted value, for the resource fields the layer's file sets."""
    out: dict[str, object] = {}
    if "filter" in spec:
        out["filter"] = spec["filter"]
    wfs = spec.get("wfs") or {}
    for key in ("maxFeatures", "decimals", "skipNumberMatched"):
        if key in wfs:
            out[f"wfs.{key}"] = wfs[key]
    if "otherCRS" in wfs:
        out["wfs.otherCRS"] = sorted(crs_code(c) for c in wfs["otherCRS"])
    if "metadataLinks" in spec:
        out["metadataLinks"] = [{"url": m["url"], "type": m.get("type", "text/html"), "standard": m.get("standard", "ISO19115:2003")}
                                for m in spec["metadataLinks"]]
    if "httpCache" in spec:
        out["httpCache.maxAge"] = (spec["httpCache"] or {}).get("maxAge")
    for name, dim in (spec.get("dimensions") or {}).items():
        out[f"dimensions.{name}"] = dimension(dim)
    return out


def dimension(dim: dict) -> dict:
    out = {"attribute": dim["attribute"], "presentation": PRESENTATIONS[dim.get("presentation", "list")]}
    for key in ("endAttribute", "resolution", "units", "unitSymbol"):
        if key in dim:
            out[key] = dim[key]
    if "nearestMatch" in dim:
        out["nearestMatchEnabled"] = dim["nearestMatch"]
    return out


def live_resource(resource: dict, wanted: dict[str, object]) -> dict[str, object]:
    meta = entries(resource)
    out: dict[str, object] = {}
    for key in wanted:
        if key == "filter":
            out[key] = resource.get("cqlFilter")
        elif key == "wfs.maxFeatures":
            out[key] = resource.get("maxFeatures")
        elif key == "wfs.decimals":
            out[key] = resource.get("numDecimals")
        elif key == "wfs.skipNumberMatched":
            out[key] = resource.get("skipNumberMatched", False)
        elif key == "wfs.otherCRS":
            codes = as_list((resource.get("responseSRS") or {}).get("string")) if resource.get("overridingServiceSRS") else []
            out[key] = sorted(crs_code(c) for c in codes)
        elif key == "metadataLinks":
            out[key] = [{"url": m.get("content"), "type": m.get("type"), "standard": m.get("metadataType")}
                        for m in as_list((resource.get("metadataLinks") or {}).get("metadataLink"))]
        elif key == "httpCache.maxAge":
            on = str((meta.get("cachingEnabled") or {}).get("$", "false")).lower() == "true"
            age = (meta.get("cacheAgeMax") or {}).get("$")
            out[key] = int(age) if on and age not in (None, "") else None
        elif key.startswith("dimensions."):
            info = (meta.get(key.split(".", 1)[1]) or {}).get("dimensionInfo") or {}
            want = wanted[key]
            out[key] = {k: info.get(k) for k in want} if info.get("enabled") else None
    return out


def patch_resource(resource: dict, wanted: dict[str, object]) -> dict:
    """A partial body setting the wanted fields; metadata entries merged into the resource's own."""
    out: dict = {}
    updates: dict[str, dict] = {}
    for key, value in wanted.items():
        if key == "filter":
            out["cqlFilter"] = value
        elif key == "wfs.maxFeatures":
            out["maxFeatures"] = value
        elif key == "wfs.decimals":
            out["numDecimals"] = value
        elif key == "wfs.skipNumberMatched":
            out["skipNumberMatched"] = value
        elif key == "wfs.otherCRS":
            out["overridingServiceSRS"] = bool(value)
            out["responseSRS"] = {"string": [c.split(":", 1)[1] if c.startswith("EPSG:") else c for c in value]}
        elif key == "metadataLinks":
            out["metadataLinks"] = {"metadataLink": [{"type": m["type"], "metadataType": m["standard"], "content": m["url"]} for m in value]}
        elif key == "httpCache.maxAge":
            updates["cachingEnabled"] = {"@key": "cachingEnabled", "$": "true" if value is not None else "false"}
            if value is not None:
                updates["cacheAgeMax"] = {"@key": "cacheAgeMax", "$": str(value)}
        elif key.startswith("dimensions."):
            updates[key.split(".", 1)[1]] = {"@key": key.split(".", 1)[1], "dimensionInfo": {"enabled": True, **value}}
    if updates:
        out["metadata"] = with_entries(resource, updates)["metadata"]
    return out


# --- the layer


def wanted_layer(spec: dict) -> dict[str, object]:
    out: dict[str, object] = {}
    if "opaque" in spec:
        out["opaque"] = spec["opaque"]
    if "buffer" in spec:
        out["buffer"] = spec["buffer"]
    attribution = spec.get("attribution")
    if isinstance(attribution, dict):
        logo = attribution.get("logo") or {}
        out["attribution"] = {k: v for k, v in {
            "title": attribution.get("title"), "href": attribution.get("link"), "logoURL": logo.get("url"),
            "logoWidth": logo.get("width"), "logoHeight": logo.get("height"), "logoType": logo.get("type")}.items() if v is not None}
    return out


def live_layer(layer: dict, wanted: dict[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in wanted.items():
        if key == "opaque":
            out[key] = layer.get("opaque", False)
        elif key == "buffer":
            buffer = (entries(layer).get("buffer") or {}).get("$")
            out[key] = int(buffer) if buffer not in (None, "") else None
        elif key == "attribution":
            live = layer.get("attribution") or {}
            out[key] = {k: live.get(k) for k in value}
    return out


def patch_layer(layer: dict, wanted: dict[str, object]) -> dict:
    """A partial body setting the wanted fields; metadata entries merged into the layer's own."""
    out: dict = {}
    if "opaque" in wanted:
        out["opaque"] = wanted["opaque"]
    if "attribution" in wanted:
        out["attribution"] = wanted["attribution"]
    if "buffer" in wanted:
        out["metadata"] = with_entries(layer, {"buffer": {"@key": "buffer", "$": str(wanted["buffer"])}})["metadata"]
    return out


def to_spec(resource: dict, layer: dict) -> dict:
    """The details a live resource and layer have that differ from GeoServer's defaults, as a Layer's spec fields."""
    out: dict = {}
    if resource.get("cqlFilter"):
        out["filter"] = resource["cqlFilter"]
    wfs: dict = {}
    if resource.get("maxFeatures"):
        wfs["maxFeatures"] = resource["maxFeatures"]
    if resource.get("numDecimals"):
        wfs["decimals"] = resource["numDecimals"]
    if resource.get("skipNumberMatched"):
        wfs["skipNumberMatched"] = True
    codes = as_list((resource.get("responseSRS") or {}).get("string"))
    if resource.get("overridingServiceSRS") and codes:
        wfs["otherCRS"] = sorted(crs_code(c) for c in codes)
    if wfs:
        out["wfs"] = wfs
    links = as_list((resource.get("metadataLinks") or {}).get("metadataLink"))
    if links:
        out["metadataLinks"] = [{"url": m.get("content"), "type": m.get("type"), "standard": m.get("metadataType")} for m in links]
    meta = entries(resource)
    if str((meta.get("cachingEnabled") or {}).get("$", "")).lower() == "true" and (meta.get("cacheAgeMax") or {}).get("$"):
        out["httpCache"] = {"maxAge": int(meta["cacheAgeMax"]["$"])}
    names = {v: k for k, v in PRESENTATIONS.items()}
    for name in ("time", "elevation"):
        info = (meta.get(name) or {}).get("dimensionInfo") or {}
        if info.get("enabled") and info.get("attribute"):
            dim = {"attribute": info["attribute"], "presentation": names.get(info.get("presentation"), "list")}
            for key in ("endAttribute", "resolution", "units", "unitSymbol"):
                if info.get(key) not in (None, ""):
                    dim[key] = info[key]
            if info.get("nearestMatchEnabled"):
                dim["nearestMatch"] = True
            out.setdefault("dimensions", {})[name] = dim
    if layer.get("opaque"):
        out["opaque"] = True
    buffer = (entries(layer).get("buffer") or {}).get("$")
    if buffer not in (None, ""):
        out["buffer"] = int(buffer)
    attribution = layer.get("attribution") or {}
    if attribution.get("href") or attribution.get("logoURL"):
        logo = {k: attribution[g] for k, g in (("url", "logoURL"), ("width", "logoWidth"), ("height", "logoHeight"), ("type", "logoType"))
                if attribution.get(g) not in (None, "", 0)}
        out["attribution"] = {k: v for k, v in (("title", attribution.get("title")), ("link", attribution.get("href")),
                                                ("logo", logo or None)) if v}
    return out
