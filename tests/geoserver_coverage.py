"""How much of GeoServer's configuration configops covers, field by field.

GeoServer keeps its configuration in Java objects that its REST API reads and writes
with XStream, one element per field. This script lists those fields from GeoServer's
and GeoWebCache's own source, at the versions GeoServer Cloud runs, and keeps them in
docs/geoserver-fields.yaml, where each one is sorted:

  yes   configops sets it; `spec` names where in the files (checked against the schema)
  no    GeoServer Cloud's REST API can set it; configops doesn't yet
  cant  GeoServer Cloud has no way to set it (`note` says why)
  none  not configuration: an id, a timestamp, worked out by GeoServer or configops

  python tests/geoserver_coverage.py refresh   # re-read the source; new fields come in as `unsorted`
  python tests/geoserver_coverage.py report    # print the coverage and update the README's badge

tests/test_geoserver_coverage.py fails while a field is unsorted, a `spec` path isn't in
the schema, or the README's number is out of date; tests/test_e2e_coverage.py checks that
every field a live GeoServer shows is listed, and that the versions match.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "docs" / "geoserver-fields.yaml"
TESTED = ROOT / "docs" / "geoserver-tested.txt"
README = ROOT / "README.md"
SCHEMA = ROOT / "configops" / "schema" / "v1alpha1.json"

REPOS = {"geoserver": "geoserver/geoserver", "geowebcache": "GeoWebCache/geowebcache"}
STATUSES = ("yes", "no", "cant", "none")

# The classes behind every page of the web UI, by page, as (repo, path ending). Base
# classes are listed once: ResourceInfoImpl's fields are shared by every kind of layer.
CLASSES: dict[str, list[tuple[str, str]]] = {
    "Workspaces": [
        ("geoserver", "catalog/impl/WorkspaceInfoImpl"),
        ("geoserver", "catalog/impl/NamespaceInfoImpl"),
    ],
    "Stores": [
        ("geoserver", "catalog/impl/StoreInfoImpl"),
        ("geoserver", "catalog/impl/DataStoreInfoImpl"),
        ("geoserver", "catalog/impl/CoverageStoreInfoImpl"),
        ("geoserver", "catalog/impl/WMSStoreInfoImpl"),
        ("geoserver", "catalog/impl/WMTSStoreInfoImpl"),
    ],
    "Layers": [
        ("geoserver", "catalog/impl/ResourceInfoImpl"),
        ("geoserver", "catalog/impl/FeatureTypeInfoImpl"),
        ("geoserver", "catalog/impl/CoverageInfoImpl"),
        ("geoserver", "catalog/impl/WMSLayerInfoImpl"),
        ("geoserver", "catalog/impl/WMTSLayerInfoImpl"),
        ("geoserver", "catalog/impl/LayerInfoImpl"),
        ("geoserver", "catalog/impl/AttributeTypeInfoImpl"),
        ("geoserver", "catalog/impl/CoverageDimensionImpl"),
        ("geoserver", "catalog/impl/DimensionInfoImpl"),
        ("geoserver", "catalog/DimensionDefaultValueSetting"),
        ("geoserver", "catalog/impl/MetadataLinkInfoImpl"),
        ("geoserver", "catalog/impl/DataLinkInfoImpl"),
        ("geoserver", "catalog/impl/AttributionInfoImpl"),
        ("geoserver", "catalog/impl/AuthorityURL"),
        ("geoserver", "catalog/impl/LayerIdentifier"),
        ("geoserver", "catalog/Keyword"),
        ("geoserver", "catalog/CoverageView"),
    ],
    "Layer groups": [
        ("geoserver", "catalog/impl/LayerGroupInfoImpl"),
        ("geoserver", "catalog/impl/LayerGroupStyleImpl"),
    ],
    "Styles": [
        ("geoserver", "catalog/impl/StyleInfoImpl"),
        ("geoserver", "catalog/impl/LegendInfoImpl"),
    ],
    "Services": [
        ("geoserver", "config/impl/ServiceInfoImpl"),
        ("geoserver", "wms/WMSInfoImpl"),
        ("geoserver", "wms/WatermarkInfoImpl"),
        ("geoserver", "wfs/WFSInfoImpl"),
        ("geoserver", "wfs/GMLInfoImpl"),
        ("geoserver", "wcs/WCSInfoImpl"),
        ("geoserver", "gwc/wmts/WMTSInfoImpl"),
        ("geoserver", "ogcapi/v1/features/FeatureConformance"),
    ],
    "Settings": [
        ("geoserver", "config/impl/GeoServerInfoImpl"),
        ("geoserver", "config/impl/SettingsInfoImpl"),
        ("geoserver", "config/impl/ContactInfoImpl"),
        ("geoserver", "config/impl/ImageProcessingInfoImpl"),
        ("geoserver", "config/impl/CoverageAccessInfoImpl"),
    ],
    "Tile caching": [
        ("geoserver", "gwc/config/GWCConfig"),
        ("geoserver", "gwc/layer/GeoServerTileLayerInfoImpl"),
        ("geoserver", "gwc/layer/StyleParameterFilter"),
        ("geowebcache", "filter/parameters/ParameterFilter"),
        ("geowebcache", "filter/parameters/CaseNormalizingParameterFilter"),
        ("geowebcache", "filter/parameters/CaseNormalizer"),
        ("geowebcache", "filter/parameters/StringParameterFilter"),
        ("geowebcache", "filter/parameters/RegexParameterFilter"),
        ("geowebcache", "filter/parameters/FloatParameterFilter"),
        ("geowebcache", "filter/parameters/IntegerParameterFilter"),
        ("geowebcache", "layer/ExpirationRule"),
        ("geowebcache", "config/XMLGridSet"),
        ("geowebcache", "config/XMLGridSubset"),
        ("geowebcache", "config/BlobStoreInfo"),
        ("geowebcache", "config/FileBlobStoreInfo"),
        ("geowebcache", "s3/S3BlobStoreInfo"),
        ("geowebcache", "azure/AzureBlobStoreInfo"),
        ("geowebcache", "gcs/GoogleCloudStorageBlobStoreInfo"),
        ("geowebcache", "diskquota/DiskQuotaConfig"),
        ("geowebcache", "diskquota/storage/LayerQuota"),
    ],
    "Security": [
        ("geoserver", "security/config/SecurityManagerConfig"),
        ("geoserver", "security/config/BruteForcePreventionConfig"),
        ("geoserver", "security/config/BaseSecurityNamedServiceConfig"),
        ("geoserver", "security/config/PasswordPolicyConfig"),
        ("geoserver", "security/password/MasterPasswordProviderConfig"),
        ("geoserver", "security/password/URLMasterPasswordProviderConfig"),
        ("geoserver", "security/config/SecurityUserGroupServiceConfig"),
        ("geoserver", "security/config/SecurityRoleServiceConfig"),
        ("geoserver", "security/config/FileBasedSecurityServiceConfig"),
        ("geoserver", "security/xml/XMLSecurityServiceConfig"),
        ("geoserver", "security/xml/XMLUserGroupServiceConfig"),
        ("geoserver", "security/xml/XMLRoleServiceConfig"),
        ("geoserver", "security/jdbc/config/JDBCSecurityServiceConfig"),
        ("geoserver", "security/jdbc/config/JDBCUserGroupServiceConfig"),
        ("geoserver", "security/jdbc/config/JDBCRoleServiceConfig"),
        ("geoserver", "security/jdbc/config/JDBCConnectAuthProviderConfig"),
        ("geoserver", "security/ldap/LDAPBaseSecurityServiceConfig"),
        ("geoserver", "security/ldap/LDAPSecurityServiceConfig"),
        ("geoserver", "security/ldap/LDAPUserGroupServiceConfig"),
        ("geoserver", "security/ldap/LDAPRoleServiceConfig"),
        ("geoserver", "security/config/UsernamePasswordAuthenticationProviderConfig"),
        ("geoserver", "security/config/SecurityFilterConfig"),
        ("geoserver", "security/config/SecurityAuthFilterConfig"),
        ("geoserver", "security/config/BasicAuthenticationFilterConfig"),
        ("geoserver", "security/config/DigestAuthenticationFilterConfig"),
        ("geoserver", "security/config/UsernamePasswordAuthenticationFilterConfig"),
        ("geoserver", "security/config/RememberMeAuthenticationFilterConfig"),
        ("geoserver", "security/config/PreAuthenticatedUserNameFilterConfig"),
        ("geoserver", "security/config/RequestHeaderAuthenticationFilterConfig"),
        ("geoserver", "security/config/CredentialsFromRequestHeaderFilterConfig"),
        ("geoserver", "security/config/RoleFilterConfig"),
        ("geoserver", "security/config/SSLFilterConfig"),
        ("geoserver", "security/config/ExceptionTranslationFilterConfig"),
        ("geoserver", "security/config/SecurityInterceptorFilterConfig"),
        ("geoserver", "security/config/SecurityContextPersistenceFilterConfig"),
        ("geoserver", "security/config/LogoutFilterConfig"),
        ("geoserver", "security/config/AnonymousAuthenticationFilterConfig"),
        ("geoserver", "org/geoserver/security/AuthenticationKeyFilterConfig"),
        ("geoserver", "org/geoserver/security/GeoServerRestRoleServiceConfig"),
        ("geoserver", "security/cors/CORSConfiguration"),
        ("geoserver", "security/csp/CSPConfiguration"),
        ("geoserver", "security/csp/CSPPolicy"),
        ("geoserver", "security/csp/CSPRule"),
        ("geoserver", "security/urlchecks/AbstractURLCheck"),
        ("geoserver", "security/urlchecks/RegexURLCheck"),
    ],
}

# Files that hold XStream's omitField/aliasField calls: fields left out, or renamed, in the XML.
PERSISTERS = [
    ("geoserver", "config/util/XStreamPersister"),
    ("geoserver", "wms/WMSXStreamLoader"),
    ("geoserver", "wfs/WFSXStreamLoader"),
    ("geoserver", "wcs/WCSXStreamLoader"),
    ("geoserver", "gwc/wmts/WMTSXStreamLoader"),
    ("geoserver", "gwc/config/GWCConfigPersister"),
]


# --- reading Java ------------------------------------------------------------------

def strip_java(source: str) -> str:
    """Remove comments and string/char literals, keeping the code's shape."""
    token = re.compile(r'/\*.*?\*/|//[^\n]*|""".*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\])\'', re.S)
    return token.sub(lambda m: " " if m.group().startswith("/") else '""', source)


