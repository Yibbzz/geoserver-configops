"""Layer details: the web UI's layer settings beyond title, styles and cache.

On the layer's resource (its feature type, coverage or remote layer): a CQL
filter, per-layer WFS and WCS settings, metadata and data links, HTTP cache
headers, dimensions (time, elevation and custom ones), and a raster's bands and
reading parameters. On the layer itself: opaque, the rendering buffer, an
attribution with a link and logo, and the WMS path and interpolation.

Some of these live in GeoServer's `metadata` entries (keyed by @key), which are
merged key by key, never replaced. Only the fields a file sets are compared.
"""

from __future__ import annotations

PRESENTATIONS = {"list": "LIST", "continuous": "CONTINUOUS_INTERVAL", "interval": "DISCRETE_INTERVAL"}
WCS = {"requestCRS": "requestSRS", "responseCRS": "responseSRS", "formats": "supportedFormats", "interpolations": "interpolationMethods"}
TRANSLATED = ("internationalTitle", "internationalAbstract")  # {language: text}, on a resource or a group
DIMENSION_FIELDS = {"acceptableInterval": "acceptableInterval", "nearestFailBehavior": "nearestFailBehavior",
                    "rawNearestMatch": "rawNearestMatchEnabled", "start": "startValue", "end": "endValue"}  # spec -> GeoServer
FT_FIELDS = {"wfs.simpleConversion": "simpleConversionEnabled", "wfs.padWithZeros": "padWithZeros", "wfs.forcedDecimal": "forcedDecimal", "wfs.encodeMeasures": "encodeMeasures",
             "circularArcs": "circularArcPresent", "linearizationTolerance": "linearizationTolerance"}  # spec -> GeoServer


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


def is_raster(resource: dict) -> bool:
    return "grid" in resource or "nativeCoverageName" in resource


def dimension_key(resource: dict, name: str) -> str:
    """The metadata entry of a dimension: time and elevation as they are; a custom one as GeoServer names it."""
    if name in ("time", "elevation"):
        return name
    return f"custom_dimension_{name.upper()}" if is_raster(resource) else f"dim_{name}"


def strings(value) -> list:
    return [str(v) for v in as_list((value or {}).get("string"))]


def live_bands(resource: dict) -> list[dict]:
    out = []
    for b in as_list((resource.get("dimensions") or {}).get("coverageDimension")):
        rng = b.get("range") or {}
        out.append({"name": b.get("name"), "description": b.get("description") or None, "unit": b.get("unit") or None,
                    "nullValues": [float(v) for v in as_list((b.get("nullValues") or {}).get("double"))],
                    "range": [float(rng["min"]), float(rng["max"])] if "min" in rng and "max" in rng else None})
    return out


def wanted_bands(bands: list[dict]) -> list[dict]:
    out = []
    for b in bands:
        band = dict(b)
        if "nullValues" in band:
            band["nullValues"] = [float(v) for v in band["nullValues"]]
        if "range" in band:
            band["range"] = [float(v) for v in band["range"]]
        out.append(band)
    return out


def live_parameters(resource: dict) -> dict:
    out = {}
    for e in as_list((resource.get("parameters") or {}).get("entry")):
        pair = as_list(e.get("string")) if isinstance(e, dict) else []
        if len(pair) == 2:
            out[pair[0]] = pair[1]
    return out


def crs_code(value) -> str:
    text = str(value)
    return text if ":" in text else f"EPSG:{text}"


# --- the resource (feature type, coverage, remote layer)


