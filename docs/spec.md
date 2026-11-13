# GeoServer ConfigOps files (`configops/v1alpha1`)

A set of YAML files that says what a GeoServer or GeoServer Cloud publishes and how it is set up: workspaces, stores, layers, styles, groups, tile caching, settings, services and security. `configops` reads the files and makes GeoServer match them, the way OpenTofu makes infrastructure match its configuration.

> **Status:** every command below is built. Loading data into PostGIS is a separate tool, [geodata-sync](https://github.com/Yibbzz/geodata-sync); configops publishes the tables, whoever loads them.

## Principles

1. **The files hold what you chose, nothing else.** Anything GeoServer or the data can work out (projection definitions, bounding boxes, attribute lists, links, timestamps) is never written in the files. The tool fills it in when it applies them.
2. **Plan, then apply.** `configops plan` shows what would change; `configops apply` makes exactly those changes, in dependency order, and running it twice changes nothing the second time.
3. **No state file.** The live GeoServer (and whether a layer's table exists in PostGIS) is the state. Drift (a change made in the web UI) shows up in the next `plan`.
4. **Secrets by reference.** API keys and passwords never appear in the files, only the name of the secret that holds them.
5. **Shaped like Kubernetes resources** (`apiVersion`, `kind`, `metadata`, `spec`), so the same files could later drive a Kubernetes operator instead of the CLI.
6. **Small.** GeoServer calls go through a thin REST client of its own; nothing else is needed but Python.

## How it fits together

```
 config files (git) ──► configops plan / apply ──► GeoServer catalog: workspaces, stores, layers, styles, tile cache,
                                                    settings, services, security
 new data loaded (geodata sync, or your own) ──► configops refresh ──► layers' bounds worked out again, cached tiles cleared
```

`apply` manages configuration only: a layer on a table that doesn't exist yet is shown as `!` and published once it does.

## Files

The tool reads every `*.yaml` file under one directory. A file can hold several resources separated by `---`; how you split them into files is up to you. A typical layout:

```
uk-basemap/
  project.yaml              # one Project
  layers/
    os-open-roads.yaml      # Layers
    survey-sites.yaml
  server.yaml               # Settings and Services: contact details, service titles, limits
  security.yaml             # Roles, Groups, Users and AccessRules
  styles/
    os-road-link.sld        # style files, picked up by name
    os-road-node.sld
    icons/church.svg        # images a style points to (icons/church.svg), and fonts (.ttf, .otf)
  templates/
    header.ftl              # FreeMarker templates for the workspace (GetFeatureInfo and others)
    road-links/content.ftl  # ... and for one Layer, in a folder named after it
```

Every resource has the same outer shape:

```yaml
apiVersion: configops/v1alpha1
kind: Layer                 # Project, Store, Layer, LayerGroup, Style, Settings, Service, Role, Group, User, AccessRule, ...
metadata:
  name: road-links          # unique per kind; lowercase letters, digits and dashes
spec: {}                    # what you want; differs per kind
```

There is no `status` in the files. Status is what `configops status` reports from the live system.

## Kinds

### Project

One per directory. Says which GeoServer to manage, where its layers' tables are, and the defaults for everything else.

```yaml
apiVersion: configops/v1alpha1
kind: Project
metadata:
  name: uk-basemap
spec:
  geoserver:
    url: http://localhost:8080/geoserver           # or https://<host>/geoserver-cloud
    credentials: { secret: geoserver-admin }       # keys: username, password
  database:
    jndi: java:comp/env/jdbc/mypostgis             # how GeoServer reaches PostGIS (its JNDI datasource)
    connection: { secret: database }               # how configops reaches it: does a layer's table exist yet?
    schema: data_layers                            # where `table: <name>` is
  workspace: uk                                    # default workspace for layers and styles
  crs: EPSG:27700                                  # the CRS import leaves out when it's this
```

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `geoserver.url` | yes | | Base URL of GeoServer (gateway) |
| `geoserver.credentials.secret` | yes | | Secret with `username` and `password` |
| `database` | for layers on tables or SQL views | | The PostGIS database GeoServer reaches through a JNDI datasource |
| `database.jndi` | with `database` | | JNDI name GeoServer uses for the data store |
| `database.connection.secret` | with `database` | | Secret with `host`, `port`, `database`, `username`, `password` |
| `database.schema` | no | `public` | The schema of a layer's `table` given by name alone |
| `workspace` | yes | | Default workspace; the tool owns it (see [Ownership](#ownership-and-pruning)) |
| `crs` | no | | The project's usual CRS |

### Layer

How data is published in GeoServer. A layer is on a table, on a SQL view (`sql:`), or on a `Store` (below).

```yaml
apiVersion: configops/v1alpha1
kind: Layer
metadata:
  name: road-links
spec:
  table: os_open_roads_road_link     # in the Project's database schema
  title: OS Open Roads, road links
  abstract: Roads in Great Britain from OS Open Roads.
  style: os-road-link                # default style
  styles: [line]                     # extra styles clients may ask for
  cache: true                        # tile caching in GeoWebCache
---
apiVersion: configops/v1alpha1
kind: Layer
metadata:
  name: survey-sites
spec:
  table: { schema: field_data, name: sites }   # in another schema
  title: Survey sites
  style: point
---
apiVersion: configops/v1alpha1
kind: Layer
metadata:
  name: big-counties
spec:
  sql:                               # a SQL view
    schema: data_layers
    query: |
      select fid, name, geom from data_layers.boundary_line_county
      where hectares > 300000
    key: fid
    geometry: { column: geom, type: MultiPolygon, crs: EPSG:27700 }
  attributes: [name]                 # publish only these (and the geometry)
```

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `table` | one of the forms | | A table: its name in the Project's database schema, or `{ schema, name }` |
| `store` + `resource` | one of the forms | `resource`: the store's only one | Something a `Store` offers (below) |
| `sql` | one of the forms | | A SQL view: `{ schema, query, key, geometry: { column, type, crs } }`, published from the schema's store |
| `name` (in GeoServer) | no | `metadata.name` with dashes as underscores | Layer name clients use |
| `crs` | no | the data's own | The declared CRS, forced on the layer |
| `attributes` | no | all | The attributes published, in this order; the geometry always is. Each is a column's name, or `{ name, source, description, nillable, minOccurs, maxOccurs, binding, range, options }` (`range: [min, max]` and `options` restrict the values allowed): `source` is a CQL expression the attribute is worked out from (`strToUpperCase(kind)`; another column's name renames it), `binding` its Java class (`java.lang.String`) |
| `workspace` | no | the project's `workspace` | |
| `title`, `abstract`, `keywords` | no | title = name | Shown in capabilities documents. A keyword is text, or `{ keyword, language, vocabulary }` (also in a `Service`) |
| `internationalTitle`, `internationalAbstract` | no | none | The title and abstract in other languages, `{ cy: Mapiau, fr: Cartes }`: clients that ask for a language (`AcceptLanguages=cy`) get it. GeoServer can't drop a language once set, only blank it |
| `style` | no | GeoServer's default for the geometry | Default style |
| `styles` | no | none | Extra styles |
| `enabled`, `advertised`, `queryable` | no | `true` | Publishing switches |
| `cache` | no | `false` | `true`, or `{ gridsets, formats, metaTiles: [w, h], gutter, expireCache, expireClients }`: the gridsets (GeoWebCache's own, or a `Gridset`), image formats, tiles drawn at once, pixels drawn around each tile, how long (seconds) the cache and clients keep a tile, and `blobStore`: the `BlobStore` the tiles go in. `parameterFilters` and `seed`: see below. Only the options set are compared |
| `attribution` | no | none | Text clients must show, e.g. "Contains OS data © Crown copyright and database right 2026"; or `{ title, link, logo: { url, width, height, type } }` |
| `filter` | no | none | A CQL filter: only these features are published, e.g. `"flood_zone = 'FZ3'"` |
| `wfs` | no | the WFS service's | `{ maxFeatures, decimals, skipNumberMatched, otherCRS: [EPSG:4326, …], padWithZeros, forcedDecimal, encodeMeasures }`: this layer's WFS settings (`padWithZeros` writes decimals up to `decimals`, `forcedDecimal` never writes scientific notation, `encodeMeasures` writes M values, `simpleConversion` also serves an app-schema layer as simple features) |
| `disabledServices` | no | none | Services this layer isn't published in: `wms`, `wfs`, `wcs`, `wmts`, `wps` |
| `circularArcs`, `linearizationTolerance` | no | `false`, none | The geometries have curves; how closely they're turned into lines, in the layer's units |
| `kml` | no | none | KML regionating: `{ regionateAttribute, regionateStrategy: best_guess \| external-sorting \| geometry \| native-sorting \| random, regionateFeatureLimit }` |
| `authorityURLs`, `identifiers` | no | none | `[{ name, href }]` and `[{ authority, identifier }]`, shown in WMS capabilities |
| `metadataLinks` | no | none | `[{ url, type, standard }]`: links to metadata records, shown in capabilities. `type` defaults to `text/html`, `standard` to `ISO19115:2003` |
| `httpCache` | no | none | `{ maxAge }`: a `Cache-Control: max-age` (seconds) on this layer's maps |
| `dimensions` | no | none | `{ time: {…}, elevation: {…}, <any other name>: {…} }`, each `{ attribute, endAttribute, presentation: list \| continuous \| interval, resolution, units, unitSymbol, nearestMatch, acceptableInterval, nearestFailBehavior: IGNORE \| EXCEPTION, rawNearestMatch, start, end, default: { strategy: MINIMUM \| MAXIMUM \| NEAREST \| FIXED \| BUILTIN, value } }` (`acceptableInterval`: how far a nearest match may be, e.g. `PT1H`; `rawNearestMatch`: nearest match in WCS too; `start`, `end`: the values to offer; `default`: the value a request that names none gets, `value` for `NEAREST` and `FIXED`). Another name is a custom dimension, which clients ask for as `DIM_<name>` |
| `opaque` | no | `false` | Tells clients nothing shows through the layer |
| `buffer` | no | none | Rendering buffer in pixels, for symbols that spill over tile edges |
| `wms` | no | none | `{ path, interpolation }`: where the layer sits in the WMS capabilities tree (`/base/roads`), and `nearest`, `bilinear` or `bicubic` resampling |
| `dataLinks` | no | none | `[{ url, type }]`: links to the data itself. `type` defaults to `text/html` |
| `view` | no | none | With `store:` instead of `resource:`, a coverage view: a raster made from bands of the store's coverages. `{ bands: [{ coverage, band }], envelope: intersection \| union, resolution: best \| worst, resolutionIndex, fillMissingBands, formula: { script, output } }`: `formula` is a Jiffle script computing one band (`doubled = dem[0] * 2;`), `output` the band it writes |
| `remote` | no | none | A cascaded WMS layer's options: `{ style, styles, format, formats, minScale, maxScale, respectBounds, vendorParameters }`: the remote style to draw with and the ones offered, the image format to ask for and the ones offered, the scale range, asking only within the layer's bounds, and extra request parameters |
| `wcs` | no | the WCS service's | Rasters: `{ requestCRS, responseCRS, formats, interpolations, defaultInterpolation }` |
| `bands` | no | from the file | Rasters: `[{ name, description, unit, nullValues: [...], range: [min, max] }]`, in band order, as WCS shows them |
| `parameters` | no | none | Rasters: the reading parameters of the web UI's *Coverage Parameters*, e.g. `{ InputTransparentColor: "#000000" }` |

Every field above works the same on a layer on a `Store`, except `filter` and `wfs`, which are for feature types, and `wcs`, `bands` and `parameters`, which are for rasters. Only the fields a file sets are compared; GeoServer's own metadata entries on a layer are kept. Not in the file, worked out on apply: native and lat/lon bounding boxes (computed from the table), attribute list and types (from the table), projection definition (from the CRS), store, links, timestamps.

**Parameter filters and seeding.** A request parameter makes tiles of its own only if the layer's cache has a filter for it:

```yaml
  cache:
    gridsets: [EPSG:900913]
    parameterFilters:
      CQL_FILTER: { type: values, values: [INCLUDE, "hectares > 300000"], default: INCLUDE }
      TIME: { type: regex, regex: "[0-9]{4}-[0-9]{2}-[0-9]{2}" }
      ELEVATION: { type: numbers, values: [0, 10.5], threshold: 0.01 }    # or integers
      STYLES: { type: styles, values: [polygon, outline] }                 # which of the layer's styles
      ENV: { type: values, values: [A, B], case: upper, locale: en }       # values made upper case first
    seed: { zoom: [0, 8] }             # also gridsets, formats, bounds [minx, miny, maxx, maxy], threads
    levels: { EPSG:900913: { published: [0, 18], cached: [0, 14], bounds: [-20000, 6600000, 30000, 6700000] } }
    expireByZoom: { 0: 86400, 12: 3600 }   # from zoom 12 on, tiles last an hour
    warningSkips: [Default, Nearest]       # say so in a header when a dimension value stops a tile being cached
```

GeoWebCache adds a `STYLES` filter (any of the layer's styles) by itself, so leaving it out means that one. A request with a value no filter allows is refused. GeoWebCache can't take a `%` in a value. `seed` draws those tiles ahead of time, in the background: after `apply` changes the layer, and after each load clears its tiles. Whether tiles are seeded isn't compared. `levels` limits a gridset: zoom levels outside `published` are refused, and those outside `cached` are drawn each time, never kept. `case` (`upper`, `lower`) applies to `values` and `regex` filters.

### Store

Data GeoServer reads where it is, rather than a table configops loads: a Cloud-Optimised GeoTIFF over HTTP or object storage, a remote WMS, WMTS or WFS to cascade, a PostGIS database reached with a password, or files on the shared data volume. A `Layer` with `store:` publishes one thing from it.

```yaml
apiVersion: configops/v1alpha1
kind: Store
metadata:
  name: sentinel-2-surrey
spec:
  type: cog                                 # a Cloud-Optimised GeoTIFF
  url: https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/30/U/XC/2025/8/S2C_30UXC_20250825_0_L2A/TCI.tif
  description: Sentinel-2 true colour, 25 Aug 2025
---
apiVersion: configops/v1alpha1
kind: Store
metadata:
  name: eox
spec:
  type: wmts                                # a remote WMTS, cascaded
  url: https://tiles.maps.eox.at/wmts/1.0.0/WMTSCapabilities.xml
---
apiVersion: configops/v1alpha1
kind: Store
metadata:
  name: warehouse
spec:
  type: postgis                             # another PostGIS database
  connection: { host: db.example.org, port: 5432, database: gis, schema: public }
  credentials: { secret: warehouse-login }  # keys username and password
---
apiVersion: configops/v1alpha1
kind: Layer
metadata:
  name: sentinel-2
spec:
  store: sentinel-2-surrey                  # its only coverage, so no resource needed
  title: Sentinel-2 true colour
---
apiVersion: configops/v1alpha1
kind: Layer
metadata:
  name: cloudless
spec:
  store: eox
  resource: s2cloudless-2024_3857           # which of the store's layers
```

| `type` | `url` | Publishes | Notes |
| --- | --- | --- | --- |
| `cog` | The file: `https://`, `s3://`, `gs://` or `az://` | Its coverage | Read in ranges, never downloaded whole. Big rasters need memory in the WMS service: one first request for a 10,980 × 10,980 image went over the default 768 Mi once |
| `wms` | The capabilities address | A remote layer, drawn by the remote server | |
| `wmts` | The capabilities address | A remote tile layer | |
| `wfs` | The capabilities address | A remote feature type, as if it were local | Only as fast as the remote server: one 30 km map from the EA's WFS took 75 s |
| `postgis` | (none: `connection`) | A table | `connection`: `host`, `port`, `database`, `schema` and any other parameter as GeoServer names it |
| `geopackage`, `flatgeobuf` | (none: `path`) | A table or layer in the file | |
| `shapefile` | (none: `path`) | The shapefile | `path` is the `.shp` |
| `shapefiles` | (none: `path`) | One of the folder's shapefiles | `path` is a folder |
| `pmtiles` | `path`, or `url` (`https://`, `s3://`) | A layer of the vector tiles | |
| `geotiff`, `worldimage`, `arcgrid` | (none: `path`) | The raster | A COG on the volume is a `geotiff` |
| `imagemosaic` | (none: `path`) | The mosaic of a folder's rasters | GeoServer writes its index into the folder |
| `imagepyramid` | (none: `path`) | A raster at several resolutions | A folder as `gdal_retile.py -levels N` makes it, with the full-size tiles moved into `0/` |
| `geoparquet` | (none: `path`) | The file's features | GeoParquet 1.1 with its `bbox` column (GDAL 3.9 or later writes it): without it, the file publishes but maps and bounding box queries fail |
| `property` | (none: `path`) | One of the folder's `.properties` files | GeoServer's own simple text format |
| `csv` | (none: `path`) | The file's rows as features | `connection: { strategy: specify, latField, lngField }`, `{ strategy: wkt, wktField }`, or `{ strategy: guess }` (columns named lat and lon); points in EPSG:4326. Standard GeoServer has no extension for it: add GeoTools' `gt-csv` and `opencsv` jars (`tests/e2e/standard-start.sh` does) |
| `sqlserver` | (none: `connection`) | A table | As `postgis`; `credentials` for the login |
| `oracle` | (none: `connection`) | A table | Standard GeoServer only, with its `oracle` extension (Oracle's JDBC driver is in it). As `postgis`; `database` is the SID, or `/<service name>` (`/FREEPDB1`) |
| `appschema` | (none: `path`) | A complex feature type the mapping file defines | The mapping and its schema on the data volume; the layer names its feature type in `resource`. GeoServer Cloud ships the extension off: set `GEOSERVER_EXTENSION_APPSCHEMA_ENABLED=true` on its services. `apply` makes an `app-schema-cache` folder beside the mapping (when the data volume is mounted where it runs), where GeoServer keeps the schemas it downloads; the workspace's namespace URI must be the mapping's target namespace (`Workspace` `namespace`) |

**Files** are read from GeoServer's `/data` folder: a volume mounted there in every GeoServer service. `path` is under it: `path: rasters/surrey.tif` is `/data/rasters/surrey.tif`. How files get there is up to you, or a raster Dataset makes them (above); `configops import` writes stores whose files are under `/data`.

| `name`, `workspace` | `metadata.name` with dashes as underscores; the project's workspace | |
| `description`, `enabled` | none; `true` | |
| `disableOnConnectionFailure` | `false` | GeoServer turns the store off when it can't connect |
| `maxConnections`, `readTimeout`, `connectTimeout`, `connectionPooling` | 6, 60 s, 30 s, `true` | `wms`, `wmts`: connections to the remote server at once, timeouts in seconds, reusing connections. `connectionPooling: false` needs `GEOTOOLS_HTTPCLIENT_PROXY_ENABLED=false` on the GeoServer services, as the chart sets it: GeoServer Cloud's proxy-aware HTTP client can't do without pooling, and creating a layer then fails ("Couldn't create HTTP client") |
| `header`, `authKey` | none | `wms`, `wmts`: an HTTP header sent with every request (`{ name, value }`, or `{ name, secret, key }` for the value from a Secret) and an authentication key (text, or `{ secret, key }`). GeoServer keeps them as they are, so they're compared; `import` names Secrets for them |

A `Layer` on a store takes `store` and `resource` (the name the store gives it: a coverage, remote layer or table) instead of `table`; `resource` can be left out when the store offers only one thing. Everything else about the layer (title, styles, cache, attribution, access rules) works as for any layer. A store the files don't name, in a workspace the project owns, is unmanaged and removed only with `--prune`.

### Style

Style files are picked up from `styles/` by file name: `styles/os-road-link.sld` is the style `os-road-link`. A `Style` resource is only needed to override that:

```yaml
apiVersion: configops/v1alpha1
kind: Style
metadata:
  name: os-road-link
spec:
  file: styles/os/road-link-detailed.sld
  workspace: uk                       # default: the project's workspace
---
apiVersion: configops/v1alpha1
kind: Style
metadata:
  name: house-colours
spec:
  file: styles/global/house-colours.sld   # not in styles/ itself, which holds the workspace's
  global: true                        # a global style, in no workspace; layers name it without one
```

A Style resource can also give a legend graphic of its own, which GetLegendGraphic serves and capabilities point to: `legend: { url, width, height, format }`.

Formats: SLD (`.sld`) and MBStyle (`.mbstyle`); CSS (`.css`) if the GeoServer images include it and the client gains support. Files are uploaded exactly as written, never re-encoded by GeoServer.

#### Images and fonts

Images under `styles/` (`.png`, `.svg`, `.jpg`, `.jpeg`, `.gif`) are uploaded beside the styles, at the same path, so a style points to them relative to `styles/`: `styles/icons/church.svg` is `xlink:href="icons/church.svg"`, whatever folder the style file is in. They go to every workspace the project has styles in, and to GeoServer's global styles folder too if it has a global style. Fonts under `styles/` (`.ttf`, `.otf`) go to GeoServer's global styles folder, where GeoServer looks for them; labels name the font's full name as GeoServer lists it (`/rest/fonts`), such as `Noto Sans Regular`.

`plan` shows them as `file` lines, compared byte for byte, before the styles that use them:

```
+ file workspaces/uk/styles/icons/church.svg   (styles/icons/church.svg)
+ file styles/ordnance.ttf                     (styles/ordnance.ttf)
```

GeoServer's services keep images and fonts in memory, so `apply` has them all read the files again after the last upload (`POST /rest/reset`); a changed image is drawn on the next request.

### Templates

FreeMarker templates shape what GeoServer writes for GetFeatureInfo (`header.ftl`, `content.ftl`, `footer.ftl`) and other outputs. `templates/*.ftl` are the project workspace's; `templates/<layer>/*.ftl` are one Layer's, in a folder named after the Layer (`metadata.name`). `validate` reports any other file or folder there. They are not a kind: the file is the template, written as it is.

```
+ template uk header.ftl                (templates/header.ftl)
~ template uk:road_links content.ftl    (templates/road-links/content.ftl differs from GeoServer's copy)
```

Templates take effect on the next request. A layer's template is written after the layer is made. Templates for a store, or global ones, aren't supported.

#### Scale limits live in styles

At which scales a feature is drawn is part of how it is represented, so it belongs in the style, not on the layer. A style can also give each kind of feature its own range, which one setting on a layer can't. The OS road style does exactly that, one range per road class:

```xml
<Rule>
  <Name>Unclassified</Name>
  <ogc:Filter>…function = 'Unclassified'…</ogc:Filter>
  <MinScaleDenominator>250</MinScaleDenominator>       <!-- drawn from 1:250 -->
  <MaxScaleDenominator>100000</MaxScaleDenominator>    <!-- up to 1:100,000 -->
  <LineSymbolizer>…</LineSymbolizer>
</Rule>
```

In MBStyle the same thing is `minzoom` and `maxzoom` on each layer of the style. `Layer` has no scale fields.

### LayerGroup

Several layers published together, in drawing order, under one name: a basemap, or a tree of layers for clients to browse.

```yaml
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata:
  name: uk-basemap
spec:
  title: UK basemap
  abstract: Counties and roads from OS OpenData.
  mode: single                       # one map layer clients can ask for in one request
  layers:                            # drawn in this order: first at the bottom
    - counties
    - road-links
    - { layer: road-nodes, style: os-road-node }
  cache: true
---
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata:
  name: os-opendata
spec:
  title: OS OpenData
  mode: container                    # a folder in clients' layer lists, not a map of its own
  layers:
    - uk-basemap                     # groups can hold groups
    - survey-sites
```

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `layers` | yes | | Layers or groups, bottom first. An entry is a name, or `{ layer: <name>, style: <style> }` to draw a layer with a style other than its default |
| `mode` | no | `single` | How clients see the group (below) |
| `name` (in GeoServer) | no | `metadata.name` with dashes as underscores | Name clients use |
| `workspace` | no | the project's `workspace` | |
| `title`, `abstract`, `keywords` | no | title = name | Shown in capabilities documents. A keyword is text, or `{ keyword, language, vocabulary }` (also in a `Service`) |
| `internationalTitle`, `internationalAbstract` | no | none | The title and abstract in other languages, `{ cy: Mapiau, fr: Cartes }`: clients that ask for a language (`AcceptLanguages=cy`) get it. GeoServer can't drop a language once set, only blank it |
| `cache` | no | `false` | Tile caching for the group as a whole, in the same form as a Layer's |
| `enabled`, `advertised` | no | `true` | An unadvertised group works but isn't listed in capabilities |
| `metadataLinks` | no | none | As on a Layer |
| `attribution`, `authorityURLs`, `identifiers` | no | none | As on a Layer |
| `queryable` | no | `true` | Whether GetFeatureInfo answers for the group |
| `rootLayer`, `rootStyle` | `eo` only | the root layer's own style | The layer drawn for the group as a whole |
| `styles` | no | none | Standard GeoServer only. Named styles of the group: each `{ name, title, abstract, internationalTitle, internationalAbstract, layers }` is another set of layers (in the same form as `layers`) that WMS draws for `STYLES=<name>`, and lists as one of the group's styles |

| `mode` | GeoServer mode | Clients see |
| --- | --- | --- |
| `single` | SINGLE | One layer that draws all members; the members are still published on their own |
| `named` | NAMED | The group as a layer, with its members listed under it |
| `container` | CONTAINER | A folder of its members; the group itself can't be requested |
| `opaque` | OPAQUE_CONTAINER | One layer; its members are hidden from capabilities |
| `eo` | EO | Earth observation: the `rootLayer` drawn for the group, its members (products, masks) listed under it |

Not in the file, worked out on apply: the group's bounding box (from its members) and its projection.

### Gridset

A tile grid of your own for GeoWebCache: GeoServer only has web mercator and latitude/longitude ones (and UTM zones), so for tiles on British National Grid:

```yaml
apiVersion: configops/v1alpha1
kind: Gridset
metadata:
  name: british-national-grid
spec:
  crs: EPSG:27700
  extent: [-238375, 0, 900000, 1376256]   # OS's own tile grid
  resolutions: [896, 448, 224, 112, 56, 28, 14, 7, 3.5, 1.75, 0.875, 0.4375, 0.21875, 0.109375]
  tileSize: 256                           # the default
```

A layer's `cache.gridsets` names it. `scales` (scale denominators) can stand instead of `resolutions`. Also `description`, `levelNames` (a name for each level, as WMTS shows them; default `<name>:<level>`), `pixelSize` (metres per pixel for scale denominators; default 0.00028) and `yAxisFirst` (the CRS lists y first). GeoWebCache's own gridsets are never compared or removed.

### BlobStore

Where GeoWebCache keeps tiles. By default each tile cache pod keeps them in its own temporary folder, lost on a restart and not shared; a blob store keeps them on the shared data volume, in S3, Azure Blob Storage or Google Cloud Storage:

```yaml
apiVersion: configops/v1alpha1
kind: BlobStore
metadata:
  name: tiles-on-volume
spec:
  type: file
  path: gwc                                # under /data
  default: true                            # GeoWebCache's default for every layer
---
apiVersion: configops/v1alpha1
kind: BlobStore
metadata:
  name: tiles-in-s3
spec:
  type: s3
  bucket: tiles
  prefix: gscloud
  endpoint: https://s3.eu-west-2.amazonaws.com   # the region's address, or any S3-compatible service; http:// turns HTTPS off
  access: private                          # or public: GeoWebCache makes the tiles readable by anyone
  credentials: { secret: tiles-s3 }        # keys accessKey and secretKey
```

```yaml
apiVersion: configops/v1alpha1
kind: BlobStore
metadata:
  name: tiles-in-azure
spec:
  type: azure
  container: tiles
  prefix: gscloud
  credentials: { secret: tiles-azure }     # keys accountName and accountKey
  # endpoint: http://azurite:10000/devstoreaccount1   # another service than Azure's own
---
apiVersion: configops/v1alpha1
kind: BlobStore
metadata:
  name: tiles-in-gcs
spec:
  type: gcs
  bucket: tiles
  project: my-project
  defaultCredentials: true                 # the pod's own Google identity; or credentials: { secret } with key apiKey
```

A layer's `cache.blobStore` names one. `maxConnections` (S3, Azure) and `gzip` (S3) are there too, `layout: tms` lays a folder's tiles out as TMS, `proxy: { host, port, domain, workstation, credentials: { secret } }` reaches S3 or Azure through an HTTP proxy (`domain` and `workstation` for NTLM, S3 only; the Secret's keys `username` and `password`), and `quotaProject` bills a Google Cloud store's requests to another project. GeoWebCache gives the keys back as they are, so `plan` compares the secret key too, without printing it. Only the blob stores the files name are compared; none is removed.

### Workspace

Only needed for a workspace's own settings in the web UI's *Edit Workspace* page; workspaces are made for you wherever a resource names one.

```yaml
apiVersion: configops/v1alpha1
kind: Workspace
metadata:
  name: uk
spec:
  namespace: https://example.org/uk   # the namespace URI; GeoServer's default is http://<name>
  isolated: false                     # an isolated workspace is only reached through its own services
  default: true                       # the default workspace
```

A Workspace resource makes its workspace the project's, as if a layer were in it. At most one is the default.

### Settings

GeoServer's own settings: the contact details every service's capabilities show, the proxy address, number formatting and the rest of the web UI's *Global* and *Contact Information* pages. With `workspace`, a workspace's own settings instead (the web UI's workspace *Settings* tab), which its workspace-specific services use.

```yaml
apiVersion: configops/v1alpha1
kind: Settings
metadata:
  name: global
spec:
  contact:
    contactPerson: Map Team
    contactOrganization: Example Council
    contactEmail: maps@example.org
    addressCity: Guildford
    addressCountry: United Kingdom
  proxyBaseUrl: https://maps.example.org/geoserver-cloud
  numDecimals: 6
  global:                                # global-only settings
    resourceErrorHandling: SKIP_MISCONFIGURED_LAYERS
```

| Field | Meaning |
| --- | --- |
| `workspace` | This workspace's own settings, made if it has none. Default: the global settings |
| `contact` | Also each field in other languages: `internationalContactPerson: { cy: ... }` and so on, plus `internationalTitle`. `contactPerson`, `contactOrganization`, `contactPosition`, `contactEmail`, `contactVoice`, `contactFacsimile`, `addressType`, `address`, `addressDeliveryPoint`, `addressCity`, `addressState`, `addressPostalCode`, `addressCountry`, `onlineResource`, `welcome` |
| `restUploadRoot`, `restQuietOnNotFound` | The folder REST file uploads go to, and REST answering 404 without logging a stack trace |
| `proxyBaseUrl`, `useHeadersProxyURL` | The address clients see in capabilities documents, when GeoServer sits behind a proxy |
| `numDecimals`, `charset`, `verbose`, `verboseExceptions`, `localWorkspaceIncludesPrefix`, `showCreatedTimeColumnsInAdminList`, `showModifiedTimeColumnsInAdminList`, `showModifiedUserAdminList` | As in the web UI |
| `global` | Only without `workspace`: `catalogMode` (`hide`, `mixed`, `challenge`: how GeoServer hides what someone may not see), `globalServices`, `resourceErrorHandling` (`OGC_EXCEPTION_REPORT`, `SKIP_MISCONFIGURED_LAYERS`), `webUIMode` (`DEFAULT`, `REDIRECT`, `DO_NOT_REDIRECT`), `xmlPostRequestLogBufferSize`, `featureTypeCacheSize`, `trailingSlashMatch`, `allowStoredQueriesPerWorkspace`, `userDetailsDisplaySettingsInfo` (how the web UI shows the logged-in user and the user list: `loggedInUserDisplayMode`, `showProfileColumnsInUserList`, `emailDisplayMode`, `revealEmailAtClick`), `masterPassword: { secret, key }` (the keystore's master password, from a Secret: `plan` shows it as changed only when GeoServer's differs, and never prints it; `import` can't read it back), `jai` (image processing: `allowInterpolation`, `recycling`, `tilePriority`, `tileThreads`, `memoryCapacity`, `memoryThreshold`, `pngEncoderType`), `coverageAccess` (`maxPoolSize`, `corePoolSize`, `keepAliveTime`, `queueType`, `imageIOCacheThreshold`, `granuleCacheMaxSizeMB`, `granuleCacheThresholdKB`), `tileCaching` (below), `requestLogging` (each request in the GeoServer services' logs: `enabled`, `headers`, `bodies`, `bodySize` in bytes, default 1024; the chart sets the logger they write to, `org.geoserver.filters`, to INFO), `urlChecks` (`false` turns the `URLCheck`s off: GeoServer then fetches any external URL; default `true`), `cors`, `contentSecurityPolicy` and `diskQuota` (standard GeoServer only, below), and `geoserver:` for any other global field |
| `geoserver` | Any other settings field, as GeoServer's REST API names it |

At most one `Settings` for the global settings and one per workspace.

**Changing the master password.** After the change `apply` has GeoServer read its security files again. GeoServer Cloud needs that: it tells its other services about the change before it has swapped the re-encrypted keystore in, and when one of them swaps it first, the REST service keeps the old keystore in memory and answers 500 (`Cannot find alias: config:password:key in security/geoserver.jceks`) for every store with a password. If you change the master password some other way (the REST API, the web UI) and see that error, restart the REST service: nothing is lost.

**Standard GeoServer only** (on GeoServer Cloud `plan` shows each as `!` with the reason, and leaves it out):

- `cors`: cross-origin requests, the web UI's *CORS* page: `enabled`, `allowedOriginPatterns` (a list; default `[*]`), `allowedMethods`, `allowedHeaders`, `maxAge` (seconds; default 3600), `supportsCredentials`. GeoServer reads them when it starts: `apply` says to restart it. (GeoServer Cloud's gateway answers CORS: the chart's `geoserver.gateway` CORS settings, in the README.)
- `contentSecurityPolicy`: the `Content-Security-Policy` header (`security/csp.xml`), used within seconds: `enabled`, `reportOnly`, `allowOverride`, `injectProxyBase`, `remoteResources`, `formAction`, `frameAncestors`, and `policies` to replace GeoServer's: each `{ name, description, enabled, rules: [{ name, description, enabled, filter, directives }] }`.
- `diskQuota`: GeoWebCache's disk quota, which drops the least used tiles once the cache is too big: `enabled`, `quotaStore` (`HSQL`, or `JDBC` with GeoWebCache's own `gwc/geowebcache-diskquota-jdbc.xml`), `globalQuota` (bytes, or `2 GiB`), `globalExpirationPolicyName` (`LFU` or `LRU`), `cacheCleanUpFrequency` and `cacheCleanUpUnits` (`SECONDS` ... `DAYS`), `maxConcurrentCleanUps`, `layerQuotas: [{ layer: <workspace:name>, quota, expirationPolicyName }]`. GeoWebCache's REST API changes the quota only until a restart, so `apply` also writes `gwc/geowebcache-diskquota.xml`; a new `quotaStore` is used after a restart.

**Tile caching defaults** (the web UI's *Caching Defaults* page) are `global.tileCaching`, in GeoServer's own names: `cacheLayersByDefault`, `cacheNonDefaultStyles`, `directWMSIntegrationEnabled`, `requireTiledParameter`, `WMSCEnabled`, `TMSEnabled`, `WMTSEnabled`, `securityEnabled`, `innerCachingEnabled`, `persistenceEnabled`, `metaTilingX`, `metaTilingY`, `gutter`, `defaultCachingGridSetIds`, `defaultCoverageCacheFormats`, `defaultVectorCacheFormats`, `defaultOtherCacheFormats`, `multiLayerCachingEnabled`, `metaTilingThreads`, `seederCorePoolSize`, `seederMaxPoolSize`, `cacheWarningSkips`, `lockProviderName` (GeoServer Cloud needs its clustering lock provider: change it only if you know why). GeoServer keeps them in a file it reads only when its services start, so `apply` writes the file and says to restart them (`kubectl rollout restart` the GeoServer deployments); `plan` compares the file.

### Service

One OGC service's settings: the web UI's *WMS*, *WFS*, *WCS* and *WMTS* pages. `metadata.name` is the service, or `service:` names it. With `workspace:`, that workspace's own settings for the service (the workspace page's service tabs), which its workspace-specific endpoint (`/<workspace>/wms`) uses; the first `apply` starts them from the global settings, as the web UI does:

```yaml
apiVersion: configops/v1alpha1
kind: Service
metadata: { name: uk-wms }
spec: { service: wms, workspace: uk, title: UK maps }
```

```yaml
apiVersion: configops/v1alpha1
kind: Service
metadata:
  name: wms                              # wms, wfs, wcs or wmts
spec:
  title: Example Council maps
  abstract: Open data maps for Surrey.
  keywords: [maps, open data]
  accessConstraints: Open Government Licence v3.0
  srs: [EPSG:27700, EPSG:4326, EPSG:3857]   # the CRSs capabilities list
  maxRenderingTime: 60
  watermark: { enabled: true, url: https://example.org/logo.png, position: BOT_RIGHT, transparency: 80 }
```

| Fields | Services |
| --- | --- |
| `enabled`, `title`, `abstract`, `keywords`, `maintainer`, `fees`, `accessConstraints`, `onlineResource`, `citeCompliant`, `schemaBaseURL`, `verbose`, `disabledVersions` (versions to turn off, e.g. `[1.1.1]`), `metadataLink` (`url`, `type`, `standard`, as a Layer's `metadataLinks`) | All |
| `srs`, `bboxForEachCRS`, `interpolation` (`Nearest`, `Bilinear`, `Bicubic`), `watermark` (`enabled`, `url`, `position`, `transparency`), `maxBuffer`, `maxRequestMemory`, `maxRenderingTime`, `maxRenderingErrors`, `maxRequestedDimensionValues`, `cacheConfiguration` (`enabled`, `maxEntries`, `maxEntrySize`), `remoteStyleMaxRequestTime`, `remoteStyleTimeout`, `getFeatureInfoMimeTypeCheckingEnabled`, `getMapMimeTypeCheckingEnabled`, `dynamicStylingDisabled`, `featuresReprojectionDisabled`, `defaultGroupStyleEnabled`, `transformFeatureInfoDisabled`, `autoEscapeTemplateValues`, `getMapMimeTypes`, `getFeatureInfoMimeTypes` (the formats allowed when checking is on), `allowedURLsForAuthForwarding`, `authorityURLs` (`[{ name, href }]`) and `identifiers` (`[{ authority, identifier }]`) for the root layer, and `metadata`: `pngCompression`, `jpegCompression` (0-100), `svgRenderer` (`Batik`, `Simple`), `svgAntiAlias`, `kmlReflectorMode` (`refresh`, `superoverlay`, `download`), `kmlSuperoverlayMode` (`auto`, `raster`, `overview`, `hybrid`, `cached`), `kmlAttr`, `kmlPlacemark`, `kmlKmscore`, `advancedProjectionHandling`, `advancedProjectionDensification`, `disableDatelineWrappingHeuristic`, `mapWrapping`, `rootLayerInCapabilities`, `MarkFactoryList`, `scalehintMapunitsPixel` | WMS |
| `srs`, `serviceLevel` (`BASIC`, `TRANSACTIONAL`, `COMPLETE`), `maxFeatures`, `featureBounding`, `canonicalSchemaLocation`, `encodeFeatureMember`, `hitsIgnoreMaxFeatures`, `includeWFSRequestDumpFile`, `disableStoredQueriesManagement`, `allowGlobalQueries`, `simpleConversionEnabled`, `getFeatureOutputTypeCheckingEnabled`, `getFeatureOutputTypes` (the formats allowed when checking is on), `gml` (for each version, `1.0`, `1.1`, `2.0`: `srsNameStyle` (`NORMAL`, `XML`, `URN`, `URN2`, `URL`), `overrideGMLAttributes`, `mimeTypeToForce`), and `metadata`: `maxNumberOfFeaturesForPreview`, `SHAPE-ZIP_DEFAULT_PRJ_IS_ESRI`, `ogcapiFeatures` (the OGC API Features conformance classes to offer: `core`, `gml321`, `gmlSF0`, `gmlSF2`, `featuresFilter`, `crsByReference`, `filter`, `search`, `queryables`, `ids`, `sortBy`, `propertySelection`) | WFS |
| `srs`, `gmlPrefixing`, `latLon`, `subsamplingEnabled`, `maxInputMemory`, `maxOutputMemory`, `defaultDeflateCompressionLevel`, `maxRequestedDimensionValues`, `overviewPolicy` (`IGNORE`, `NEAREST`, `QUALITY`, `SPEED`) | WCS |
| `internationalTitle`, `internationalAbstract` (all), `internationalRootLayerTitle`, `internationalRootLayerAbstract` (WMS) | Text in other languages, `{ cy: Mapiau }`, for clients that ask for one (`AcceptLanguages`) |
| `geoserver` | All: any other field, as GeoServer's REST API names it |

**How they're compared and applied.** Both kinds change objects GeoServer already has, so `plan` shows them as `~` (or `+` for a workspace's first settings of its own), and only the fields the files set are compared. The fields are GeoServer's own names, except `abstract` (GeoServer's `abstrct`), and lists are plain lists. `metadata` entries and `gml` versions the file doesn't name are kept as GeoServer has them. GeoServer replaces whatever a PUT leaves out of its settings, so `apply` reads the whole object, sets the files' fields in it, and writes it all back. A project without `Settings` or `Service` resources leaves them alone.

The contact field `addressElectronicMailAddress` isn't kept by GeoServer; use `contactEmail`.

### Role, Group, User and AccessRule

Security: the web UI's *Users, Groups, Roles* and *Data*, *Services* and *REST* security pages. Users and groups live in GeoServer's default user/group service, roles in its default role service.

```yaml
apiVersion: configops/v1alpha1
kind: Role
metadata:
  name: editor                        # GeoServer role EDITOR
spec: {}
---
apiVersion: configops/v1alpha1
kind: Group
metadata:
  name: editors
spec:
  roles: [EDITOR]                     # every member has these
---
apiVersion: configops/v1alpha1
kind: User
metadata:
  name: alice
spec:
  password: { secret: geoserver-users, key: alice }
  groups: [editors]
  roles: []                           # roles of her own, besides her groups'
  enabled: true
---
apiVersion: configops/v1alpha1
kind: AccessRule
metadata:
  name: flood-zones-for-editors
spec:
  layer: { workspace: uk, layer: flood_zones, access: read }
  roles: [EDITOR]
```

| Kind | Field | Default | Meaning |
| --- | --- | --- | --- |
| `Role` | `name` | `metadata.name` upper-cased, dashes as underscores | The role's name in GeoServer |
| | `parent` | none | A role this one inherits from: whatever the parent may do, this role may too |
| | `properties` | none | Role parameters, `{ name: value }` |
| `Group` | `name` | `metadata.name` | The group's name in GeoServer |
| | `roles` | none | Roles every member has |
| | `enabled` | `true` | A group turned off gives its members none of its roles |
| `User` | `name` | `metadata.name` | The user name, for names `metadata.name` can't hold (`alice@example.org`) |
| | `password` | required | `{ secret, key }`: the Secret and key holding it |
| | `groups`, `roles` | none | Groups the user is in, and roles of their own |
| | `enabled` | `true` | A disabled user can't log in |
| | `properties` | none | User attributes, `{ name: value }` |
| `AccessRule` | `roles` | required | Roles allowed, or `["*"]` for everyone |
| | `layer` | | `{ workspace, layer, access }`: who may `read`, `write` or `admin` a layer. `*` (the default for `workspace` and `layer`) means all |
| | `service` | | `{ service, operation }`: who may use an operation, e.g. `{ service: wfs, operation: Transaction }`. `*` means all |
| | `rest` | | `{ path, methods }`: who may call the REST API on an Ant path, e.g. `{ path: /rest/workspaces/uk/**, methods: [GET] }` (`methods` default `[GET]`) |

A role's parent and properties, a user's properties and a group turned off aren't in GeoServer's REST API: `apply` writes them into the default services' files (`security/role/default/roles.xml`, `security/usergroup/default/users.xml`) and has GeoServer read its security files again. An `AccessRule` has exactly one of `layer`, `service` or `rest`. Roles are named as GeoServer shows them, and GeoServer's own `ADMIN` and `GROUP_ADMIN` can be used without defining them. How GeoServer hides what someone may not see is the global `Settings`' `catalogMode` (`hide`, `mixed` or `challenge`).

**What's managed.** A project manages security only when it has at least one of these resources. Then users, groups, roles and rules the files don't name show as unmanaged, and `apply --prune` removes them. GeoServer's own are never touched unless a file names them: the `admin` user, the `ADMIN` and `GROUP_ADMIN` roles, and the default rules (everyone reads every layer, admins write, admins use the REST API). To change a default rule, write a rule with the same `layer`, `service` or `rest`. The `admin` user can't be a `User`: configops logs in as it, so change its password in GeoServer and the `geoserver-admin` Secret together.

**Passwords** can't be read back from GeoServer, so `plan` checks each enabled user's by logging in with it: if GeoServer refuses, it shows `password: (differs)` and `apply` sets it. GeoServer remembers a successful login for some minutes, so for that long an old password still works, and changing back to one used minutes ago may not show.

**REST rules** are checked in order and the first match wins, so a rule after GeoServer's default `/**` is never reached. `apply` keeps them in GeoServer's `security/rest.properties` in that order (the project's rules first, the `/**` defaults last), and `plan` shows a rule that has slipped below `/**` as `place: after /** (never reached)`. GeoServer Cloud can't delete a REST rule through its access-rule API (the path's slashes), which is why the file is written instead.

Changes reach every GeoServer Cloud service within a minute or so.

### PasswordPolicy, AuthFilter, AuthProvider, UserGroupService, RoleService and MasterPasswordProvider

The web UI's *Authentication*, *Passwords* and *Users, Groups, Roles* pages: how GeoServer logs people in, and where it keeps users and roles.

```yaml
apiVersion: configops/v1alpha1
kind: PasswordPolicy
metadata: { name: default }                # GeoServer's own; or a new one, which a user/group service names
spec: { minLength: 12, digit: true, uppercase: true }
---
apiVersion: configops/v1alpha1
kind: AuthFilter
metadata: { name: proxy-header }
spec:
  class: org.geoserver.security.config.RequestHeaderAuthenticationFilterConfig
  className: org.geoserver.security.filter.GeoServerRequestHeaderAuthenticationFilter
  config:                                  # its fields, as GeoServer's own file names them
    principalHeaderAttribute: X-User
    userGroupServiceName: default
    roleSource: { class: "org.geoserver.security.config.PreAuthenticatedUserNameFilterConfig$PreAuthenticatedUserNameRoleSource", value: UserGroupService }
---
apiVersion: configops/v1alpha1
kind: Settings
metadata: { name: global }
spec:
  global:
    security:
      roleService: default
      authProviders: [default]             # tried in this order
      bruteForcePrevention: { enabled: true, maxDelaySeconds: 5 }
      filterChains:                        # every chain, in the order GeoServer tries them
        - { name: wms-by-header, paths: ["/wms/**"], filters: [proxy-header, anonymous], interceptor: interceptor, exceptionTranslation: exception }
        - { name: web, paths: ["/web/**", "/gwc/rest/web/**", "/"], filters: [rememberme, form, gateway-shared-auth, anonymous] }
        # ... and the rest of GeoServer's chains: import --settings writes them all
```

`MasterPasswordProvider` is where GeoServer reads its master password from (`class: org.geoserver.security.password.URLMasterPasswordProviderConfig`, `config: { url, encrypting, readOnly }`); making one doesn't make it the one GeoServer uses. `AuthFilter`, `AuthProvider`, `UserGroupService` and `RoleService` take any kind GeoServer has (LDAP, JDBC, request headers, …): `class` is its configuration class and `className` the class that does the work, both needed for a new one and left out to change one GeoServer has; `config` holds its other fields, with `{ class, value }` for a field that has a class. `PasswordPolicy` takes `uppercase`, `lowercase`, `digit`, `minLength` and `maxLength` (-1: none).

`Settings` `global.security` holds the security settings: `roleService` (the active role service), `authProviders`, `encryptUrlParams`, `passwordEncryption`, `bruteForcePrevention` (`enabled`, `minDelaySeconds`, `maxDelaySeconds`, `maxBlockedThreads`, `whitelistedMasks`), `rememberMe` (`className`, `key`: the key remember-me cookies are signed with), and `filterChains`: the whole list, each `{ name, paths, filters }` plus `class`, `interceptor`, `exceptionTranslation`, `roleFilter`, `disabled`, `allowSessionCreation`, `ssl`, `matchHTTPMethod`, `httpMethods`; a chain's settings the file leaves out stay as GeoServer has them. `validate` refuses a list without a chain for `/rest`, which would lock configops out.

**How they're applied.** GeoServer Cloud's REST API has no endpoints for most of these, but GeoServer keeps each in a file (`security/<kind>/<name>/config.xml`, `security/config.xml`) that `/rest/resource` reads and writes. `apply` writes the file, then has GeoServer read its security files again (by saving its default authentication provider unchanged); the other services follow within a minute. Objects are written before the settings that name them. If GeoServer refuses new settings, `apply` puts the old file back. Only the fields a file sets are compared; nothing is removed. A workspace administrator is an `AccessRule` with `access: admin` on the workspace's layers.

### URLCheck

An external URL GeoServer may fetch: a style given as `SLD=<url>` in a GetMap, a graphic a style points to, and other remote references. While URL checks are on (the global `Settings`' `urlChecks`, on in GeoServer Cloud), GeoServer refuses every URL no enabled check matches.

```yaml
apiVersion: configops/v1alpha1
kind: URLCheck
metadata: { name: council-styles }
spec:
  regex: '^https://styles\.example\.org/.*$'
  description: Our published styles
```

| Field | Meaning |
| --- | --- |
| `regex` | A Java regular expression the whole URL must match. Required |
| `description` | Shown in GeoServer's web UI |
| `enabled` | Default: `true` |
| `name` | The check's name in GeoServer. Default: `metadata.name` |

The WMS service follows a change within about 20 s.

## Secrets

A `secret` field names a secret; the files never hold its value. Where the tool looks:

| Running | Where secret `os-api`, key `apiKey` comes from |
| --- | --- |
| In Kubernetes | Kubernetes Secret `os-api`, key `apiKey` |
| On a laptop or in CI | Environment variable `CONFIGOPS_OS_API_APIKEY`, or a git-ignored `secrets.yaml` next to the project |

## Ownership and pruning

The tool must never touch what it didn't create. The rule is simple: **a workspace named in the project belongs to the tool**, much like a Kubernetes namespace:

- Everything inside it (stores, layers, workspace styles, the images beside them, templates) is compared with the files. Anything there that the files don't mention is shown by `plan` as `unmanaged` and removed only with `apply --prune`.
- Global styles and other workspaces are never changed or deleted, unless a resource names them. Fonts are global: added and changed, never deleted.
- Tables: configops never creates, changes or deletes a table; it publishes them.
- Settings and services: only the ones a `Settings` or `Service` resource names, and only the fields it sets. They are never deleted.
- URL checks: only when the project has a `URLCheck`; then checks it doesn't name are unmanaged, removed only with `--prune`.
- Security: only when the project has a `Role`, `Group`, `User` or `AccessRule`; then the rest is unmanaged, as in a workspace (see above). GeoServer's own users, roles and default rules are never touched unless a file names them. GeoServer has one set of global settings, so when several projects share a GeoServer, let one of them hold the global `Settings` and the `Service`s.

## Commands

| Command | Does |
| --- | --- |
| `configops validate` | Checks the files: schema, references between resources, style files exist. No network |
| `configops plan` | Compares files with live GeoServer and PostGIS; prints what `apply` would create, change or delete. Exit code 2 when there are changes, for CI |
| `configops apply [--prune]` | Makes the changes `plan` shows, in order: workspace, settings, security, stores, styles, layers, layer groups, tile cache |
| `configops refresh [table or path]` | After new data: works the bounds of the layers on it out again, clears their cached tiles (and their groups'), seeds again what the files seed |
| `configops status` | Per layer and group: published, drifted, tiles cached |
| `configops import` | Reads a live GeoServer workspace and writes the files that describe it, or updates the files that do; the way in for an existing install, and for changes made in the web UI |

### `validate`, and checking files as you type

`configops validate` checks every resource against the JSON Schema in [`configops/schema/v1alpha1.json`](../configops/schema/v1alpha1.json), then the rules across files: every reference resolves, style files exist, no two layers or groups share a GeoServer name, and no layer group contains itself. Problems come out as `file:line`, with a non-zero exit code for CI:

```
$ configops validate examples/uk-basemap
layers/os-open-roads.yaml:6: error: Layer road-links: spec.titel: unknown field 'titel' (did you mean 'title'?)
layers/os-open-roads.yaml:20: error: Layer road-nodes: spec.style: no style named 'os-road-nodes' (a file in styles/, a Style resource, or one of GeoServer's built-in styles)

2 error(s), 0 warning(s); 10 resources in 6 files
```

Editors that understand JSON Schema check the files as you type. In VS Code, with the YAML extension, the repo's `.vscode/settings.json` already points the example files at the schema; for your own project, add the same `yaml.schemas` entry.

### `plan` and `apply`

`configops plan` compares the files with live GeoServer, and with PostGIS for whether each layer's table exists yet. It prints what `apply` would do, with each changed field. `configops apply` makes those changes in dependency order: workspaces, stores, styles, layers, layer groups (each after the groups it holds), then, with `--prune`, deletions, groups first. If a change fails, `apply` stops there and says so; running it again carries on, since everything already done now matches.

```
$ configops plan
  ~ style uk:os-road-node   (styles/os-road-node.sld differs from GeoServer's copy)
  ~ layer uk:road_links
      title: "Roads (edited in UI)" -> "OS Open Roads, road links"
  + layer uk:road_nodes   (data_layers.os_open_roads_road_node)
  ? layer uk:old_test_points   (unmanaged; removed with --prune)
  ! layer uk:survey_sites   (table field_data.sites doesn't exist yet; configops only publishes it)

Plan: 1 to add, 2 to change, 0 to delete, 1 unmanaged. 1 can't be done.
```

`+` adds, `~` changes, `-` deletes (only with `--prune`), `?` is unmanaged and left alone, and `!` can't be done yet, such as a layer on a table that doesn't exist yet. Groups leave such a layer out until it can be published. `plan`'s exit code is 0 when GeoServer matches the files, 2 when there are changes to make (for CI), 3 when nothing is left but `!` lines, and 1 when it can't run. `apply` exits 0 once it has made every change it can, even with `!` lines left: those wait for tables loaded separately, often after the deploy, so an apply Job doesn't fail for them, or are settings only standard GeoServer can take (below). It exits 1 when a change fails.

**GeoServer Cloud or standard GeoServer.** The same files work on both. A few settings only standard GeoServer can take: a group's `styles`, `oracle` stores, and the global `cors`, `contentSecurityPolicy` and `diskQuota`. When the files set any of them, `plan` asks GeoServer which it is (GeoServer Cloud's version ends in `-CLOUD`) and, on GeoServer Cloud, shows each as `!` with the reason and leaves it out; a layer on an `oracle` store goes with it:

```
  ! group uk:basemap   (GeoServer Cloud can't set styles: its catalog database doesn't keep a group style's layers, ...)
```

Run `plan` on a schedule to catch drift: [docs/kubernetes.md](../kubernetes.md) has an hourly CronJob whose Job fails when GeoServer has drifted from the files, say a layer edited or deleted in the web UI, or when `plan` can't run; `!` lines alone don't fail it. The log shows what differs, and `apply` puts it back. Both commands read GeoServer's address and the `credentials` secret (keys `username`, `password`) from the Project; `--geoserver http://localhost:8080/geoserver-cloud` points them elsewhere: another environment, or a port-forward.

Each layer gets a PostGIS store in its workspace for the schema its table is in, named after the schema and reaching the database through the Project's JNDI datasource, so no password goes into GeoServer. Bounding boxes are worked out from the table whenever a layer is published or changed.

#### What `plan` compares

Only fields the files set, or whose defaults this spec defines (`title`, `enabled`, `advertised`, `queryable`, a group's `mode`), are compared. Fields GeoServer works out, or that nobody set, never show as changes, so a plan after `apply` is empty. A title changed in the web UI shows as drift and `apply` puts it back; a default style changed in the web UI on a layer whose file sets none is left alone. A style is compared by its file's text.

`cache` is compared with GeoWebCache's settings for the layer or group. GeoServer Cloud caches every new layer by itself, so `cache: false` (the default) means `apply` removes that cache; `cache: true` caches in EPSG:4326 and EPSG:900913 as PNG, and `{ gridsets, formats }` sets both. GeoWebCache's REST API is at `<geoserver url>/gwc/rest` through the gateway; `CONFIGOPS_GWC_URL` points elsewhere (a GeoServer Cloud REST service reached directly serves it under `/geoserver-cloud/gwc/rest`).

## `refresh`

After new data is loaded into a table or a file, GeoServer still has the old bounding boxes, and its tile cache the old tiles. `configops refresh` names what changed (`schema.table`, or a file store's `path`, with or without `/data/`), or reads it from a file, one per line, as `geodata sync --loaded FILE` writes it:

```
$ geodata sync -C data --loaded loaded.txt && configops refresh -C publish --from loaded.txt
Refreshed: bounds of 2 layer(s) worked out again, cached tiles of 3 layer(s) or group(s) cleared; seeding 1 again
```

For each layer on them it resets a raster store (so GeoServer reads the new file), works the bounds out again, clears the cached tiles of the layer and of the groups that hold it (directly or through other groups) and starts again the seeding the files ask for. With nothing named, it refreshes every layer on a table or file.

## `status`

`configops status` shows each layer and group as GeoServer has them, and changes nothing.

```
$ configops status
LAYER                           PUBLISHED  DRIFTED  TILES CACHED
uk:conservation_areas           yes        no       no
uk:road_links                   yes        title    yes
uk:survey_sites                 no: table field_data.sites doesn't exist yet; configops only publishes it  -  no
uk:uk_basemap (group)           yes        no       yes
```

"Drifted" lists the fields `plan` would change. `--format json` is for scripts and dashboards.

## Importing today's config

`configops import <workspace>` reads a live GeoServer workspace and writes the files that describe it: its layers and groups in `<workspace>.yaml`, its styles and the images beside them in `styles/`, its templates and its layers' in `templates/`, and `project.yaml` if the directory has none (with `--geoserver` for the address, and the login from secret `geoserver-admin`). Only what differs from the spec's defaults is written: a title equal to the layer name, GeoServer's own keywords, the default style for the layer's geometry, and switches left on all drop out. So `configops plan` on the result has nothing to do. A layer on a table in the Project's database schema names the table alone. Stores the files have a kind for (Cloud-Optimised GeoTIFFs, cascaded WMS, WMTS and WFS, PostGIS with a password) are written as `Store`s with their layers; a store's password can't be read back, so it names a credentials Secret, which the import lists.

```
$ configops import test -C my-project --geoserver http://localhost:8080/geoserver-cloud
Wrote test.yaml: 0 store(s), 4 layer(s), 0 group(s); 0 style(s) in styles/; project.yaml
  note: store postgis: apply moves its layers to a store named after their schema (data_layers)
```

```yaml
apiVersion: configops/v1alpha1
kind: Layer
metadata:
  name: test-points-chang
spec:
  table:
    schema: data_layers
    name: test_points_yo
  title: test_points_chagned
```

The notes say what couldn't be carried over as it is: a store not named after its schema (`apply` makes one that is), a store that connects with a password instead of the JNDI datasource, styles from another workspace or global styles other than GeoServer's built-in ones, and rasters.

`--settings` and `--security` bring in the rest of GeoServer, with or without a workspace (without one, the directory needs a Project):

```
$ configops import --settings --security -C my-project
Wrote server.yaml: 1 Settings, 2 Service(s); security.yaml: 2 role(s), 1 group(s), 2 user(s), 3 rule(s)
  note: passwords can't be read from GeoServer: put each user's in Secret geoserver-users (keys: alice, bob); plan stops until it's there
```

`server.yaml` holds the global `Settings`, a `Settings` for each workspace with settings of its own, and a `Service` for each service, with only the fields that differ from a fresh GeoServer Cloud. `security.yaml` holds every role, group, user and access rule that isn't GeoServer's own unchanged default, and every URL check. Fields outside the schema aren't imported. Passwords can't be read back, so each user's comes from Secret `geoserver-users`, under a key named after the user; until it holds them, `plan` stops and says which is missing. With them there, `plan` on the imported files has nothing to do.

### Bringing changes made in GeoServer back into the files

Run in a directory that already describes what's being imported, `import` updates the files instead of writing new ones, so a change made in the web UI can be committed rather than undone by the next `apply`:

```
$ configops import uk -C examples/uk-basemap
Updated the files: 1 changed: Layer counties (title); 1 added: Layer parishes; 1 style files written: styles/os-road-node.sld
Next: git diff, then configops plan -C examples/uk-basemap
```

- What import reads is matched with the files by what it is in GeoServer (a layer's workspace and name, a user's name, a rule), so the files' own resource names, and the references to them, stay.
- Only the keys whose meaning changed are rewritten, in place; the rest of the file, its order and its comments are left as written. A value spelled out in a file that is GeoServer's default (`enabled: true`, a title equal to the name, the default style for the geometry) counts as the same.
- A resource only in GeoServer is added to `<workspace>.yaml`, `server.yaml` or `security.yaml`. A style file is rewritten where the project keeps it, as GeoServer has it, when its content differs.
- A resource only in the files is listed (`not in GeoServer`); `--prune` takes it out of the files.
- What GeoServer can't give back stays as the files have it: passwords and keys (the Secrets they name), a layer's `table` as the files spell it, the master password, and its `attributes` when the project's database can't be reached (import compares the published attributes with the table's columns).

`--force` writes fresh files instead, replacing those that exist. Then `git diff` shows the change, and `plan` on the files has nothing to do.

## Decided

- **Scale limits** live in styles ([above](#scale-limits-live-in-styles)), 9 Nov 2026.
- **Layer groups** are in this version as `LayerGroup`, 9 Nov 2026.

- **Data loading** is its own tool, geodata-sync; configops publishes tables and refreshes GeoServer after loads (`refresh`), 13 Nov 2026.

## Open questions

1. **Data versions on screen:** should a layer's abstract or keywords carry the loaded version, from geodata's ingest log?
2. **Several projects on one GeoServer:** fine if each owns its own workspaces; anything else needed?