def java_fields(source: str) -> list[str]:
    """The instance fields of a Java file's top-level class: what XStream writes."""
    code = strip_java(source)
    fields, depth, statement = [], 0, ""
    for char in code:
        if char == "{":
            depth += 1
            statement = ""
        elif char == "}":
            depth -= 1
            statement = ""
        elif depth == 1 and char == ";":
            fields += declared(statement)
            statement = ""
        elif depth == 1:
            statement += char
    return fields


def declared(statement: str) -> list[str]:
    statement = re.sub(r"@\w+(\([^)]*\))?", " ", statement).strip()
    words = statement.split()
    if not words or {"static", "transient", "import", "package", "abstract"} & set(words):
        return []
    left = statement.split("=", 1)[0]
    if "(" in left or words[0] in ("return", "throw"):
        return []
    left = re.sub(r"<[^<>]*(<[^<>]*>[^<>]*)*>", "", left)  # drop generics, which hold commas
    names = []
    for part in left.split(","):
        match = re.search(r"(\w+)\s*(\[\s*\])*\s*$", part)
        if match:
            names.append(match.group(1))
    return names


def xstream_rules(sources: list[str]) -> tuple[set[tuple[str, str]], dict[tuple[str, str], str]]:
    """(class, field) pairs XStream omits, and fields it writes under another name."""
    omitted, aliases = set(), {}
    comments = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\\n])*"', re.S)
    for source in sources:
        source = comments.sub(lambda m: m.group() if m.group().startswith('"') else " ", source)
        for cls, field in re.findall(r'omitField\(\s*(?:impl\()?\s*(\w+)\.class\)?\s*,\s*"(\w+)"', source):
            omitted.add((impl_name(cls), field))
        for alias, cls, field in re.findall(r'aliasField\(\s*"(\w+)"\s*,\s*(?:impl\()?\s*(\w+)\.class\)?\s*,\s*"(\w+)"', source):
            aliases[(impl_name(cls), field)] = alias
    return omitted, aliases