def wanted_resource(spec: dict) -> dict[str, object]:
    """Field name -> wanted value, for the resource fields the layer's file sets."""
    out: dict[str, object] = {}
    if "filter" in spec:
        out["filter"] = spec["filter"]
    for key in TRANSLATED:
        if key in spec:
            out[key] = {str(k): str(v) for k, v in spec[key].items()}
    wfs = spec.get("wfs") or {}
    for key in ("maxFeatures", "decimals", "skipNumberMatched", "padWithZeros", "forcedDecimal", "encodeMeasures", "simpleConversion"):
        if key in wfs:
            out[f"wfs.{key}"] = wfs[key]
    for key in ("circularArcs", "linearizationTolerance"):
        if key in spec:
            out[key] = spec[key]
    if "disabledServices" in spec:
        out["disabledServices"] = sorted(s.upper() for s in spec["disabledServices"])
    for key, value in (spec.get("kml") or {}).items():
        out[f"kml.{key}"] = str(value)
    if "otherCRS" in wfs:
        out["wfs.otherCRS"] = sorted(crs_code(c) for c in wfs["otherCRS"])
    if "metadataLinks" in spec:
        out["metadataLinks"] = [{"url": m["url"], "type": m.get("type", "text/html"), "standard": m.get("standard", "ISO19115:2003")}
                                for m in spec["metadataLinks"]]
    if "httpCache" in spec:
        out["httpCache.maxAge"] = (spec["httpCache"] or {}).get("maxAge")
    for name, dim in (spec.get("dimensions") or {}).items():
        out[f"dimensions.{name}"] = dimension(dim)
    if "dataLinks" in spec:
        out["dataLinks"] = [{"url": d["url"], "type": d.get("type", "text/html")} for d in spec["dataLinks"]]
    wcs = spec.get("wcs") or {}
    for key in WCS:
        if key in wcs:
            out[f"wcs.{key}"] = sorted(wcs[key])
    if "defaultInterpolation" in wcs:
        out["wcs.defaultInterpolation"] = wcs["defaultInterpolation"]
    if "bands" in spec:
        out["bands"] = wanted_bands(spec["bands"])
    if "parameters" in spec:
        out["parameters"] = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in spec["parameters"].items()}
    return out


def as_text(value):
    """A dimension field's value as configops compares it: GeoServer gives numeric text back as numbers."""
    if isinstance(value, dict):  # defaultValue
        return {k: as_text(v) for k, v in value.items()}
    return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value


def dimension(dim: dict) -> dict:
    out = {"attribute": dim["attribute"], "presentation": PRESENTATIONS[dim.get("presentation", "list")]}
    for key in ("endAttribute", "resolution", "units", "unitSymbol"):
        if key in dim:
            out[key] = str(dim[key]) if key == "resolution" else dim[key]  # GeoServer gives it back as a number
    if "nearestMatch" in dim:
        out["nearestMatchEnabled"] = dim["nearestMatch"]
    for key, field in DIMENSION_FIELDS.items():
        if key in dim:
            out[field] = dim[key] if isinstance(dim[key], bool) else str(dim[key])
    if "default" in dim:
        default = dim["default"]
        out["defaultValue"] = {"strategy": default["strategy"], **({"referenceValue": str(default["value"])} if "value" in default else {})}
    return out


def live_resource(resource: dict, wanted: dict[str, object]) -> dict[str, object]:
    meta = entries(resource)
    out: dict[str, object] = {}
    for key in wanted:
        if key in TRANSLATED:
            live = resource.get(key)
            out[key] = {k: v for k, v in live.items() if v} if isinstance(live, dict) else {}
        elif key == "filter":
            out[key] = resource.get("cqlFilter")
        elif key == "wfs.maxFeatures":
            out[key] = resource.get("maxFeatures")
        elif key == "wfs.decimals":
            out[key] = resource.get("numDecimals")
        elif key == "wfs.skipNumberMatched":
            out[key] = resource.get("skipNumberMatched", False)
        elif key in FT_FIELDS:
            out[key] = resource.get(FT_FIELDS[key], None if key == "linearizationTolerance" else False)
        elif key == "disabledServices":
            out[key] = sorted(strings(resource.get("disabledServices"))) if resource.get("serviceConfiguration") else []
        elif key.startswith("kml."):
            value = (meta.get(key) or {}).get("$")
            out[key] = None if value is None else str(value)
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
            info = (meta.get(dimension_key(resource, key.split(".", 1)[1])) or {}).get("dimensionInfo") or {}
            want = wanted[key]
            out[key] = {k: as_text(info.get(k)) for k in want} if info.get("enabled") else None
        elif key == "dataLinks":
            links = as_list((resource.get("dataLinks") or {}).get("org.geoserver.catalog.impl.DataLinkInfoImpl"))
            out[key] = [{"url": d.get("content"), "type": d.get("type")} for d in links]
        elif key.startswith("wcs.") and key[4:] in WCS:
            out[key] = sorted(strings(resource.get(WCS[key[4:]])))
        elif key == "wcs.defaultInterpolation":
            out[key] = resource.get("defaultInterpolationMethod")
        elif key == "bands":
            live = live_bands(resource)
            out[key] = [{k: band.get(k) for k in want} for band, want in zip(live, wanted[key])] + live[len(wanted[key]):]
        elif key == "parameters":
            live = live_parameters(resource)
            out[key] = {k: live.get(k) for k in wanted[key]}
    return out


