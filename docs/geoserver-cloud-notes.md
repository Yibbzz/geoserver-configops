# GeoServer Cloud: notes from building configops

What we learned about GeoServer Cloud 3.0's REST API, GeoWebCache and the services while building configops. Every point here was found against a real deployment, most of them by an e2e test that failed. If you script GeoServer Cloud yourself, these will save you time. `configops` handles all of them.

## Writing settings and objects

- **A PUT to `/rest/settings` replaces what it leaves out.** A partial PUT unset `charset`, flipped `verbose` and dropped metadata entries. Always GET, merge your change in, and PUT the whole object back. Service settings (`/rest/services/<s>/settings`) are the opposite: a PUT keeps missing fields, so a field is cleared only by sending `""` (or `{"string": []}` for a list).
- **But a PUT replaces some parts whole:** a service's `metadata` map and WFS's `gml` map. Merge by key, or you lose the entries you didn't send (WFS has `maxNumberOfFeaturesForPreview` by default).
- **The global settings' `metadata` is the opposite again:** a PUT without it keeps it; `"metadata": {}` clears it. Write it as `{"entry": [{"@key": "...", "$": "..."}]}` (a `{"map": ...}` body stores an entry named `map`).
- **PUT on a missing object fails.** Look first, then PUT or POST.
- **One-item lists come back as plain values:** `{"string": "x"}`, and a single metadata entry as an object, not a list. Sets come back as `{"@class": "linked-hash-set", "string": ...}`.
- **Numbers and text swap.** Dimension values written as text (`"0"`, `"5"`) come back as numbers; compare them as text.
- **Odd names:** a service's `abstract` is `abstrct`; a layer group's mode "opaque" is `OPAQUE_CONTAINER`; `addressElectronicMailAddress` in the contact isn't kept (use `contactEmail`).
- **Some elements can't be removed once set**, only blanked: a service's `metadataLink`. Its `content` must be a URL.
- **Shapes worth knowing:** `disabledVersions` is `{"org.geotools.util.Version": [{"version": "1.1.1"}]}`; authority URLs `{"AuthorityURL": [...]}`; identifiers `{"Identifier": [...]}`; the OGC API Features conformance classes are a WFS metadata entry `{"@key": "ogcapiFeatures", "ogcapiFeatures": {...}}`; a cascaded WMS layer's `vendorParameters` is `{"entry": [{"@key", "$"}]}`.
- **Store passwords come back encrypted** (`crypt1:...`): never compare them. A cascaded store's `headerValue` and `authKey` come back as written.
- GeoServer adds keywords `["features", <name>]` to feature types and cascaded WMS layers by itself.

## Styles, layers and groups

- Create a style by POSTing the style file itself with `?name=`; JSON style bodies are refused.
- A layer group saved with a member that doesn't exist silently drops it, and then never matches what you asked for.
- A `container` group is a folder in WMS: GetMap on it answers `LayerNotDefined`, which is right. EO groups need a root layer style.
- A layer inside a group is listed under the group in WMS capabilities, not under its own path.
- **Layer group styles don't work.** The catalog database (pgconfig) doesn't keep a group style's members: the REST service shows them from memory until it restarts, then the group can't be read at all (500 `Unexpected item null whose type is not among [LayerInfo, LayerGroupInfo]`). WMS lists the style in capabilities but a GetMap with it answers "No layers have been requested". Changing a feature type the style names breaks the group the same way at once. Writing the group's `layerGroupStyles` again (or an empty list) makes it readable again.
- WMS resolves a style's relative image against its local copy of the resource store. An image uploaded before the style draws at once; one uploaded after the style was used, or changed later, isn't seen until `POST /rest/reset` (which reaches every service). Fonts in the global `styles/` folder need the reset too; `/rest/fonts` lists them by full name ("Noto Sans Regular"), not family.
- FreeMarker templates take effect at once, no reset. They live at `workspaces/<ws>/<store>/<resource>/<file>.ftl` and `workspaces/<ws>/<file>.ftl`.
- Polygon labels need `goodnessOfFit` 0 to draw on small polygons.

## Stores