def impl_name(cls: str) -> str:
    """XStreamPersister names interfaces through impl(): WMSInfo means WMSInfoImpl."""
    return cls if cls.endswith("Impl") or not cls.endswith("Info") else cls + "Impl"


# --- fetching ----------------------------------------------------------------------

def fetch(url: str) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "configops-coverage"}), timeout=60) as r:
        return r.read().decode()


def source_files(versions: dict[str, str]) -> dict[tuple[str, str], str]:
    """Each class's and persister's source, from GitHub at the pinned versions."""
    wanted = [entry for page in CLASSES.values() for entry in page] + PERSISTERS
    out = {}
    for repo, tag in versions.items():
        tree = json.loads(fetch(f"https://api.github.com/repos/{REPOS[repo]}/git/trees/{tag}?recursive=1"))
        paths = [t["path"] for t in tree["tree"] if t["path"].endswith(".java") and "/test/" not in t["path"]]
        for want_repo, ending in wanted:
            if want_repo != repo:
                continue
            found = [p for p in paths if p.endswith("/" + ending + ".java")]
            if len(found) != 1:
                raise SystemExit(f"{repo} {tag}: {ending}.java found {len(found)} times: {found}")
            out[(repo, ending)] = fetch(f"https://raw.githubusercontent.com/{REPOS[repo]}/{tag}/{found[0]}")
    return out