def patch_resource(resource: dict, wanted: dict[str, object]) -> dict:
    """A partial body setting the wanted fields; metadata entries merged into the resource's own."""
    out: dict = {}
    updates: dict[str, dict] = {}
    for key, value in wanted.items():
        if key in TRANSLATED:
            out[key] = value
        elif key == "filter":
            out["cqlFilter"] = value
        elif key == "wfs.maxFeatures":
            out["maxFeatures"] = value
        elif key == "wfs.decimals":
            out["numDecimals"] = value
        elif key == "wfs.skipNumberMatched":
            out["skipNumberMatched"] = value
        elif key in FT_FIELDS:
            out[FT_FIELDS[key]] = value
        elif key == "disabledServices":
            out["serviceConfiguration"] = bool(value)
            out["disabledServices"] = {"string": value}
        elif key.startswith("kml."):
            updates[key] = {"@key": key, "$": value}
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
            meta_key = dimension_key(resource, key.split(".", 1)[1])
            updates[meta_key] = {"@key": meta_key, "dimensionInfo": {"enabled": True, **value}}
        elif key == "dataLinks":
            out["dataLinks"] = {"org.geoserver.catalog.impl.DataLinkInfoImpl": [{"type": d["type"], "content": d["url"]} for d in value]}
        elif key.startswith("wcs.") and key[4:] in WCS:
            out[WCS[key[4:]]] = {"string": value}
        elif key == "wcs.defaultInterpolation":
            out["defaultInterpolationMethod"] = value
        elif key == "bands":
            live = as_list((resource.get("dimensions") or {}).get("coverageDimension"))
            bands = []
            for i, band in enumerate(value):
                b = dict(live[i]) if i < len(live) else {"dimensionType": {"name": "REAL_32BITS"}}
                b["name"] = band["name"]
                for k in ("description", "unit"):
                    if k in band:
                        b[k] = band[k]
                if "nullValues" in band:
                    b["nullValues"] = {"double": band["nullValues"]}
                if "range" in band:
                    b["range"] = {"min": band["range"][0], "max": band["range"][1]}
                bands.append(b)
            out["dimensions"] = {"coverageDimension": bands}
        elif key == "parameters":
            merged = {**live_parameters(resource), **value}
            out["parameters"] = {"entry": [{"string": [k, v]} for k, v in merged.items()]}
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
    wms = spec.get("wms") or {}
    if "path" in wms:
        out["wms.path"] = wms["path"]
    if "interpolation" in wms:
        out["wms.interpolation"] = wms["interpolation"].capitalize()
    if "authorityURLs" in spec:
        out["authorityURLs"] = [{"name": a["name"], "href": a["href"]} for a in spec["authorityURLs"]]
    if "identifiers" in spec:
        out["identifiers"] = [{"authority": i["authority"], "identifier": i["identifier"]} for i in spec["identifiers"]]
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
        elif key == "wms.path":
            out[key] = layer.get("path") or None
        elif key == "authorityURLs":
            out[key] = as_list((layer.get("authorityURLs") or {}).get("AuthorityURL"))
        elif key == "identifiers":
            out[key] = as_list((layer.get("identifiers") or {}).get("Identifier"))
        elif key == "wms.interpolation":
            out[key] = layer.get("defaultWMSInterpolationMethod")
    return out


def patch_layer(layer: dict, wanted: dict[str, object]) -> dict:
    """A partial body setting the wanted fields; metadata entries merged into the layer's own."""
    out: dict = {}
    if "opaque" in wanted:
        out["opaque"] = wanted["opaque"]
    if "attribution" in wanted:
        out["attribution"] = wanted["attribution"]
    if "wms.path" in wanted:
        out["path"] = wanted["wms.path"]
    if "authorityURLs" in wanted:
        out["authorityURLs"] = {"AuthorityURL": wanted["authorityURLs"]}
    if "identifiers" in wanted:
        out["identifiers"] = {"Identifier": wanted["identifiers"]}
    if "wms.interpolation" in wanted:
        out["defaultWMSInterpolationMethod"] = wanted["wms.interpolation"]
    if "buffer" in wanted:
        out["metadata"] = with_entries(layer, {"buffer": {"@key": "buffer", "$": str(wanted["buffer"])}})["metadata"]
    return out


