# What the tool covers

Every page of GeoServer's web UI, and whether `configops` can define it in files and apply it with `plan`/`apply`. The aim is everything that can be configured, as code. Update this page in the same PR as any change to what's covered.

This page is the overview, for GeoServer Cloud; configops works with standard GeoServer too, which can also take the "Standard only" rows below (on GeoServer Cloud `plan` shows them as `!` with the reason, and leaves them out). [geoserver-fields.yaml](geoserver-fields.yaml) goes field by field: every field of GeoServer's configuration, read from GeoServer's and GeoWebCache's source at the versions GeoServer Cloud runs, sorted as here, with `cloud:` or `standard:` where the two differ. [geoserver-tested.txt](geoserver-tested.txt) (GeoServer Cloud) and [geoserver-tested-standard.txt](geoserver-tested-standard.txt) (standard GeoServer) list the fields the e2e tests have proved on each: configops changed the field and GeoServer then showed the new value. The README's figures count only those, out of every setting GeoServer has, so what one can't set counts against it. `tests/geoserver_coverage.py` explains how; `refresh` after a GeoServer Cloud upgrade brings in new fields, and the tests fail until each is sorted.

| Status | Meaning |
| --- | --- |
| **Yes** | In the files: `plan` shows drift, `apply` sets it, `import` writes it |
| **Partly** | Some of it; the notes say what's missing |
| **Not yet** | Possible through GeoServer's REST API; not built |
| **Standard only** | GeoServer Cloud has no way to set it; on standard GeoServer configops does |
| **Deployment** | Set where GeoServer is deployed (its environment, which services run), not in the project files |
| — | Not configuration (a tool or a view of the data) |

What the GeoServer Cloud 3.0 images include decides what's possible: the vector stores (PostGIS, GeoPackage, Shapefile, FlatGeobuf, GeoParquet, PMTiles, property files, Oracle, SQL Server, app-schema), raster formats (GeoTIFF and Cloud-Optimised GeoTIFF over HTTP, S3, Azure and Google Cloud Storage, image mosaics and pyramids, world images, ArcGrid), cascading of remote WFS, WMS and WMTS, CSS and MBStyle styles, vector tiles, OGC API Features, and LDAP, JDBC, auth-key and gateway-shared authentication.

## Data

### Workspaces

| Setting | Status | Notes |
| --- | --- | --- |
| Name | Yes | The project's `workspace`, and any a Layer, LayerGroup or Style names |
| Namespace URI | Yes | `Workspace` `namespace` |
| Default workspace | Yes | `Workspace` `default` |
| Isolated | Yes | `Workspace` `isolated` |
| Workspace settings (contact, proxy, numbers) | Yes | `Settings` with `workspace:` |
| Workspace service settings (its own WMS, WFS…) | Yes | `Service` with `workspace:` |
| Workspace administrators | Yes | `AccessRule` `layer: { workspace, layer: "*", access: admin }` |

### Stores