def source_fields(versions: dict[str, str]) -> tuple[dict[str, dict[str, list[str]]], dict[str, str]]:
    """({page: {class: [field as written in the XML]}}, {class: the class it extends}) from the source."""
    files = source_files(versions)
    parents = {}
    for entry in [e for page in CLASSES.values() for e in page]:
        match = re.search(r"class\s+(\w+)(?:<[^{]*?>)?\s+extends\s+(\w+)", strip_java(files[entry]))
        if match:
            parents[match.group(1)] = match.group(2)
    omitted, aliases = xstream_rules([files[p] for p in PERSISTERS])
    out: dict[str, dict[str, list[str]]] = {}
    for page, entries in CLASSES.items():
        for entry in entries:
            cls = entry[1].rsplit("/", 1)[-1]
            names = [aliases.get((cls, f), f) for f in java_fields(files[entry]) if (cls, f) not in omitted]
            out.setdefault(page, {})[cls] = names
    return out, parents


# --- the manifest ------------------------------------------------------------------

def load() -> dict:
    return yaml.safe_load(MANIFEST.read_text())


def save(manifest: dict) -> None:
    MANIFEST.write_text(HEADER + yaml.dump(manifest, sort_keys=False, width=200, allow_unicode=True))


HEADER = """\
# Every field of GeoServer's configuration, from its source, and whether configops sets it.
# tests/geoserver_coverage.py writes the list; the statuses, `spec` and `note` are kept by hand.
#   yes: configops sets it (spec: where, as a path in configops/schema/v1alpha1.json's $defs,
#        or a project folder: styles/, templates/)
#   no: possible through GeoServer Cloud's REST API, not built yet
#   cant: GeoServer Cloud has no way to set it
#   none: not configuration (ids, timestamps, values worked out by GeoServer or configops)
"""


def refresh() -> dict:
    """Re-read the source: keep what's sorted, add new fields as unsorted, drop vanished ones."""
    manifest = load() if MANIFEST.exists() else {"versions": {"geoserver": "3.0.2", "geowebcache": "2.0.2"}, "pages": {}}
    found, parents = source_fields(manifest["versions"])
    old = {cls: fields for classes in manifest["pages"].values() for cls, fields in classes.items()}
    pages = {}
    for page, classes in found.items():
        pages[page] = {}
        for cls, names in classes.items():
            kept = (old.get(cls) or {})
            pages[page][cls] = {n: kept.get(n) or {"status": "unsorted"} for n in names}
            for extra, entry in kept.items():  # metadata keys and the like, added by hand
                if extra not in names and entry.get("extra"):
                    pages[page][cls][extra] = entry
    for page, classes in manifest["pages"].items():  # classes made only of entries added by hand
        for cls, fields in classes.items():
            if not any(cls in c for c in found.values()) and fields and all(e.get("extra") for e in fields.values()):
                pages.setdefault(page, {})[cls] = fields
    manifest["pages"] = pages
    manifest["parents"] = dict(sorted(parents.items()))
    save(manifest)
    return manifest


# --- checks and the number ---------------------------------------------------------

def entries(manifest: dict):
    for page, classes in manifest["pages"].items():
        for cls, fields in classes.items():
            for name, entry in fields.items():
                yield page, cls, name, entry


def schema_has(path: str, schema: dict | None = None) -> bool:
    """Whether a dotted path (`layer.wfs.maxFeatures`) is in the schema's $defs.

    A step may pass through an object that takes any key (GeoServer's own fields, passed on)."""
    schema = schema or json.loads(SCHEMA.read_text())
    defs = schema["$defs"]
    first, *rest = path.split(".")
    node = defs.get(first)
    if node is None:
        return False
    for step in rest:
        node = resolve(node, defs)
        if step in (node.get("properties") or {}):
            node = node["properties"][step]
        elif isinstance(node.get("additionalProperties"), dict):
            node = node["additionalProperties"]
        elif node.get("additionalProperties") is True or ("properties" not in node and node.get("type") == "object"):
            return True
        else:
            return False
    return True


PROJECT_FOLDERS = {"styles/", "templates/"}  # files taken as they are, with no schema: see configops/assets.py


def in_files(path: str, schema: dict) -> bool:
    """A schema path, or one of the project's folders of files."""
    return path in PROJECT_FOLDERS or schema_has(path, schema)


def resolve(node: dict, defs: dict) -> dict:
    """Follow $ref and merge allOf/anyOf/oneOf branches' properties into one view."""
    while "$ref" in node:
        node = defs[node["$ref"].rsplit("/", 1)[-1]]
    merged = dict(node)
    for key in ("allOf", "anyOf", "oneOf"):
        for branch in node.get(key) or []:
            branch = resolve(branch, defs)
            merged["properties"] = {**(branch.get("properties") or {}), **(merged.get("properties") or {})}
            if branch.get("additionalProperties") is True and "additionalProperties" not in node:
                merged["additionalProperties"] = True
            if branch.get("type") == "object":
                merged.setdefault("type", "object")
    if node.get("type") == "array" and "items" in node:
        return resolve(node["items"], defs)
    return merged