def to_spec(resource: dict, layer: dict) -> dict:
    """The details a live resource and layer have that differ from GeoServer's defaults, as a Layer's spec fields."""
    out: dict = {}
    if resource.get("cqlFilter"):
        out["filter"] = resource["cqlFilter"]
    for key in TRANSLATED:
        texts = {k: v for k, v in (resource.get(key) or {}).items() if v} if isinstance(resource.get(key), dict) else {}
        if texts:
            out[key] = texts
    wfs: dict = {}
    if resource.get("maxFeatures"):
        wfs["maxFeatures"] = resource["maxFeatures"]
    if resource.get("numDecimals"):
        wfs["decimals"] = resource["numDecimals"]
    if resource.get("skipNumberMatched"):
        wfs["skipNumberMatched"] = True
    for key, field in FT_FIELDS.items():
        if resource.get(field) not in (None, False, 0, 0.0, ""):
            if key.startswith("wfs."):
                wfs[key[4:]] = resource[field]
            else:
                out[key] = resource[field]
    if resource.get("serviceConfiguration") and strings(resource.get("disabledServices")):
        out["disabledServices"] = sorted(s.lower() for s in strings(resource.get("disabledServices")))
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
    custom = {k[len("custom_dimension_"):].lower() if k.startswith("custom_dimension_") else k[len("dim_"):]: k
              for k in meta if k.startswith(("dim_", "custom_dimension_"))}
    for name, key in {"time": "time", "elevation": "elevation", **custom}.items():
        info = (meta.get(key) or {}).get("dimensionInfo") or {}
        if info.get("enabled") and info.get("attribute"):
            dim = {"attribute": info["attribute"], "presentation": names.get(info.get("presentation"), "list")}
            for key in ("endAttribute", "resolution", "units", "unitSymbol"):
                if info.get(key) not in (None, ""):
                    dim[key] = str(info[key]) if key == "resolution" else info[key]
            if info.get("nearestMatchEnabled"):
                dim["nearestMatch"] = True
            for k, field in DIMENSION_FIELDS.items():
                value = info.get(field)
                if value not in (None, "") and value is not False and not (k == "nearestFailBehavior" and value == "IGNORE"):
                    dim[k] = value if isinstance(value, bool) else str(value)
            default = info.get("defaultValue") or {}
            if default.get("strategy") not in (None, "BUILTIN"):
                dim["default"] = {"strategy": default["strategy"],
                                  **({"value": str(default["referenceValue"])} if default.get("referenceValue") not in (None, "") else {})}
            out.setdefault("dimensions", {})[name] = dim
    links = as_list((resource.get("dataLinks") or {}).get("org.geoserver.catalog.impl.DataLinkInfoImpl"))
    if links:
        out["dataLinks"] = [{"url": d.get("content"), **({"type": d["type"]} if d.get("type") not in (None, "text/html") else {})}
                            for d in links]
    wcs = {key: strings(resource.get(field)) for key, field in WCS.items() if strings(resource.get(field))} if is_raster(resource) else {}
    if resource.get("defaultInterpolationMethod"):
        wcs["defaultInterpolation"] = resource["defaultInterpolationMethod"]
    if wcs:
        out["wcs"] = wcs
    bands = []
    for band in live_bands(resource):
        keep = {k: v for k, v in band.items() if v not in (None, [], "")}
        if keep.get("range") and any(abs(v) == float("inf") for v in keep["range"]):
            del keep["range"]
        bands.append(keep)
    if bands:
        out["bands"] = bands
    params = live_parameters(resource)
    if params:
        out["parameters"] = params
    kml = {k[4:]: int(e["$"]) if k == "kml.regionateFeatureLimit" else e["$"] for k, e in meta.items() if k.startswith("kml.") and "$" in e}
    if kml:
        out["kml"] = kml
    if layer.get("opaque"):
        out["opaque"] = True
    buffer = (entries(layer).get("buffer") or {}).get("$")
    if buffer not in (None, ""):
        out["buffer"] = int(buffer)
    wms = {}
    if layer.get("path"):
        wms["path"] = layer["path"]
    if layer.get("defaultWMSInterpolationMethod"):
        wms["interpolation"] = layer["defaultWMSInterpolationMethod"].lower()
    if wms:
        out["wms"] = wms
    for key, inner in (("authorityURLs", "AuthorityURL"), ("identifiers", "Identifier")):
        items = as_list((layer.get(key) or {}).get(inner))
        if items:
            out[key] = items
    attribution = layer.get("attribution") or {}
    if attribution.get("href") or attribution.get("logoURL"):
        logo = {k: attribution[g] for k, g in (("url", "logoURL"), ("width", "logoWidth"), ("height", "logoHeight"), ("type", "logoType"))
                if attribution.get(g) not in (None, "", 0)}
        out["attribution"] = {k: v for k, v in (("title", attribution.get("title")), ("link", attribution.get("href")),
                                                ("logo", logo or None)) if v}
    return out
