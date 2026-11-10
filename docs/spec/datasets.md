# Dataset spec (draft, `v1alpha1`)

A set of YAML files that says which open datasets you want, where they come from, how often they update, and how they are published and styled in GeoServer Cloud. A small command-line tool reads the files and makes PostGIS and GeoServer match them, the way OpenTofu makes infrastructure match its configuration.

> **Status:** first draft for review, 9 Nov 2026. `gisdata validate` and `gisdata discover` are built; the other commands aren't yet. `gisdata` is a working name for the tool and the `apiVersion` group; both will change when the project is named.

## Principles

1. **The files hold what you chose, nothing else.** Anything GeoServer or the data can work out (projection definitions, bounding boxes, attribute lists, links, timestamps) is never written in the files. The tool fills it in when it applies them.
2. **Plan, then apply.** `gisdata plan` shows what would change; `gisdata apply` makes exactly those changes, in dependency order, and running it twice changes nothing the second time.
3. **No state file.** The live GeoServer, the PostGIS schemas and the ingest log are the state. Drift (a change made in the web UI) shows up in the next `plan`.
4. **Secrets by reference.** API keys and passwords never appear in the files, only the name of the secret that holds them.
5. **Shaped like Kubernetes resources** (`apiVersion`, `kind`, `metadata`, `spec`), so the same files could later drive a Kubernetes operator instead of the CLI.
6. **Build on existing tools.** GeoServer calls go through `python-geoservercloud`; data loading through GDAL.

## How it fits together

```
 config files (git) ──► gisdata plan / apply ──► GeoServer catalog: workspaces, stores, layers, styles, tile cache
        │
        └────────────► gisdata sync (nightly) ──► PostGIS tables: download, stage, validate, back up, swap in
                                                    └─► then refresh extents and clear cached tiles of the layers on them
```

`apply` manages configuration; `sync` manages data. They are separate so that a data update never changes configuration, and a configuration change never downloads hundreds of megabytes by surprise. `apply` does run a first `sync` for a dataset whose tables don't exist yet, so a new layer has something to show.

## Files

The tool reads every `*.yaml` file under one directory. A file can hold several resources separated by `---`; how you split them into files is up to you. A typical layout:

```
uk-basemap/
  project.yaml              # one Project
  sources.yaml              # Sources
  datasets/
    os-open-roads.yaml      # a Dataset and the Layers made from it
    boundary-line.yaml
  layers/
    survey-sites.yaml       # Layers on tables you load yourself
  styles/
    os-road-link.sld        # style files, picked up by name
    os-road-node.sld
```

Every resource has the same outer shape:

```yaml
apiVersion: gisdata/v1alpha1
kind: Layer                 # Project, Source, Dataset, Layer, LayerGroup or Style
metadata:
  name: road-links          # unique per kind; lowercase letters, digits and dashes
spec: {}                    # what you want; differs per kind
```

There is no `status` in the files. Status is what `gisdata status` reports from the live system; writing it back would make every sync a git change.

## Kinds

### Project

One per directory. Says which GeoServer and database to manage and the defaults for everything else.