def problems(manifest: dict) -> list[str]:
    schema = json.loads(SCHEMA.read_text())
    out = []
    for page, cls, name, entry in entries(manifest):
        where = f"{page}: {cls}.{name}"
        status = entry.get("status")
        if status not in STATUSES:
            out.append(f"{where} is {status}: sort it ({', '.join(STATUSES)})")
        elif status == "yes" and not entry.get("spec"):
            out.append(f"{where} is yes but has no spec path")
        elif status == "yes" and not all(in_files(p.strip(), schema) for p in str(entry["spec"]).split(",")):
            out.append(f"{where}: spec {entry['spec']} isn't in the schema")
        elif status == "cant" and not entry.get("note"):
            out.append(f"{where} is cant but has no note saying why")
    for field in sorted(tested() - yes_fields(manifest)):
        out.append(f"{TESTED.name} has {field}, which isn't a yes field")
    return out


# --- tested: which `yes` fields the e2e tests have proved -----------------------------

def tested() -> set[str]:
    """'Class.field' lines in docs/geoserver-tested.txt: written only by the e2e tests."""
    if not TESTED.exists():
        return set()
    return {line.strip() for line in TESTED.read_text().splitlines() if line.strip() and not line.startswith("#")}


def save_tested(fields: set[str]) -> None:
    TESTED.write_text("# Fields the e2e tests have proved (tests/test_e2e_coverage.py writes this file; don't edit it):\n"
                      "# configops changed the field in GeoServer and GeoServer then showed the new value,\n"
                      "# or the test the field names in docs/geoserver-fields.yaml passed.\n"
                      + "".join(f"{f}\n" for f in sorted(fields)))


def yes_fields(manifest: dict) -> set[str]:
    return {f"{cls}.{name}" for _, cls, name, e in entries(manifest) if e.get("status") == "yes"}


def counts(manifest: dict, proven: set[str] | None = None) -> dict[str, dict[str, int]]:
    """{page: {status: n, 'tested': n}} and the total under 'all'."""
    proven = tested() if proven is None else proven
    keys = (*STATUSES, "unsorted", "tested")
    out: dict[str, dict[str, int]] = {}
    for page, cls, name, entry in entries(manifest):
        c = out.setdefault(page, dict.fromkeys(keys, 0))
        c[entry["status"]] += 1
        c["tested"] += entry["status"] == "yes" and f"{cls}.{name}" in proven
    out["all"] = {k: sum(c[k] for c in out.values()) for k in keys}
    return out


def percent(c: dict[str, int]) -> int:
    """Tested share of what can be set: tested / (yes + no). `cant` is reported apart."""
    settable = c["yes"] + c["no"]
    return int(100 * c["tested"] / settable) if settable else 100


def badge(manifest: dict) -> str:
    c = counts(manifest)["all"]
    p = percent(c)
    colour = "brightgreen" if p >= 90 else "green" if p >= 80 else "yellow" if p >= 60 else "orange"
    return (f"![GeoServer coverage](https://img.shields.io/badge/GeoServer_coverage-{p}%25-{colour}) "
            f"Of the {c['yes'] + c['no']} settings GeoServer Cloud's REST API can set, configops sets {c['yes']}, "
            f"and the e2e tests have proved {c['tested']} of those ({c['cant']} more GeoServer Cloud can't set; "
            f"[field by field](docs/geoserver-fields.yaml)).")


BADGE = re.compile(r"!\[GeoServer coverage\][^\n]*")


def report(manifest: dict, write: bool = True) -> str:
    lines = [f"{'page':<14} {'tested':>6} {'yes':>4} {'no':>4} {'cant':>4} {'none':>4}  covered (tested)"]
    for page, c in counts(manifest).items():
        lines.append(f"{page:<14} {c['tested']:>6} {c['yes']:>4} {c['no']:>4} {c['cant']:>4} {c['none']:>4}  {percent(c)}%"
                     + (f"  ({c['unsorted']} unsorted)" if c["unsorted"] else ""))
    if write:
        text = README.read_text()
        README.write_text(BADGE.sub(badge(manifest), text, count=1))
    return "\n".join(lines)


# --- documents: what configops writes and GeoServer shows ---------------------------