- The 3.0 images ship GeoTIFF/COG (HTTP, S3, Azure and Google range readers), ImageMosaic, ImagePyramid, WorldImage, ArcGrid, GeoPackage, Shapefile, FlatGeobuf, GeoParquet, PMTiles, property files, SQL Server, app-schema, and WFS, WMS and WMTS cascading. Oracle has only the JNDI factory and no JDBC driver. App-schema is off unless `GEOSERVER_EXTENSION_APPSCHEMA_ENABLED=true`, and it needs an `app-schema-cache` folder beside the mapping (there's no data directory); `list=available` fails for app-schema stores.
- **COG:** a `coverageStore` of type `GeoTIFF` with metadata entry `CogSettings.Key` → `cogSettings.rangeReaderSettings` (`HTTP`, `S3`, `GS`, `AZURE`). The coverage's native name is `geotiff_coverage`.
- A coverage store POST fails ("Store must be part of a workspace") without `workspace: {name}` in the body.
- A WMTS layer's `resource` has no `href`; WMS layers, coverages and feature types have one.
- GeoParquet maps need the GeoParquet 1.1 `bbox` column. CSV stores take `strategy` `guess`, `specify` or `wkt`.
- A fresh cascaded WMS store has `maxConnections` 6, `readTimeout` 60, `connectTimeout` 30 and metadata `useConnectionPooling` true; a fresh cascaded layer has `preferredFormat` `image/png`.
- **Turning connection pooling off** (`useConnectionPooling` false) works only with `GEOTOOLS_HTTPCLIENT_PROXY_ENABLED=false`: the proxy-aware HTTP client (GeoServer Cloud's default) can't do without pooling, and creating a cascaded layer then answers 500 ("Couldn't create HTTP client").
- Big rasters need WMS memory: with a 768 Mi limit, the first GetMap of a 10,980 px Sentinel-2 COG reprojected to EPSG:27700 was OOMKilled once.
- Cascading a slow WFS is slow: the Environment Agency's took 75 s for one 30 km map.

## Tile caching (GeoWebCache)

- Its REST API is at `<geoserver>/gwc/rest` through the gateway, or `/geoserver-cloud/gwc/rest` on the REST service directly. Choose the format with the `Accept` header, not an extension; write XML (its JSON writing fails); keep the `:` in `ws:name` unencoded.
- Its JSON leaves things out (filter types, zoom limits, expiry by zoom, warning skips): read tile layers as XML.
- GeoServer Cloud caches every new layer by itself: to have no cache, delete it.
- `geowebcache-cache-result: HIT` or `MISS` on a WMTS GetTile tells whether a tile came from the cache. GeoWebCache keeps a deleted layer's tiles: truncate before testing.
- Parameter filter values can't contain `%`.
- The caching defaults (`gwc-gs.xml`, through `/rest/resource`) are read only when the services start. GeoServer Cloud sets a clustering lock provider there: leave `lockProviderName` alone.
- Disk quota is switched off (`GWC_DISKQUOTA_DISABLED`) and has no REST endpoint.
- Azurite 3.35 needs `--skipApiVersionCheck` for GeoWebCache's Azure client.

## Security

- `/rest/resource/security/...` reads and writes GeoServer's security files: `config.xml` (filter chains, active role service), and each filter, provider, user/group service, role service and password policy. Password policies apply at once. The rest apply after GeoServer re-reads its security config, which saving an authentication provider (even unchanged) triggers; `/rest/reload` doesn't. Other services follow in about 20 s.
- XStream accepts the configuration class name as a new file's root element, e.g. `org.geoserver.security.config.RequestHeaderAuthenticationFilterConfig`.
- **REST access rules can't be managed through their endpoint:** rules are first-match in file order and the API appends after `/**`, so new ones never apply, and their DELETE refuses the slashes in the path. Edit `security/rest.properties` through `/rest/resource` instead.
- Data and service rules: POST adds (409 if it exists), PUT changes (409 if missing).
- Deleting a missing user, group or role answers 400, not 404.
- GeoServer caches a successful login for minutes: an old password still works just after a change. To check a password, GET `/rest/about/version.json` as the user: 401 means wrong; 403 or 200 means right.
- Brute force prevention exempts 127.0.0.1, so requests through `kubectl port-forward` are never slowed.
- Security changes reach the other services in 20–60 s.
- **The master password race.** REST writes the new keystore (`geoserver.jceks.new`) and tells the other services before it has swapped it in; each service's reload finishes the swap itself. If another service wins, REST logs `cannot delete security/geoserver.jceks` and keeps the old keystore in memory, and every store with a password answers 500 (`Cannot find alias: config:password:key in security/geoserver.jceks`). Have GeoServer re-read its security files after a change (save an authentication provider unchanged), or restart REST: nothing is lost. Right after the change, that save can answer 500 "Cannot save security configuration" for a few seconds while GeoServer reloads by itself: retry.

## Settings with no visible effect, and what to use instead

- **CORS:** the gateway answers it (any origin, from its `gateway.yml`); GeoServer's own CORS setting is read only at startup and never reached. Configure the gateway (`spring.cloud.gateway.server.webmvc.globalcors`, e.g. through `SPRING_APPLICATION_JSON`).
- **Content Security Policy:** the services don't send the header from `security/csp.xml`; changing it changes nothing. The web UI sends Wicket's own.
- **Request logging** (global metadata `logRequestsEnabled`, `logHeadersEnabled`, `logBodiesEnabled`, `requestLogBufferSize`) works, but logs at INFO to `org.geoserver.filters`, which GeoServer Cloud keeps at WARN: set `LOGGING_LEVEL_ORG_GEOSERVER_FILTERS=INFO`. Each line appears twice. The actuator on each pod's port 8081 reads and sets logger levels (`/actuator/loggers/<name>`).
- **URL checks** are on with none defined, so every external URL (a remote SLD, a remote graphic) is refused until a check allows it. `/rest/urlchecks` manages the checks (POST `{"regexUrlCheck": {...}}`; a partial PUT keeps the rest). The switch is `security/urlchecks.xml`; deleting that file doesn't turn checks back on (the last setting stays in memory): write `<checks><enabled>true</enabled></checks>`. A GetMap with `sld=<url>` and no layers shows the effect: refused is "was not accepted by external URL checks".
- **JAI:** `imageIOCache`, `jpegAcceleration`, `allowNativeMosaic`, `allowNativeWarp` aren't kept.
- Workspace service settings exist at `/rest/services/<s>/workspaces/<ws>/settings` (a PUT needs `name`, e.g. `WMS`); GET answers 404 until they do. `/rest/workspaces/<ws>/services/...` has no endpoint.

## Timing

- Other services follow REST's catalog and settings changes over the message bus in about 20 s.
- The tile cache is its own service and starts after REST.

## Data sources (for loading open data)

- Big layers over WFS or OGC API Features don't load reliably: servers that page by `startIndex` get slower the deeper the page, and drop pages. Use a file download when there is one: 415,000 flood zone features never loaded over OGC API; the same data as a 1 GB GeoPackage loaded in 8 minutes.
- GDAL retries HTTP 429, 502, 503 and 504 (`GDAL_HTTP_MAX_RETRY`, `GDAL_HTTP_RETRY_DELAY`), but not broken connections.
- GeoJSON always counts as WGS84.
- data.gov.uk's CKAN API answers plain `curl` with HTML; send an `Accept` header.

## Standard GeoServer, for comparison

configops is also tested against standard GeoServer 3.0.2, the version GeoServer Cloud 3.0 is built on (`tests/e2e/up.sh standard`). What differs:

- Everything above marked as GeoServer Cloud only (the gateway answering CORS, CSP, disk quota, group styles, the master password race) doesn't apply: one process serves REST, the OGC services and the tile cache.
- **A tile layer PUT without `metaWidthHeight` stores 0 × 0** (GeoServer Cloud fills in the caching defaults), and seeding it then fails with `/ by zero`. Always send it.
- A fresh standard GeoServer leaves some settings unset (`resourceErrorHandling`, a WCS `overviewPolicy`): a PUT can't unset them again, and `""` isn't one of their values.
- The official image (`docker.osgeo.org/geoserver`) starts with sample workspaces unless `SKIP_DEMO_DATA=true`; it installs extensions from `STABLE_EXTENSIONS` and `COMMUNITY_EXTENSIONS`, but community builds are snapshots that carry their own copies of core libraries (`gt-main-35-SNAPSHOT` beside `gt-main-35.2`): remove those before starting (`tests/e2e/standard-start.sh`). It runs as root, so files it writes into a mounted folder are root's. Standard GeoServer has no CSV extension: GeoTools' `gt-csv` jar and `opencsv` 5.12 (GeoServer Cloud's) beside the other jars make one. The `oracle` extension includes Oracle's JDBC driver (`ojdbc17`).
- It tells you it's standard GeoServer: `/rest/about/version` says `3.0.2`, GeoServer Cloud `3.0.2-CLOUD`. Security rules can refuse `/rest/about` even to admin (403): treat no answer as standard GeoServer, and ask only when it matters.
- **Group styles:** `layerGroupStyles: {LayerGroupStyle: [{name: "alt", title, abstractTxt, internationalTitle, layers: {published: [{"@type": "layer", name}]}, styles: {style: [{name}]}}]}`. The name is plain text (`<name><name>x</name></name>` is taken but saved empty). WMS lists it as one of the group's styles and draws it for `STYLES=alt`; it survives a restart.
- **CORS:** a global settings metadata entry `{"string": "corsConfigurationSettings", "org.geoserver.security.cors.CORSConfiguration": {enabled, allowedOriginPatterns (comma-separated text), allowedMethods: {string: [...]}, allowedHeaders, maxAge, supportsCredentials}}`. GeoServer reads it only at startup (`CORSInitializer` registers Tomcat's filter); after a restart a preflight from an allowed origin gets the headers, others none.
- **Content Security Policy:** `security/csp.xml` through `/rest/resource`; GeoServer uses a change within about 2 s. `frame-ancestors ${geoserver.csp.frameAncestors}` comes out as `'self' <your value>`.
- **Disk quota:** `/gwc/rest/diskquota` PUT changes it in memory only (`DiskQuotaController.applyDiff`, no save) and ignores `quotaStore`; GET shows quotas as `bytes`, but a PUT must send `<value>` and `<units>` (`B`, `KiB`, ... `TiB`), in that order. What survives a restart is `gwc/geowebcache-diskquota.xml` (root `gwcQuotaConfiguration`, `LayerQuota` entries), read at startup with the quota store. 3.0.2 has HSQL and JDBC quota stores, not H2.
- **Settings that do nothing in 3.0.2:** JAI's `imageIOCache` (kept, never read; the web UI's JAI page no longer has it) and a tile layer's `formatModifiers` (kept, but `GeoServerTileLayer.getFormatModifiers()` always returns none). configops sets neither.