| Store type | Status | Notes |
| --- | --- | --- |
| PostGIS through the JNDI datasource | Yes | Made for you: one store per schema the layers use |
| PostGIS with a password | Yes | `Store` `type: postgis`, login from a Secret |
| SQL Server | Yes | `Store` `type: sqlserver` (tested by hand against SQL Server 2022; not in CI) |
| Oracle | Standard only | `Store` `type: oracle`, with standard GeoServer's `oracle` extension. GeoServer Cloud's images don't ship Oracle's JDBC driver |
| GeoPackage, Shapefile, directory of shapefiles, FlatGeobuf, PMTiles | Yes | `Store` with `path:` in GeoServer's `/data` folder; PMTiles also over `url:` |
| GeoParquet, property files, CSV | Yes | `Store` `type: geoparquet`, `property`, `csv` (standard GeoServer: add GeoTools' `gt-csv` and `opencsv` jars) |
| Remote WFS (cascaded) | Yes | `Store` `type: wfs` |
| Remote WMS, WMTS (cascaded) | Yes | `Store` `type: wms`, `wmts`; connections, timeouts, an HTTP header, an authentication key |
| Cascaded WMS layer options | Yes | A Layer's `remote`: remote styles and formats, scale range, vendor parameters |
| Auto-disable on connection failure | Yes | `Store` `disableOnConnectionFailure` |
| Cloud-Optimised GeoTIFF (HTTP, S3, Azure, Google) | Yes | `Store` `type: cog` |
| GeoTIFF on disk | Yes | `Store` `type: geotiff` with `path:` |
| Image mosaic, world image, ArcGrid | Yes | `Store` `type: imagemosaic`, `worldimage`, `arcgrid` |
| Image pyramid | Yes | `Store` `type: imagepyramid` |
| App-schema (complex features) | Yes | `Store` `type: appschema`; the extension is off in GeoServer Cloud: `GEOSERVER_EXTENSION_APPSCHEMA_ENABLED=true` |
| Store description, enabled | Yes | For `Store`s; the JNDI PostGIS stores have none |

### Layers

| Tab and setting | Status | Notes |
| --- | --- | --- |
| **Data:** name, title, abstract, keywords | Yes | Keywords with a language and vocabulary: `{ keyword, language, vocabulary }` |
| Enabled, advertised | Yes | |
| Declared CRS | Yes | `crs`, forced on the layer |
| Bounding boxes | Yes | Worked out from the data on every apply and after every load |
| Metadata links | Yes | `metadataLinks` |
| Data links | Yes | `dataLinks` |
| Restrict features (CQL filter) | Yes | `filter` |
| Attributes shown | Yes | `attributes`; `import` tells a narrowed list from the full one by reading the table, so it needs the project's database |
| Attribute customisation: computed or renamed attributes, descriptions, nillable, occurrences | Yes | `attributes: [{ name, source, description, nillable, minOccurs, maxOccurs, binding }]` |
| SQL views | Yes | `sql: { schema, query, key, geometry }` |
| Raster bands and coverage parameters | Yes | `bands`, `parameters` |
| Coverage views (bands of several coverages, or a Jiffle formula) | Yes | A Layer's `view`, with `store:` |
| **Publishing:** default style, other styles | Yes | |
| Queryable | Yes | |
| Opaque | Yes | `opaque` |
| Attribution title | Yes | From the file, or the source's licence for OS data |
| Attribution link and logo | Yes | `attribution: { title, link, logo }` |
| Rendering buffer, HTTP cache headers | Yes | `buffer`, `httpCache` |
| Per-layer WFS settings (max features, decimals, other CRSs, zero padding, no scientific notation, M values) | Yes | `wfs` |
| Services the layer is published in | Yes | `disabledServices` |
| Curved geometries | Yes | `circularArcs`, `linearizationTolerance` |
| KML regionating | Yes | `kml` |
| Authority URLs and identifiers | Yes | `authorityURLs`, `identifiers` |
| Per-layer WMS and WCS settings | Yes | `wms: { path, interpolation }`, `wcs` |
| **Dimensions:** time, elevation | Yes | `dimensions` |
| Default value, nearest match, acceptable interval, start and end | Yes | `dimensions.<name>: { default, nearestMatch, acceptableInterval, nearestFailBehavior, rawNearestMatch, start, end }` |
| Custom dimensions | Yes | Any other name under `dimensions` |
| **Tile caching:** on or off, gridsets, image formats | Yes | `cache:` |
| Metatiling, gutter, expiry | Yes | `cache: { metaTiles, gutter, expireCache, expireClients }` |
| Parameter filters | Yes | `cache.parameterFilters`, with `case` and `locale` to normalise values |
| Published and cached zoom levels, bounds per gridset | Yes | `cache.levels` |
| Expiry by zoom level, warning skips | Yes | `cache.expireByZoom`, `cache.warningSkips` |
| **Security:** who may read, write, administer | Yes | `AccessRule` with `layer:` |

### Layer groups

| Setting | Status | Notes |
| --- | --- | --- |
| Name, workspace, title, abstract, keywords | Yes | |
| Mode (single, named, container, opaque) | Yes | |
| Layers and their styles, in order; groups in groups | Yes | |
| Bounds, CRS | Yes | Worked out from the members |
| Tile caching | Yes | |
| Enabled, advertised, metadata links | Yes | |
| Queryable, attribution, authority URLs and identifiers | Yes | `queryable`, `attribution`, `authorityURLs`, `identifiers` |
| Group styles (other named sets of members and styles) | Standard only | `LayerGroup` `styles`. GeoServer Cloud's catalog database doesn't keep their members: after a restart the group can't be read, and WMS never draws them (docs/geoserver-cloud-notes.md) |
| Earth observation groups (root layer) | Yes | `mode: eo`, `rootLayer`, `rootStyle` |

### Styles

| Setting | Status | Notes |
| --- | --- | --- |
| SLD, CSS, MBStyle files | Yes | Files in `styles/`, uploaded as written |
| Workspace styles | Yes | |
| Global styles | Yes | `Style` `global: true`; GeoServer's built-in ones are used without defining them |
| Legend graphic | Yes | `Style` `legend` |
| Images a style points to (icons, fills) | Yes | `.png`, `.svg`, `.jpg`, `.gif` in `styles/` |
| Fonts | Yes | `.ttf`, `.otf` in `styles/` |
| FreeMarker templates (GetFeatureInfo and others) | Yes | `templates/` for the workspace, `templates/<layer>/` for one layer; not for a store or global |

### Loading data

| What | Status | Notes |
| --- | --- | --- |
| Vector data from Ordnance Survey, CKAN portals, WFS, OGC API Features, links | Yes | `Dataset`: staged, checked, backed up, swapped in, on a schedule |
| Raster data | Yes | A `Dataset` with `raster:` makes one Cloud-Optimised GeoTIFF on the data volume (OS, CKAN, links), backed up and swapped in; a `Store` with `dataset:` publishes it |
| Tables you load yourself | Yes | A Layer with `table:` and no `dataset`; it waits until the table exists |
| The importer extension | — | `Dataset` does this job |

## Services

| Page | Status | Notes |
| --- | --- | --- |
| WMS, WFS, WCS, WMTS | Yes | `Service`: every field on the page, plus `geoserver:` for others |
| WPS | Deployment | GeoServer Cloud's WPS service, when deployed |
| OGC API Features | Yes | The WFS `Service`'s `metadata.ogcapiFeatures`: which conformance classes it offers |
| WMS options: KML, SVG, PNG/JPEG compression, projection handling, map wrapping, root layer, authority URLs | Yes | The WMS `Service`'s `metadata`, `authorityURLs`, `identifiers` |
| GML per WFS version, allowed output formats, disabled versions, metadata link | Yes | `Service` `gml`, `getFeatureOutputTypes`, `getMapMimeTypes`, `disabledVersions`, `metadataLink` |
| Turning a service on or off | Yes | `enabled:` in a `Service`; whether GeoServer Cloud runs the service at all is up to its deployment |

## Settings

| Page | Status | Notes |
| --- | --- | --- |
| Global | Yes | `Settings`: proxy address, decimals, charset, verbose, error handling, web UI mode and the rest |
| Contact information | Yes | `Settings` `contact:` |
| Translations (titles, abstracts, contact details in other languages) | Yes | `internationalTitle`, `internationalAbstract` on layers, groups and services; `contact.international*` |
| JAI, coverage access | Yes | `Settings` `global.jai`, `global.coverageAccess`. Not JAI's *Image I/O caching*: GeoServer 3.0 keeps it but nothing reads it, and its web UI no longer shows it |
| Image processing | Yes | As JAI |
| REST: upload folder, quiet on not found | Yes | `Settings` `restUploadRoot`, `restQuietOnNotFound` |
| How the web UI shows users | Yes | `Settings` `global.userDetailsDisplaySettingsInfo` |
| Logging | Deployment | GeoServer Cloud logs through Spring Boot, set in its environment |
| Request logging | Yes | `Settings` `global.requestLogging`: each request (and its headers and body) in the services' logs; GeoServer Cloud needs the logger (`org.geoserver.filters`) to INFO |

## Tile caching

| Page | Status | Notes |
| --- | --- | --- |
| Tile layers | Yes | Each layer's and group's `cache:`. Not format modifiers: GeoServer keeps them, but its tile layers never use them |
| Caching defaults | Yes | `Settings` `global.tileCaching`, with multi-layer caching, metatiling and seeding threads; GeoServer reads them when its services start |
| Gridsets (your own) | Yes | `Gridset`, e.g. British National Grid; description, level names, pixel size, axis order |
| Disk quota | Standard only | `Settings` `global.diskQuota`. GeoServer Cloud switches it off (`GWC_DISKQUOTA_DISABLED`), has no REST endpoint for it (404), and would read its settings from each pod's own folder |
| Blob stores: a folder, S3 | Yes | `BlobStore` `type: file` (on the data volume) or `s3`; `cache.blobStore` |
| Blob stores: Azure, Google Cloud Storage | Yes | `BlobStore` `type: azure`, `gcs` |
| Blob store options: TMS layout, proxies, quota project | Yes | `layout`, `proxy`, `quotaProject` |
| Seeding, truncating | Yes | `cache.seed`: after apply changes a layer and after each load, which also clears the old tiles |

## Security

| Page | Status | Notes |
| --- | --- | --- |
| Users, groups, roles | Yes | `User`, `Group`, `Role`, in GeoServer's default user/group and role services; passwords from Secrets; a role's parent and properties, a user's properties, a group turned off |
| Data rules | Yes | `AccessRule` `layer:` |
| Service rules | Yes | `AccessRule` `service:` |
| REST rules | Yes | `AccessRule` `rest:`, kept in order |
| Catalog mode | Yes | `Settings` `global.catalogMode` |
| Settings (active role service, encryption) | Yes | `Settings` `global.security` |
| Authentication: providers, filters, brute force prevention | Yes | `AuthProvider`, `AuthFilter`, `global.security.authProviders`, `bruteForcePrevention` |
| Filter chains, role services | Yes | `global.security.filterChains`, `RoleService`: written as GeoServer's own files through `/rest/resource` |
| Other user/group and role services (LDAP, JDBC) | Yes | `UserGroupService`, `RoleService`, `AuthProvider` with their class; tested: logins through an LDAP directory and a JDBC user database |
| Master password | Yes | `Settings` `global.masterPassword` from a Secret; `import` doesn't write it. Providers: `MasterPasswordProvider` |
| Remember-me | Yes | `Settings` `global.security.rememberMe` |
| Password policies | Yes | `PasswordPolicy` |
| URL checks | Yes | `URLCheck` (a regular expression a URL may match), `Settings` `global.urlChecks` for the switch. GeoServer Cloud has them on with no checks, so every external URL (a remote style, graphic) is refused until a check allows it |
| Content Security Policy | Standard only | `Settings` `global.contentSecurityPolicy`. GeoServer Cloud's services don't send the header from GeoServer's policy (`security/csp.xml`): changing it changes nothing. The web UI sends its own |
| CORS | Deployment; standard: Yes | GeoServer Cloud's gateway answers it (README). Standard GeoServer: `Settings` `global.cors`, used after a restart |
| GeoServer ACL | Deployment | GeoServer Cloud's ACL service, when deployed |

## Workflow

| What | Status | Notes |
| --- | --- | --- |
| Export a live GeoServer to files | Yes | `configops import <workspace> --settings --security` |
| Bring changes made in the web UI back into the files | Yes | `import` in a project folder updates only what changed, keeping names, order and comments; `--prune` removes what GeoServer no longer has |
| Drift detection | Yes | `plan` (exit code 2), run on a schedule (docs/kubernetes.md has an hourly CronJob) |
| Promotion between environments | Yes | The same project files applied to each environment: its address with `--geoserver`, its logins from its own Secrets |

## Not configuration

Layer preview, demos, server status, the reprojection console and the importer's web pages are tools, not settings: nothing to keep in files.

## Deployment

How GeoServer itself is deployed (which services run, their images, memory, health checks, the database and the JNDI datasource) is outside configops: it manages what is in a running GeoServer. [docs/kubernetes.md](kubernetes.md) runs configops itself beside it.