# REST paths (after /rest, or /gwc/rest for GeoWebCache) and the documents found there: the
# root (a JSON key or XML tag; a dict when the tag picks the classes; None for any tag, read
# as a class name) and the classes whose fields it holds.
DOCUMENTS = [
    (r"^/workspaces(/[^/]+)?$", "workspace", ["WorkspaceInfoImpl"]),
    (r"^/namespaces(/[^/]+)?$", "namespace", ["NamespaceInfoImpl"]),
    (r"/datastores(/[^/]+)?$", "dataStore", ["StoreInfoImpl", "DataStoreInfoImpl"]),
    (r"/coveragestores(/[^/]+)?$", "coverageStore", ["StoreInfoImpl", "CoverageStoreInfoImpl"]),
    (r"/wmsstores(/[^/]+)?$", "wmsStore", ["StoreInfoImpl", "WMSStoreInfoImpl"]),
    (r"/wmtsstores(/[^/]+)?$", "wmtsStore", ["StoreInfoImpl", "WMTSStoreInfoImpl"]),
    (r"/featuretypes(/[^/]+)?$", "featureType", ["ResourceInfoImpl", "FeatureTypeInfoImpl"]),
    (r"/coverages(/[^/]+)?$", "coverage", ["ResourceInfoImpl", "CoverageInfoImpl"]),
    (r"/wmslayers(/[^/]+)?$", "wmsLayer", ["ResourceInfoImpl", "WMSLayerInfoImpl"]),
    (r"/wmtslayers(/[^/]+)?$", "wmtsLayer", ["ResourceInfoImpl", "WMTSLayerInfoImpl"]),
    (r"^(/workspaces/[^/]+)?/layers(/[^/]+)?$", "layer", ["LayerInfoImpl"]),
    (r"/layergroups(/[^/]+)?$", "layerGroup", ["LayerGroupInfoImpl"]),
    (r"/styles(/[^/]+)?$", "style", ["StyleInfoImpl"]),
    (r"^/settings$", "global", ["GeoServerInfoImpl"]),
    (r"^/workspaces/[^/]+/settings$", "settings", ["SettingsInfoImpl"]),
    (r"^/settings/contact$", "contact", ["ContactInfoImpl"]),
    (r"^/services/wms/(workspaces/[^/]+/)?settings$", "wms", ["ServiceInfoImpl", "WMSInfoImpl"]),
    (r"^/services/wfs/(workspaces/[^/]+/)?settings$", "wfs", ["ServiceInfoImpl", "WFSInfoImpl"]),
    (r"^/services/wcs/(workspaces/[^/]+/)?settings$", "wcs", ["ServiceInfoImpl", "WCSInfoImpl"]),
    (r"^/services/wmts/(workspaces/[^/]+/)?settings$", "wmts", ["ServiceInfoImpl", "WMTSInfoImpl"]),
    (r"^/resource/security/config\.xml$", "security", ["SecurityManagerConfig"]),
    (r"^/resource/security/.+/config\.xml$", None, []),
    (r"^/resource/gwc-gs\.xml$", "GeoServerGWCConfig", ["GWCConfig"]),
    (r"^/gwc/rest/layers/[^/]+$", "GeoServerLayer", ["GeoServerTileLayerInfoImpl"]),
    (r"^/gwc/rest/gridsets/[^/]+$", "gridSet", ["XMLGridSet"]),
    (r"^/gwc/rest/blobstores/[^/]+$", {
        "FileBlobStore": ["BlobStoreInfo", "FileBlobStoreInfo"], "S3BlobStore": ["BlobStoreInfo", "S3BlobStoreInfo"],
        "AzureBlobStore": ["BlobStoreInfo", "AzureBlobStoreInfo"],
        "GoogleCloudStorageBlobStore": ["BlobStoreInfo", "GoogleCloudStorageBlobStoreInfo"]}, []),
]
# Objects inside a document, by the key they sit under, and their classes.
NESTED = {
    "settings": ["SettingsInfoImpl"], "contact": ["ContactInfoImpl"], "jai": ["ImageProcessingInfoImpl"],
    "coverageAccess": ["CoverageAccessInfoImpl"], "watermark": ["WatermarkInfoImpl"], "legend": ["LegendInfoImpl"],
    "attribution": ["AttributionInfoImpl"], "attribute": ["AttributeTypeInfoImpl"],
    "coverageDimension": ["CoverageDimensionImpl"], "metadataLink": ["MetadataLinkInfoImpl"],
    "org.geoserver.catalog.impl.DataLinkInfoImpl": ["DataLinkInfoImpl"], "dimensionInfo": ["DimensionInfoImpl"],
    "bruteForcePrevention": ["BruteForcePreventionConfig"], "gridSubset": ["XMLGridSubset"],
    "styleParameterFilter": ["StyleParameterFilter", "ParameterFilter"],
    "stringParameterFilter": ["StringParameterFilter", "CaseNormalizingParameterFilter", "ParameterFilter"],
    "regexParameterFilter": ["RegexParameterFilter", "CaseNormalizingParameterFilter", "ParameterFilter"],
    "floatParameterFilter": ["FloatParameterFilter", "ParameterFilter"],
    "integerParameterFilter": ["IntegerParameterFilter", "ParameterFilter"],
    "AuthorityURL": ["AuthorityURL"], "Identifier": ["LayerIdentifier"], "ogcapiFeatures": ["FeatureConformance"],
    "defaultValue": ["DimensionDefaultValueSetting"], "expirationRule": ["ExpirationRule"],
}
# Keys a document adds that aren't fields: links, and how lists and maps are wrapped.
WRAPPERS = {"@key", "$", "@class", "entry", "string", "@name", "link", "@rel", "@type", "@xmlns:atom"}


