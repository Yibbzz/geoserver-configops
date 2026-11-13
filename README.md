# GeoServer ConfigOps

![GeoServer Cloud coverage](https://img.shields.io/badge/GeoServer_Cloud-93%25-brightgreen) ![GeoServer coverage](https://img.shields.io/badge/GeoServer-100%25-brightgreen) Of the 662 settings GeoServer can configure, the e2e tests prove configops sets 618 on GeoServer Cloud (44 more it can't set) and 662 on standard GeoServer ([field by field](docs/geoserver-fields.yaml)).

GeoServer configuration and open GIS data as code: `configops` is a small command that keeps your GIS data and what GeoServer publishes in YAML files, for [GeoServer Cloud](https://github.com/geoserver/geoserver-cloud) and standard GeoServer:

- **choose open datasets** from the Ordnance Survey, CKAN portals such as data.gov.uk, WFS and OGC API Features services, or plain links;
- **load them into PostGIS** and keep them up to date: a nightly job checks each one for a new version and loads it with staging, checks and backups;
- **publish them** as GeoServer layers, styles and layer groups, alongside other stores (Cloud-Optimised GeoTIFFs, GeoPackages and other files, cascaded WMS, WMTS and WFS, other PostGIS databases), SQL views and tile caching; set GeoServer's own settings and services (contact details, titles, limits), and its security (users, groups, roles, who may see and do what, the master password), with an OpenTofu-style `plan` and `apply`;
- **catch drift and keep it:** an hourly check shows when GeoServer no longer matches the files, `apply` puts it back, and `import` brings a change made in the web UI back into the files instead.

Point it at a GeoServer you already run: `https://<host>/geoserver-cloud` for GeoServer Cloud, `https://<host>/geoserver` for standard GeoServer. Standard GeoServer can also take a few settings GeoServer Cloud can't (below); when the files set one, configops asks GeoServer which it is (by the version it reports), and on GeoServer Cloud `plan` shows it as `!` with the reason and leaves it out.

> **Status: early.** The same e2e tests run against GeoServer Cloud and against standard GeoServer 3.0.2 (the version GeoServer Cloud 3.0 is built on). Expect breaking changes. [docs/coverage.md](docs/coverage.md) goes through the web UI page by page; the figures above count every field of GeoServer's configuration, read from its source (`tests/geoserver_coverage.py`), and what one of them can't set counts against it.

## Install

```bash
pip install geoserver-configops     # the configops command; loading data also needs GDAL's ogr2ogr (apt install gdal-bin)
docker run --rm ghcr.io/yibbzz/geoserver-configops --help     # or the image, GDAL included
```

## Quick start

With `docker` and `configops`. This starts a GeoServer and a PostGIS database ([`examples/compose.yaml`](examples/compose.yaml)), then applies [`examples/quickstart`](examples/quickstart/project.yaml): one dataset from data.gov.uk, published as one layer.

```bash
docker compose -f examples/compose.yaml up -d --wait    # GeoServer takes a minute or two

cat > examples/quickstart/secrets.yaml <<'EOF'          # the logins project.yaml names (git-ignored)
geoserver-admin: { username: admin, password: geoserver }
database: { host: localhost, port: "5433", database: postgres, username: postgres, password: postgres }
EOF

configops plan  -C examples/quickstart    # what would change
configops apply -C examples/quickstart
#   + workspace quickstart
#   + store quickstart:data_layers
#   + table data_layers.conservation_areas_conservation_areas
#   + layer quickstart:conservation_areas
# Applied 4 change(s).
```

Open the layer: <http://localhost:8080/geoserver/wms?service=WMS&version=1.3.0&request=GetMap&layers=quickstart:conservation_areas&styles=&crs=EPSG:27700&bbox=484000,155000,496000,167000&width=512&height=512&format=image/png>

`docker compose -f examples/compose.yaml down -v` removes it all.

## A project

A `configops` project is a folder of YAML files. This is the quick start's, in full:

```yaml
apiVersion: configops/v1alpha1
kind: Project               # where GeoServer and the database are
metadata: { name: quickstart }
spec:
  geoserver:
    url: http://localhost:8080/geoserver
    credentials: { secret: geoserver-admin }    # GeoServer's admin login
  database:
    jndi: java:comp/env/jdbc/mypostgis          # how GeoServer reaches the database
    connection: { secret: database }            # how configops reaches it
  workspace: quickstart
  crs: EPSG:27700
---
apiVersion: configops/v1alpha1
kind: Source                # where data comes from
metadata: { name: data-gov-uk }
spec: { type: ckan, url: https://data.gov.uk }
---
apiVersion: configops/v1alpha1
kind: Dataset               # what to load, and how often to check for a new version
metadata: { name: conservation-areas }
spec:
  source: data-gov-uk
  product: conservation-areas125
  update: monthly
  tables:
    conservation_areas: {}
---
apiVersion: configops/v1alpha1
kind: Layer                 # what GeoServer publishes
metadata: { name: conservation-areas }
spec:
  dataset: conservation-areas
  table: conservation_areas
  title: Conservation areas (Surrey Heath)
  style: polygon
```

Styles are SLD files in the project's `styles/` folder, with the images and fonts they use beside them; GetFeatureInfo templates go in `templates/`. `LayerGroup`s combine layers. [docs/spec/datasets.md](docs/spec/datasets.md) describes every field and command.

## Examples

| Project | What it holds |
| --- | --- |
| [`examples/quickstart`](examples/quickstart/) | One dataset (Surrey Heath conservation areas from data.gov.uk, 10 polygons) and its layer. Loads in seconds |
| [`examples/uk-basemap`](examples/uk-basemap/) | The worked example: OS Open Roads (7.3M features), OS Boundary-Line, the Environment Agency's flood zones (415k polygons) and the conservation areas; 6 layers with their own styles, a basemap group drawn as one layer, tile caching, WMS and WFS service settings, and a layer on a table you load yourself. Downloads about 3 GB; the first load takes 20 minutes or so |
| [`examples/terrain`](examples/terrain/) | Rasters: OS Terrain 50 (heights of Great Britain, 50 m) made into one Cloud-Optimised GeoTIFF in GeoServer's `/data` folder and drawn with a colour ramp |

## How to

**Add a dataset.** Find it, write a Dataset and a Layer, check, then apply:

```bash
configops discover os-downloads                                  # OS products
configops discover data-gov-uk --search "flood zones" -C my-project
configops discover data-gov-uk <product> -C my-project           # its files and the layers inside
configops validate my-project                                    # problems come out as file:line
configops apply -C my-project
```

**Keep the logins out of the files.** A project names Secrets (`credentials: { secret: geoserver-admin }`); configops reads each from a git-ignored `secrets.yaml` in the project folder, from `CONFIGOPS_<SECRET>_<KEY>` environment variables, or from files `$CONFIGOPS_SECRETS_DIR/<secret>/<key>` (how Kubernetes mounts Secrets).

**See what would change, or what's loaded:**

```bash
configops plan   -C my-project    # exit code 2 when there are changes
configops status -C my-project    # datasets, layers, drift, tile caches
configops plan   -C my-project --geoserver https://test.example.org/geoserver   # the same files against another GeoServer
```

**Catch drift.** Run `configops plan` on a schedule: it exits 2 when GeoServer no longer matches the files, say a layer changed in the web UI. `configops apply` puts GeoServer back; or keep the change by running `configops import` (below) and committing it.

**Run it in Kubernetes**, beside GeoServer: [docs/kubernetes.md](docs/kubernetes.md) has a Job that applies the project, an hourly drift check and a nightly load, from an image with your project in it.

**Load new data now** instead of waiting for the nightly run: `configops sync -C my-project` (`--force <dataset>` loads it even when unchanged).

**Publish data GeoServer reads where it is:** a `Store` for a Cloud-Optimised GeoTIFF (over `https://` or `s3://`), a remote WMS, WMTS or WFS to cascade, or another PostGIS database, and a `Layer` with `store:`:

```yaml
apiVersion: configops/v1alpha1
kind: Store
metadata: { name: sentinel-2-surrey }
spec: { type: cog, url: "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/30/U/XC/2025/8/S2C_30UXC_20250825_0_L2A/TCI.tif" }
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: sentinel-2 }
spec: { store: sentinel-2-surrey, title: Sentinel-2 true colour }
```

Files in GeoServer's `/data` folder (GeoTIFFs, GeoPackages, shapefiles, FlatGeobuf, PMTiles, GeoParquet, CSV, image mosaics and pyramids, app-schema mappings) are `Store`s with `path:`; SQL Server is a `Store` too; a raster `Dataset` downloads and builds the GeoTIFF for one ([`examples/terrain`](examples/terrain)).

**Publish a SQL view:** a Layer with `sql: { schema, query, key, geometry }` instead of a table.

**Cache tiles:** `cache: true` on a layer or group, or `cache: { gridsets, formats, metaTiles, blobStore, ... }`; a `Gridset` adds your own (British National Grid in uk-basemap) and a `BlobStore` keeps tiles in a folder on the data volume or in S3.

**Publish a table you load yourself:** a Layer with `table: { schema: field_data, name: sites }` and no `dataset`. Until the table exists, `plan` shows it as `!` (can't be done yet) and `apply` leaves it, and its place in groups, for later.

**Set contact details, service titles and limits:** a `Settings` resource for GeoServer's global settings (or a workspace's own), and a `Service` per OGC service, in GeoServer's own field names. Only the fields you set are managed:

```yaml
apiVersion: configops/v1alpha1
kind: Service
metadata: { name: wfs }          # wms, wfs, wcs or wmts
spec:
  title: Example Council features
  maxFeatures: 100000
```

**Control who sees and does what:** `Role`, `Group` and `User` resources, with passwords from a Secret, and an `AccessRule` per layer, service or REST rule:

```yaml
apiVersion: configops/v1alpha1
kind: User
metadata: { name: alice }
spec: { password: { secret: geoserver-users, key: alice }, groups: [editors] }
---
apiVersion: configops/v1alpha1
kind: AccessRule
metadata: { name: flood-zones-for-editors }
spec: { layer: { workspace: uk, layer: flood_zones, access: read }, roles: [EDITOR] }
```

`plan` spots a changed password by logging in with it, and a REST rule GeoServer would never reach. GeoServer's own `admin` user, roles and default rules are left alone unless a file names them.

**Bring in an existing GeoServer:** `configops import <workspace> --settings --security -C my-project` writes the files that describe the workspace, GeoServer's settings and services, and its security, so `plan` on them has nothing to do. Passwords can't be read back: put each user's in Secret `geoserver-users`, as the import's note says.

**Allow web pages on other sites to call GeoServer (CORS).** On standard GeoServer, `Settings` `global.cors`. GeoServer Cloud's gateway answers cross-origin requests itself, from any origin; to allow only yours, give the gateway the environment variable `SPRING_APPLICATION_JSON` = `{"spring": {"cloud": {"gateway": {"server": {"webmvc": {"globalcors": {"cors-configurations": {"[/**]": {"allowedOrigins": ["https://maps.example.org"]}}}}}}}}}` (`allowedMethods`, `allowedHeaders`, `allowCredentials` and `maxAge` go beside `allowedOrigins`).

**Let GeoServer fetch remote styles and graphics.** GeoServer Cloud checks every external URL (a style given as `SLD=<url>` in a GetMap, a graphic a style points to) and refuses those no check allows, which at first is all of them. Allow yours with a `URLCheck`:

```yaml
apiVersion: configops/v1alpha1
kind: URLCheck
metadata: { name: council-styles }
spec: { regex: '^https://styles\.example\.org/.*$', description: Our published styles }
```

`Settings` `global.urlChecks: false` turns the checks off. `global.requestLogging: { enabled: true, headers: true }` logs every request in the services' logs.

**Keep a change made in the web UI:** run the same `import` in the project folder. It updates the files in place, only what changed, keeping your names, order and comments; review it with `git diff` and commit. `--prune` also takes out what GeoServer no longer has.

## What can't be done as code

All of GeoServer's configuration can be kept in the files on standard GeoServer: [docs/coverage.md](docs/coverage.md) goes page by page, and [docs/geoserver-fields.yaml](docs/geoserver-fields.yaml) field by field. GeoServer Cloud has no way to set these, so they work only on standard GeoServer:

| What | In the files | Why GeoServer Cloud can't |
| --- | --- | --- |
| Group styles | `LayerGroup` `styles` | Its catalog database doesn't keep their layers: the group breaks after a restart |
| Oracle stores | `Store` `type: oracle` | Its images don't ship Oracle's JDBC driver |
| Tile cache disk quota | `Settings` `global.diskQuota` | It switches the disk quota off and has no REST endpoint for it |
| Content Security Policy | `Settings` `global.contentSecurityPolicy` | Its services don't send GeoServer's policy header; the web UI sends its own |
| CORS | `Settings` `global.cors` | Its gateway answers CORS: set the gateway's instead (above) |

Some settings take effect only when GeoServer's services restart, and `apply` says so: the tile caching defaults, and on standard GeoServer CORS and the disk quota's store. A few belong to how GeoServer is deployed rather than its configuration: which GeoServer Cloud services run, logging, WPS and GeoServer ACL.

## How it fits together

```
 project files (YAML, SLD) ──► configops plan / apply / import ──► GeoServer or GeoServer Cloud (REST API)
                                │                                        │
                                │ sync: stage, check, back up, swap in   │
 Open data sources ───────────► ▼                                        ▼
  (OS, CKAN, WFS, …)        PostGIS (data_layers) ◄── GeoServer reads ── WMS / WFS / WCS / tiles
```

When `sync` finds a new version, it loads every table into staging, leaves out invalid geometries, checks the row counts, then swaps them all in at once, moving the old ones to the `backup` schema (the newest `backups` copies are kept). Then it refreshes GeoServer: layer bounds, and cached tiles cleared.

## Repository layout

| Path | What |
| --- | --- |
| `configops/`, `pyproject.toml`, `Dockerfile` | The `configops` command and its image |
| `examples/` | Example projects, and `compose.yaml`: a GeoServer and a database to try them on |
| `docs/spec/datasets.md` | The file format and every command |
| `docs/kubernetes.md`, `docs/kubernetes/` | Running configops in Kubernetes |
| `docs/coverage.md` | Every page of GeoServer's web UI, and what the tool covers |
| `docs/geoserver-cloud-notes.md` | What we learned about GeoServer Cloud's REST API, GeoWebCache and services: quirks, shapes, timing |
| `tests/` | Unit tests; `tests/e2e/` runs GeoServer Cloud, or standard GeoServer, in Docker for the e2e tests |

## Testing

CI runs on every pull request and push to `main`: shellcheck and a schema check of the Kubernetes example (lint), the unit tests and a real-PostGIS `sync` (test), the image build (configops-image), and the e2e tests against GeoServer Cloud in Docker, with its gateway, REST, WMS, WFS, WCS and tile cache services (e2e-cloud), and against standard GeoServer with the same extensions (e2e-standard).

Locally (with `docker`, `curl` and `ogr2ogr`):

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                      # integration and e2e tests skip without their services

docker run -d --rm --name test-postgis -p 55432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=gis postgis/postgis:18-3.7
TEST_DB_HOST=localhost TEST_DB_PORT=55432 .venv/bin/pytest -m integration

tests/e2e/up.sh                       # GeoServer Cloud; about 3 GB of RAM
E2E_GEOSERVER_URL=http://localhost:59090/geoserver-cloud/rest E2E_SERVICES_URL=http://localhost:59090/geoserver-cloud \
  E2E_DB_PORT=55433 E2E_DATA_DIR=tests/e2e/data E2E_LDAP_URL=ldap://ldap:389 \
  E2E_REST_CONTAINER=gscloud-e2e-rest-1 E2E_WMS_CONTAINER=gscloud-e2e-wms-1 .venv/bin/pytest -m e2e
tests/e2e/up.sh down

tests/e2e/up.sh standard              # standard GeoServer instead (downloads its extensions once)
E2E_GEOSERVER_URL=http://localhost:59091/geoserver/rest E2E_SERVICES_URL=http://localhost:59091/geoserver \
  E2E_GWC_URL=http://localhost:59091/geoserver/gwc/rest E2E_DB_PORT=55433 E2E_DB_HOST_IN_GEOSERVER=db \
  E2E_DATA_DIR=tests/e2e/data E2E_LDAP_URL=ldap://ldap:389 E2E_REST_CONTAINER=gscloud-e2e-geoserver-1 .venv/bin/pytest -m e2e
tests/e2e/up.sh down
```

## Releasing

Set the version in `pyproject.toml` and `configops/__init__.py`, merge, then push the tag `v<version>`: the Release workflow publishes the package to PyPI (`geoserver-configops`), the image to `ghcr.io/yibbzz/geoserver-configops`, and a GitHub release.

## Licence and data attribution

BSD 2-Clause (see `LICENSE.txt`); see `NOTICE.md` for credits.

Data you load keeps its own licence. OS OpenData is released under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) and needs the attribution "Contains OS data © Crown copyright and database right &lt;year&gt;"; `configops` adds it to OS layers by itself.
