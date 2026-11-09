# Dataset spec (draft, `v1alpha1`)

A set of YAML files that says which open datasets you want, where they come from, how often they update, and how they are published and styled in GeoServer Cloud. A small command-line tool reads the files and makes PostGIS and GeoServer match them, the way OpenTofu makes infrastructure match its configuration.

> **Status:** first draft for review, 9 Nov 2026. Nothing implements it yet. `gisdata` is a working name for the tool and the `apiVersion` group; both will change when the project is named.

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
kind: Layer                 # Project, Source, Dataset, Layer or Style
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
  credentials: { secret: os-api }    # key: apiKey
---
apiVersion: gisdata/v1alpha1
kind: Source
metadata:
  name: web
spec:
  type: url                          # plain file downloads, no key
```

| `type` | Lists products (`discover`) | Needs a key | Status |
| --- | --- | --- | --- |
| `os-downloads` | yes | yes (free OS Data Hub key) | exists today in the pipeline |
| `url` | no: you give the URL | no | to build |
| `ckan` (data.gov.uk and others) | yes | no | later |
| `ogc-api-features`, `wfs` | yes (collections, feature types) | usually no | later |

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
| `gisdata apply [--prune]` | Makes the changes `plan` shows, in order: workspace, store, styles, tables (first sync), layers, tile cache |
| `gisdata sync [dataset]` | Checks sources for new versions and loads them (stage, validate, back up, swap in), then refreshes extents and clears cached tiles of the layers on them. The nightly job |
| `gisdata status` | Per dataset: version loaded, when, rows, last error, next check. Per layer: published, drifted, tiles cached |
| `gisdata import` | Reads a live GeoServer workspace and writes the files that describe it; the way in for an existing install |

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

- **One table per product today.** The pipeline loads one layer of a multi-layer file into one table: the `openroads` layer holds only road nodes (points), not road links. `tables` fixes this in the spec; the pipeline needs the same change.
- **Layer-to-style links aren't exported today.** `export-geoserver-config.sh` saves feature types, not GeoServer's layer records, so which style a layer uses isn't in git. `Layer.style` covers it.
- **Client gaps** (tracked in the roadmap): bounding boxes, full JNDI settings, deleting styles, tile cache seed/truncate, layer publishing settings.

## Open questions

1. **Layer names inside OS products.** The examples assume OS Open Roads contains `road_link` and `road_node`; confirm with `discover` against the real API.
2. **Scale limits** (show roads only below 1:100,000): leave them inside style files, or add `minScale`/`maxScale` to `Layer` and have the tool write them into the style?
3. **Layer groups** (a basemap made of several layers): a `LayerGroup` kind now, or later?
4. **Dataset versions on screen:** should GeoServer's layer abstract or keywords carry the loaded version automatically?
5. **Several projects on one GeoServer:** fine if each owns its own workspaces; anything else needed?
6. **The working name** `gisdata`: replace when the project is named.