def classes(manifest: dict) -> dict[str, dict]:
    return {cls: fields for page in manifest["pages"].values() for cls, fields in page.items()}


def metadata_name(key: str) -> str:
    """A metadata entry as listed: custom dimensions under one name."""
    if key.startswith(("dim_", "custom_dimension_")):
        return "metadata.dim_<name>"
    return f"metadata.{key}"


def rest_path(url_path: str) -> str:
    """/geoserver-cloud/rest/workspaces/x.json -> /workspaces/x; GeoWebCache's under /gwc/rest."""
    if "/gwc/rest/" in url_path:
        path = "/gwc/rest/" + url_path.split("/gwc/rest/", 1)[1]
    elif "/rest/" in url_path:
        path = "/" + url_path.split("/rest/", 1)[1]
    else:
        return url_path
    return path if path.startswith("/resource/") else re.sub(r"\.(json|xml)$", "", path)


def parse(body) -> dict | None:
    """A request or response body as a dict: JSON as it is, XML as {root tag: {child: ...}}."""
    if isinstance(body, dict):
        return body
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    if not isinstance(body, str) or not body.strip():
        return None
    text = body.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            return None
    if text.startswith("<"):
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return None
        return {tag_name(root.tag): xml_dict(root)}
    return None


def tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml_dict(element):
    """XStream XML in the shape its JSON has: attributes as @name, text beside them as $."""
    out: dict = {f"@{k}": v for k, v in element.attrib.items()}
    children = list(element)
    if not children:
        text = (element.text or "").strip()
        if not out:
            return text
        if text:
            out["$"] = text
        return out
    for child in children:
        key, value = tag_name(child.tag), xml_dict(child)
        if key in out:
            out[key] = out[key] if isinstance(out[key], list) else [out[key]]
            out[key].append(value)
        else:
            out[key] = value
    return out