```yaml
apiVersion: gisdata/v1alpha1
kind: Project
metadata:
  name: uk-basemap
spec:
  geoserver:
    url: http://geoserver-gsc-gateway:8080/geoserver-cloud
    credentials: { secret: geoserver-admin }       # keys: username, password
  database:
    jndi: java:comp/env/jdbc/mypostgis             # how GeoServer reaches PostGIS (from the Helm chart)
    connection: { secret: geoserver-pgconfig-secret }   # how gisdata itself reaches it, for loading data
    schemas: { live: data_layers, staging: staging, backup: backup }
  workspace: uk                                    # default workspace for layers and styles
  crs: EPSG:27700                                  # default CRS for tables that don't say
  backups: 3                                       # previous loads kept per table
```

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `geoserver.url` | yes | | Base URL of GeoServer (gateway) |
| `geoserver.credentials.secret` | yes | | Secret with `username` and `password` |
| `database.jndi` | yes | | JNDI name GeoServer uses for the data store |
| `database.connection.secret` | yes | | Secret with `host`, `port`, `database`, `username`, `password` |
| `database.schemas` | no | `data_layers`, `staging`, `backup` | Where live, staged and backed-up tables live |
| `workspace` | yes | | Default workspace; the tool owns it (see [Ownership](#ownership-and-pruning)) |
| `crs` | no | `EPSG:4326` | Default CRS of tables |
| `backups` | no | `3` | Previous versions kept per table |

### Source

Where data comes from. One per provider account.

```yaml
apiVersion: gisdata/v1alpha1
kind: Source
metadata:
  name: os
spec:
  type: os-downloads                 # OS Downloads API
  credentials: { secret: os-api }    # key: apiKey; optional, OpenData products need none
---
apiVersion: gisdata/v1alpha1
kind: Source
metadata:
  name: web
spec:
  type: url                          # plain file downloads, no key
---
apiVersion: gisdata/v1alpha1
kind: Source
metadata:
  name: data-gov-uk
spec:
  type: ckan                         # a CKAN portal
  url: https://data.gov.uk
```

| Field | Required | Meaning |
| --- | --- | --- |
| `type` | yes | One of the types below |
| `url` | for `ckan`, `ogc-api-features`, `wfs` | The source's address. For CKAN, the portal (`https://data.gov.uk`); an address already inside `/api/` (such as `https://portal.example/api/3/action`) is used as given. For WFS, the service (`https://host/wfs`); request parameters copied with it (`?service=WFS&request=GetCapabilities`) are dropped, others (`?map=`) kept. For OGC API Features, the API's landing page; a collection's address is cut back to it |
| `credentials.secret` | no | Secret with `apiKey`. For CKAN it is an API token, sent only to the portal, never to the servers its files are on. Not yet used by `wfs` or `ogc-api-features`, which work with public services only so far |

| `type` | Lists products (`discover`) | Needs a key | Status |
| --- | --- | --- | --- |
| `os-downloads` | yes | no for OpenData products | `discover` built; loading exists in the pipeline |
| `url` | no: you give the URL | no | `discover` built (reads the file's layers) |
| `ckan` (data.gov.uk and many city, regional and national portals) | yes, by search | no for public data | `discover` built |
| `wfs` (versions 1.0 to 2.0) | yes: feature types | usually no | `discover` built |
| `ogc-api-features` | yes: feature collections | usually no | `discover` built |

Each type is a plug-in with two parts: list what's available, and fetch one product with its version. Adding a provider means adding one plug-in.

### Dataset

One product from a source, loaded into one or more tables, with an update schedule.

```yaml
apiVersion: gisdata/v1alpha1
kind: Dataset
metadata:
  name: os-open-roads
spec:
  source: os
  product: OpenRoads                 # the provider's product ID, as `gisdata discover os` lists it
  update: weekly                     # daily | weekly | monthly | manual | a cron expression
  tables:                            # source layer -> table; names as `gisdata discover os OpenRoads` lists them
    road_link: {}                    # table os_open_roads_road_link
    road_node: { name: road_nodes }  # table road_nodes
```

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `source` | yes | | Name of a Source |
| `product` | for `os-downloads`, `ckan`, `wfs` | | Product, package or collection ID at the source |
| `url` | for `url` | | File to download (zip, GeoPackage, GeoJSON, CSV, Shapefile) |
| `resource` | no, `ckan` only | the best loadable file | Which file of the package: a resource ID or name, as `discover` lists them. Packages often hold the same data in several formats, or several files |
| `update` | no | `manual` | When `sync` checks for a new version |
| `tables` | no | every layer in the file | Which layers of the downloaded file to load, and as what |
| `tables.<layer>.name` | no | `<dataset>_<layer>` with dashes as underscores | Table name in the live schema |
| `tables.<layer>.crs` | no | from the file, else the project's `crs` | Override when the file doesn't declare its CRS |
| `validation.dropInvalidGeometries` | no | `true` | Leave out rows whose geometry PostGIS calls invalid |
| `validation.minRows` | no | `1` | Fail the update (keep the old table) below this count |

A product is a *version* at the source (OS publishes `2026-10`, for example). `sync` loads a new version only when the source has one, and keeps the last `backups` versions of each table.

### Layer

How a table is published in GeoServer. A layer is either on a dataset's table, or on any table you load yourself.

```yaml
apiVersion: gisdata/v1alpha1
kind: Layer
metadata:
  name: road-links
spec:
  dataset: os-open-roads
  table: road_link                   # a key under the dataset's `tables`
  title: OS Open Roads, road links
  abstract: Roads in Great Britain from OS Open Roads.
  style: os-road-link                # default style
  styles: [line]                     # extra styles clients may ask for
  cache: true                        # tile caching in GeoWebCache
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata:
  name: survey-sites
spec:
  table: { schema: field_data, name: sites }   # a table gisdata doesn't load
  title: Survey sites
  style: point
```

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `dataset` + `table` | one of the two forms | | A table loaded by a Dataset |
| `table.schema` + `table.name` | one of the two forms | | Any existing table |
| `name` (in GeoServer) | no | `metadata.name` with dashes as underscores | Layer name clients use |
| `workspace` | no | the project's `workspace` | |
| `title`, `abstract`, `keywords` | no | title = name | Shown in capabilities documents |
| `style` | no | GeoServer's default for the geometry | Default style |
| `styles` | no | none | Extra styles |
| `enabled`, `advertised`, `queryable` | no | `true` | Publishing switches |
| `cache` | no | `false` | `true`, or `{ gridsets: [...], formats: [...] }` |
| `attribution` | no | from the dataset's source, where known | Text clients must show, e.g. "Contains OS data © Crown copyright and database right 2026" |

Not in the file, worked out on apply: native and lat/lon bounding boxes (computed from the table), attribute list and types (from the table), projection definition (from the CRS), store, links, timestamps.

### Style

Style files are picked up from `styles/` by file name: `styles/os-road-link.sld` is the style `os-road-link`. A `Style` resource is only needed to override that:

```yaml
apiVersion: gisdata/v1alpha1
kind: Style
metadata:
  name: os-road-link
spec:
  file: styles/os/road-link-detailed.sld
  workspace: uk                       # default: the project's workspace
```

Formats: SLD (`.sld`) and MBStyle (`.mbstyle`); CSS (`.css`) if the GeoServer images include it and the client gains support. Files are uploaded exactly as written, never re-encoded by GeoServer.

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
apiVersion: gisdata/v1alpha1
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
apiVersion: gisdata/v1alpha1
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
| `title`, `abstract`, `keywords` | no | title = name | Shown in capabilities documents |
| `cache` | no | `false` | Tile caching for the group as a whole |

| `mode` | GeoServer mode | Clients see |
| --- | --- | --- |
| `single` | SINGLE | One layer that draws all members; the members are still published on their own |
| `named` | NAMED | The group as a layer, with its members listed under it |
| `container` | CONTAINER | A folder of its members; the group itself can't be requested |
| `opaque` | OPAQUE_CONTAINER | One layer; its members are hidden from capabilities |

Not in the file, worked out on apply: the group's bounding box (from its members) and its projection.

## Secrets

A `secret` field names a secret; the files never hold its value. Where the tool looks:

| Running | Where secret `os-api`, key `apiKey` comes from |
| --- | --- |
| In Kubernetes | Kubernetes Secret `os-api`, key `apiKey` |
| On a laptop or in CI | Environment variable `GISDATA_OS_API_APIKEY`, or a git-ignored `secrets.yaml` next to the project |

## Ownership and pruning

The tool must never touch what it didn't create. The rule is simple: **a workspace named in the project belongs to the tool**, much like a Kubernetes namespace:

- Everything inside it (stores, layers, workspace styles) is compared with the files. Anything there that the files don't mention is shown by `plan` as `unmanaged` and removed only with `apply --prune`.
- Global styles and other workspaces are never changed or deleted, unless a resource names them.
- Tables: only tables a Dataset declares, in the configured schemas, are created, replaced or backed up. A Layer on someone else's table only publishes it.

## Commands

| Command | Does |
| --- | --- |
| `gisdata validate` | Checks the files: schema, references between resources, style files exist. No network |
| `gisdata discover <source> [product]` | Lists products a source offers, or the layers inside one product |
| `gisdata plan` | Compares files with live GeoServer and PostGIS; prints what `apply` would create, change or delete. Exit code 2 when there are changes, for CI |
| `gisdata apply [--prune]` | Makes the changes `plan` shows, in order: workspace, store, styles, tables (first sync), layers, layer groups, tile cache |
| `gisdata sync [dataset]` | Checks sources for new versions and loads them (stage, validate, back up, swap in), then refreshes extents and clears cached tiles of the layers on them. The nightly job |
| `gisdata status` | Per dataset: version loaded, when, rows, last error, next check. Per layer: published, drifted, tiles cached |
| `gisdata import` | Reads a live GeoServer workspace and writes the files that describe it; the way in for an existing install |

### `validate`, and checking files as you type

`gisdata validate` checks every resource against the JSON Schema in [`gisdata/schema/v1alpha1.json`](../../gisdata/schema/v1alpha1.json), then the rules across files: every reference resolves, style files exist, no two layers or groups share a GeoServer name, no two datasets load the same table, and no layer group contains itself. Problems come out as `file:line`, with a non-zero exit code for CI:

```
$ gisdata validate examples/uk-basemap
datasets/os-open-roads.yaml:22: error: Layer road-links: spec.titel: unknown field 'titel' (did you mean 'title'?)
datasets/os-open-roads.yaml:34: error: Layer road-nodes: spec.style: no style named 'os-road-nodes' (a file in styles/, a Style resource, or one of GeoServer's built-in styles)

2 error(s), 0 warning(s); 10 resources in 6 files
```

Editors that understand JSON Schema check the files as you type. In VS Code, with the YAML extension, the repo's `.vscode/settings.json` already points the example files at the schema; for your own project, add the same `yaml.schemas` entry.

### `discover`

`gisdata discover <source>` lists a source's products and the format each would load as; `gisdata discover <source> <product>` lists its downloads, marks the one `sync` would use, and lists the layers inside it, which are the names `tables` takes. `<source>` is a Source in the project (`-C dir`, default the current directory), whose secret supplies the API key if it has one, or a source type when there is no project yet: `os-downloads`, `ckan --url <portal>`, or `url`.

| Source type | `discover <source>` | `discover <source> <product>` |
| --- | --- | --- |
| `os-downloads` | Every OS product (`--search` narrows) | The product's formats; layers of the one `sync` uses |
| `ckan` | Packages matching `--search`, 20 at a time (`--limit`); without a search, the latest changed | The package's resources, marked loadable or not (web pages and services aren't); layers of the best file, or of `--resource <id or name>` |
| `url` | (nothing to list) | The product is the file's address: `gisdata discover url https://example.org/roads.zip` |
| `wfs` | The service's feature types (`--search` narrows) | The feature type (with or without its namespace prefix): geometry, CRS and count, read by GDAL's WFS driver, which also loads it, so the layer name shown is the loader's |
| `ogc-api-features` | The API's feature collections (coverages and records are left out) | The collection: the count from `numberMatched`, the geometry from the first feature, the CRS the data is stored in |

On a CKAN package, a resource that is a WFS or OGC API service (recognised by its format, address or title, since portals often leave the format blank) shows the command that discovers it, and search results mark packages with only a service as `via WFS`. Many data.gov.uk datasets are published this way, the Environment Agency's among them:

```
$ gisdata discover ogc-api-features --url https://environment.data.gov.uk/spatialdata/flood-map-for-planning-flood-zones/ogc/features/v1 Flood_Zones_2_3_Rivers_and_Sea
Flood Zones 2 3 Rivers And Sea (Flood_Zones_2_3_Rivers_and_Sea)
  OGC API Features: https://environment.data.gov.uk/spatialdata/flood-map-for-planning-flood-zones/ogc/features/v1/collections/Flood_Zones_2_3_Rivers_and_Sea/items

LAYER                           GEOMETRY      CRS         FEATURES
Flood_Zones_2_3_Rivers_and_Sea  MultiPolygon  EPSG:27700  415,408
```

Services are asked directly, so nothing is cached. Requests to any source are tried again, twice, when a server drops the connection or is briefly unavailable.

```
$ gisdata discover data-gov-uk conservation-areas125
Conservation Areas (conservation-areas125), version 2026-11-04
    Shapefile: Conservation Areas, conservation_areas.zip, size unknown  [c3a73b80-a7fb-4e21-ab32-e87f514b620e]
    GeoJSON: Conservation Areas, conservation_areas.geojson, size unknown  [5dcaa616-b91c-4e58-8b11-141fda4db7e1]
    GML: Conservation Areas, conservation_areas.gml, size unknown  [57f9f97a-82da-4e98-b5a4-7f9d2dff75ee]
  * GeoPackage: Conservation Areas, conservation_areas.gpkg, size unknown  [9dd40b81-62f5-4c75-b1cd-d200af101632]
  (* the download sync uses)
Another one: --resource <id or name>; in a Dataset, `resource: 9dd40b81-62f5-4c75-b1cd-d200af101632`

LAYER               GEOMETRY  CRS         FEATURES
conservation_areas  Geometry  EPSG:27700  11
```

Portals label formats loosely (`SHP`, `ZIP`, a media-type URL, or nothing), so the format comes from the declared format, the media type and the file name together; a zip named `roads.gpkg.zip` counts as a GeoPackage. Loadable files, best first: GeoPackage, Shapefile, GeoJSON, GML, File Geodatabase, KML, FlatGeobuf, then a zip of unknown contents.

```
$ gisdata discover os OpenRoads
OS Open Roads (OpenRoads), version 2026-10
    ESRI® Shapefile: oproad_essh_gb.zip, 585.8 MB
    GML 3: oproad_gml3_gb.zip, 611.8 MB
  * GeoPackage: oproad_gpkg_gb.zip, 1.0 GB
    Vector Tiles (MBTiles): oproad_mbtiles_gb.zip, 1.4 GB
  (* the download sync uses)

LAYER              GEOMETRY    CRS         FEATURES
motorway_junction  Point       EPSG:27700  668
road_link          LineString  EPSG:27700  3,980,725
road_node          Point       EPSG:27700  3,364,565
```

The layers are read from the download in place, with GDAL over HTTP range requests, so nothing is saved to disk (a server that only sends whole files has the file fetched to a temporary folder instead, up to 300 MB); the zip's file list costs a few requests, but a GeoPackage inside a compressed zip is read through, which takes a minute or two for a 1 GB product. A product version never changes (for CKAN, the package's last change; for a link, its ETag or Last-Modified), so the layers are cached (`~/.cache/gisdata`) and the next run is instant; `--refresh` reads them again, `--no-layers` skips them, `--format json` is for scripts. GDAL 3.7 or later (`ogrinfo`) is needed for layers.

The download `sync` uses is the first of GeoPackage, Shapefile, GML, MapInfo TAB that comes as one file for the whole country; a format offered only as grid-square tiles is passed over, since each tile would become its own table.

### What `plan` compares

Only fields the files set (or whose defaults this spec defines) are compared. Fields GeoServer works out, or that nobody set, never show as changes, so a plan after `apply` is empty. A changed title in the web UI shows as drift and `apply` puts it back.

```
$ gisdata plan
  ~ layer uk:road_links
      title: "Roads (edited in UI)" -> "OS Open Roads, road links"
  + layer uk:road_nodes
  + table data_layers.road_nodes            (first sync: OpenRoads 2026-10, ~180 MB)
  ? layer uk:old_test_points                (unmanaged; removed with --prune)

Plan: 2 to add, 1 to change, 0 to delete, 1 unmanaged.
```

## Importing today's config

`gisdata import` on the current `test` workspace would produce about 30 lines in place of the 541 lines of exported JSON for its four layers and data store:

```yaml
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: { name: openroads }
spec:
  workspace: test
  table: { schema: data_layers, name: openroads }
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: { name: opengreenspace }
spec:
  workspace: test
  table: { schema: data_layers, name: opengreenspace }
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: { name: boundaryline }
spec:
  workspace: test
  table: { schema: data_layers, name: boundaryline }
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: { name: test-points-chang }
spec:
  workspace: test
  table: { schema: data_layers, name: test_points_yo }
  title: test_points_chagned
```

Titles equal to the layer name, extents, attribute lists and store details all drop out because they are defaults or worked out. `import` can link a layer to a Dataset when the ingest log shows which product loaded its table.

## Known problems this spec has to answer

- **One table per product** (fixed in PR #4). The pipeline loaded one layer of a multi-layer file into one table, so the `openroads` layer held only road nodes, not road links. It now loads every layer into its own table. Run on its own, it names them after the provider's product ID (`openroads_road_link`); run by the tool, it gets the Dataset's name, giving the default above (`os_open_roads_road_link`).
- **Layer-to-style links aren't exported today.** `export-geoserver-config.sh` saves feature types, not GeoServer's layer records, so which style a layer uses isn't in git. `Layer.style` covers it.
- **Client gaps** (tracked in the roadmap): bounding boxes, full JNDI settings, deleting styles, tile cache seed/truncate, layer publishing settings. Layer groups are supported (create, read, delete).

## Decided

- **Scale limits** live in styles ([above](#scale-limits-live-in-styles)), 9 Nov 2026.
- **Layer groups** are in this version as `LayerGroup`, 9 Nov 2026.
- **Layer names inside OS products**, checked with `discover`, 10 Nov 2026: OS Open Roads (GeoPackage) holds `motorway_junction`, `road_link` and `road_node`; the example names two in `tables`, so the third isn't loaded. Boundary-Line holds 18 layers, among them `county`.
- **OS API key** is optional: the OS Downloads API serves OpenData products without one, 10 Nov 2026.
- **CKAN sources** take the portal's address in the Source (`url`), and a Dataset can pin one file of a package with `resource`, 10 Nov 2026.

## Open questions

1. **Dataset versions on screen:** should GeoServer's layer abstract or keywords carry the loaded version automatically?
2. **Several projects on one GeoServer:** fine if each owns its own workspaces; anything else needed?
3. **The working name** `gisdata`: replace when the project is named.