def norm(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    text = str(value).strip()
    if text.lower() in ("true", "false"):
        return text.lower()
    try:
        return repr(float(text))
    except ValueError:
        return text


def leaves(value) -> frozenset[str]:
    """Every value under a field, without the shape: lists and wrappers differ between JSON and XML."""
    if isinstance(value, dict):
        return frozenset().union(*(leaves(v) for k, v in value.items() if k != "@class" and not is_link(v)))
    if isinstance(value, list):
        return frozenset().union(*(leaves(v) for v in value))
    if value is None or value == "":
        return frozenset()
    return frozenset([norm(value)])


def is_link(value) -> bool:
    """A link GeoServer adds to a listing or another object: into its own REST API."""
    return isinstance(value, str) and value.startswith("http") and "/rest/" in value and value.endswith((".json", ".xml"))


def walk(known: dict, obj: dict, owners: list[str]) -> tuple[dict[str, frozenset], set[str]]:
    """({'Class.field': its values}, {'Class.field' not listed}) for a document's object."""
    values: dict[str, frozenset] = {}
    unlisted: set[str] = set()
    label = "/".join(owners)

    def owner_of(name: str) -> str | None:
        return next((c for c in owners if name in known.get(c, {})), None)

    def add(field: str, found: frozenset):
        values[field] = values.get(field, frozenset()) | found

    for key, value in obj.items():
        if key in WRAPPERS or is_link(value):
            continue
        name = key[1:] if key.startswith("@") else key
        owner = owner_of(name) or owner_of(f"_{name}")
        if key.startswith("@") and not owner:
            continue  # an XML attribute that isn't a field
        if not owner:
            unlisted.add(f"{label}.{name}")
        elif key.startswith("@"):
            add(f"{owner}.{name if name in known[owner] else '_' + name}", leaves(value))
        if key == "metadata" and isinstance(value, dict):
            items = value.get("entry")
            for entry in items if isinstance(items, list) else [items]:
                if not isinstance(entry, dict) or "@key" not in entry:
                    continue
                meta = metadata_name(entry["@key"])
                meta_owner = owner_of(meta)
                if not meta_owner:
                    unlisted.add(f"{label}.{meta}")
                else:
                    add(f"{meta_owner}.{meta}", leaves({k: v for k, v in entry.items() if k != "@key"}))
                for k, v in entry.items():  # an object as the entry's value: dimensionInfo, ogcapiFeatures
                    if k in NESTED and isinstance(v, dict):
                        inner, missing = walk(known, v, NESTED[k])
                        for f, found in inner.items():
                            add(f, found)
                        unlisted |= missing
            continue
        if owner and not key.startswith("@"):
            add(f"{owner}.{name}", leaves(value))
        inner, missing = nested(known, {key: value} if key in NESTED else value)
        for f, v in inner.items():
            add(f, v)
        unlisted |= missing
    return values, unlisted


def nested(known: dict, value) -> tuple[dict[str, frozenset], set[str]]:
    """Objects under a field whose key names their class (contact, attribute, coverageDimension...)."""
    values: dict[str, frozenset] = {}
    unlisted: set[str] = set()
    items = []
    if isinstance(value, list):
        items = [(None, v) for v in value]
    elif isinstance(value, dict):
        items = list(value.items())
    for key, inner in items:
        if key in NESTED:
            for item in inner if isinstance(inner, list) else [inner]:
                if isinstance(item, dict):
                    found, missing = walk(known, item, NESTED[key])
                    for f, v in found.items():
                        values[f] = values.get(f, frozenset()) | v
                    unlisted |= missing
        elif key not in WRAPPERS and isinstance(inner, (dict, list)):
            found, missing = nested(known, inner)
            for f, v in found.items():
                values[f] = values.get(f, frozenset()) | v
            unlisted |= missing
    return values, unlisted


class Evidence:
    """What the e2e tests wrote to GeoServer and read back, in order.

    A field is proved when configops changed it (wrote a value GeoServer didn't already show)
    and GeoServer then showed the value written. A later read showing something else is a
    mismatch: a proved field that mismatches, and isn't proved again, has regressed."""

    def __init__(self, manifest: dict):
        self.known = classes(manifest)
        self.parents = manifest.get("parents") or {}
        self.owners: dict[tuple, list[str]] = {}
        self.shown: dict[tuple, frozenset] = {}
        self.pending: dict[tuple, frozenset] = {}
        self.proved: set[str] = set()
        self.mismatched: dict[str, str] = {}
        self.unlisted: dict[str, str] = {}

    def chain(self, cls: str) -> list[str]:
        out = []
        while cls in self.known or cls in self.parents:
            if cls in self.known:
                out.append(cls)
            cls = self.parents.get(cls, "")
        return out

    def document(self, path: str, body) -> tuple[tuple, list[str], dict] | None:
        doc = parse(body)
        if not isinstance(doc, dict) or len(doc) != 1:
            return None
        root, obj = next(iter(doc.items()))
        if not isinstance(obj, dict):
            return None
        for pattern, want, owners in DOCUMENTS:
            if not re.search(pattern, path):
                continue
            if isinstance(want, dict):
                owners = want.get(root)
            elif want is None:  # a security file: its class, or what was written there
                owners = self.chain(root.rsplit(".", 1)[-1]) or self.owners.get(("file", path))
            elif root != want:
                owners = None
            if not owners:
                return None
            if want is None or path.startswith("/resource/"):
                identity = ("file", path)
            else:
                ws = re.search(r"/workspaces/([^/]+)", path)
                name = obj.get("name") if isinstance(obj.get("name"), str) else path.rsplit("/", 1)[-1]
                ws = ws.group(1) if ws else (name.split(":")[0] if ":" in name else None)
                identity = (pattern, ws, name.split(":")[-1])
            self.owners[identity] = owners
            return identity, owners, obj
        return None

    def write(self, path: str, body) -> None:
        doc = self.document(rest_path(path), body)
        if not doc:
            return
        identity, owners, obj = doc
        values, _ = walk(self.known, obj, owners)
        for field, found in values.items():
            key = (identity, field)
            before = self.shown.get(key)
            if found and (before is None or not found <= before):
                self.pending[key] = found

    def read(self, path: str, body) -> None:
        path = rest_path(path)
        doc = self.document(path, body)
        if not doc:
            return
        identity, owners, obj = doc
        values, unlisted = walk(self.known, obj, owners)
        for field in unlisted:
            self.unlisted.setdefault(field, path)
        for field, found in values.items():
            key = (identity, field)
            self.shown[key] = found
            want = self.pending.get(key)
            if want is None:
                continue
            encrypted = sum(v.startswith(("crypt1:", "crypt2:")) for v in found)  # passwords come back encrypted
            if len(want - found) <= encrypted:
                self.proved.add(field)
                del self.pending[key]
            else:
                self.mismatched.setdefault(field, f"{path}: wrote {sorted(want)}, GeoServer showed {sorted(found)}")


def named_tests(manifest: dict, passed: set[str]) -> set[str]:
    """`yes` fields whose entry names a test (`test: file.py::name`) that passed."""
    return {f"{cls}.{name}" for _, cls, name, e in entries(manifest)
            if e.get("status") == "yes" and e.get("test") and any(n.endswith(e["test"]) for n in passed)}


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "report"
    manifest = refresh() if command == "refresh" else load()
    print(report(manifest, write=command in ("refresh", "report")))
    issues = problems(manifest)
    for issue in issues[:50]:
        print(issue)
    if len(issues) > 50:
        print(f"... and {len(issues) - 50} more")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
