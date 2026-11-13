# GeoServer ConfigOps

![GeoServer Cloud coverage](https://img.shields.io/badge/GeoServer_Cloud-93%25-brightgreen) ![GeoServer coverage](https://img.shields.io/badge/GeoServer-100%25-brightgreen) Of the 662 settings GeoServer can configure, the e2e tests prove configops sets 618 on GeoServer Cloud (44 more it can't set) and 662 on standard GeoServer ([field by field](docs/geoserver-fields.yaml)).

GeoServer configuration as code, for [GeoServer Cloud](https://github.com/geoserver/geoserver-cloud) and standard GeoServer. `configops` keeps what GeoServer publishes and how it is set up in YAML files, with an OpenTofu-style `plan` and `apply`:

- **publish** layers, styles and layer groups from PostGIS tables, SQL views and other stores (Cloud-Optimised GeoTIFFs, GeoPackages and other files, cascaded WMS, WMTS and WFS, other databases), with tile caching;
- **set GeoServer up:** its settings and services (contact details, titles, limits), and its security (users, groups, roles, who may see and do what, the master password);
- **catch drift and keep it:** `plan` on a schedule shows when GeoServer no longer matches the files, `apply` puts it back, and `import` brings a change made in the web UI back into the files instead. `import` is also how you start from a GeoServer you already have.

Point it at a GeoServer you already run: `https://<host>/geoserver-cloud` for GeoServer Cloud, `https://<host>/geoserver` for standard GeoServer. Standard GeoServer can also take a few settings GeoServer Cloud can't (below); when the files set one, configops asks GeoServer which it is (by the version it reports), and on GeoServer Cloud `plan` shows it as `!` with the reason and leaves it out.

Loading open data into PostGIS (Ordnance Survey, CKAN portals such as data.gov.uk, WFS, OGC API Features) and keeping it up to date is a separate tool, [geodata-sync](https://github.com/Yibbzz/geodata-sync); configops publishes the tables, whoever loads them.

> **Status: early.** The same e2e tests run against GeoServer Cloud and against standard GeoServer 3.0.2 (the version GeoServer Cloud 3.0 is built on). Expect breaking changes. [docs/coverage.md](docs/coverage.md) goes through the web UI page by page; the figures above count every field of GeoServer's configuration, read from its source (`tests/geoserver_coverage.py`), and what one of them can't set counts against it.

## Install

```bash
pip install geoserver-configops                          # the configops command: Python 3.10+, nothing else
docker run --rm ghcr.io/yibbzz/geoserver-configops --help  # or its image
```

## Quick start

A whole GeoServer set up from one file, [`examples/quickstart/project.yaml`](examples/quickstart/project.yaml): a workspace, the contact details and WMS title, a Sentinel-2 image read in place on AWS (a Cloud-Optimised GeoTIFF) with its own style, EOX's cloudless mosaic cascaded from their WMS, both as one cached map, and an editor with what only editors may do. Nothing to load.

```yaml
apiVersion: configops/v1alpha1
kind: Project
metadata: { name: quickstart }
spec:
  geoserver:
    url: http://localhost:8080/geoserver           # or https://<host>/geoserver-cloud
    credentials: { secret: geoserver-admin }       # keys username, password: secrets.yaml, env or a Kubernetes Secret
  workspace: demo
---
# Who runs this GeoServer, as its capabilities documents say
apiVersion: configops/v1alpha1
kind: Settings
metadata: { name: global }
spec:
  contact: { contactPerson: Map Team, contactOrganization: Example Council, contactEmail: maps@example.org }
---
apiVersion: configops/v1alpha1
kind: Service
metadata: { name: wms }
spec: { title: Example Council maps, maxRenderingTime: 60 }
---
# A Sentinel-2 image, a Cloud-Optimised GeoTIFF read in place on AWS
apiVersion: configops/v1alpha1
kind: Store
metadata: { name: sentinel-2 }
spec:
  type: cog
  url: https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/30/U/XC/2025/8/S2C_30UXC_20250825_0_L2A/TCI.tif
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: imagery }
spec: { store: sentinel-2, title: Sentinel-2 true colour, style: imagery }   # styles/imagery.sld
---
# EOX's cloudless mosaic, cascaded from their WMS
apiVersion: configops/v1alpha1
kind: Store
metadata: { name: eox }
spec: { type: wms, url: "https://tiles.maps.eox.at/wms?service=WMS&request=GetCapabilities" }
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: cloudless }
spec: { store: eox, resource: s2cloudless-2024, title: Sentinel-2 cloudless (EOX) }
---
# Both as one map, its tiles cached
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata: { name: basemap }
spec:
  title: Basemap
  layers: [cloudless, imagery]                   # drawn bottom first
  cache: true
---
# An editor, and what only editors may do
apiVersion: configops/v1alpha1
kind: Role
metadata: { name: editor }                       # GeoServer role EDITOR
spec: {}
---
apiVersion: configops/v1alpha1
kind: User
metadata: { name: alice }
spec: { password: { secret: geoserver-users, key: alice }, roles: [EDITOR] }
---
apiVersion: configops/v1alpha1
kind: AccessRule
metadata: { name: demo-write }
spec: { layer: { workspace: demo, layer: "*", access: write }, roles: [EDITOR] }
```

Start a GeoServer to try it on ([`examples/compose.yaml`](examples/compose.yaml)), give configops the logins the file names, and see what it would do:

```bash
docker compose -f examples/compose.yaml up -d --wait    # standard GeoServer on :8080; a minute or two

cat > examples/quickstart/secrets.yaml <<'EOF'          # git-ignored
geoserver-admin: { username: admin, password: geoserver }
geoserver-users: { alice: change-me }
EOF

configops plan -C examples/quickstart
```

```
  + workspace demo
  ~ settings global
      contact.contactPerson: None -> "Map Team"
      contact.contactOrganization: None -> "Example Council"
      contact.contactEmail: None -> "maps@example.org"
  ~ service wms
      title: None -> "Example Council maps"
      maxRenderingTime: 0 -> 60
  + role EDITOR
  + user alice   (groups: none; roles: EDITOR)
  + store demo:sentinel_2   (cog: https://sentinel-cogs.s3.us-west-2.amazonaws.com/...)
  + store demo:eox   (wms: https://tiles.maps.eox.at/wms?service=WMS&request=GetCapabilities)
  + style demo:imagery   (styles/imagery.sld)
  + layer demo:imagery   (cog store sentinel-2)
  + layer demo:cloudless   (wms store eox: s2cloudless-2024)
  + group demo:basemap   (2 members)
  + rule layers demo.*.w   (roles: EDITOR)

Plan: 10 to add, 2 to change, 0 to delete, 0 unmanaged.
```

Make it so, and look: <http://localhost:8080/geoserver/demo/wms?service=WMS&version=1.3.0&request=GetMap&layers=demo:basemap&styles=&crs=EPSG:4326&bbox=50.4,-3.0,51.4,-1.4&width=768&height=480&format=image/png>

```bash
configops apply -C examples/quickstart     # ... Applied 12 change(s).
configops plan  -C examples/quickstart     # No changes: GeoServer matches the files.
```

Now change something in the web UI, say the cloudless layer's title (Layers → cloudless). `plan` notices, with exit code 2, which is what a scheduled drift check looks for:

```
$ configops plan -C examples/quickstart
  ~ layer demo:cloudless
      title: "Cloudless, renamed in the web UI" -> "Sentinel-2 cloudless (EOX)"
```

Either `apply` puts it back, or `import` keeps it: it updates the file in place, only what changed, and leaves the rest of the file, its order and its comments as they were.

```
$ configops import demo -C examples/quickstart
Updated the files: 1 changed: Layer cloudless (title)
Next: git diff, then configops plan -C examples/quickstart

$ configops status -C examples/quickstart
LAYER                 PUBLISHED  DRIFTED  TILES CACHED
demo:imagery          yes        no       no
demo:cloudless        yes        no       no
demo:basemap (group)  yes        no       yes
```

`docker compose -f examples/compose.yaml down -v` removes it all. [docs/spec.md](docs/spec.md) describes every kind and field.

## Examples

| Project | What it holds |
| --- | --- |
| [`examples/quickstart`](examples/quickstart/) | The quick start above: one file, no data to load |
| [`examples/uk-basemap`](examples/uk-basemap/) | The worked example: OS Open Roads, OS Boundary-Line, the Environment Agency's flood zones and Surrey's conservation areas, from tables geodata-sync loads (its own `examples/uk-basemap`); 6 layers with their own styles, a basemap group drawn as one layer, tile caching on British National Grid, WMS and WFS service settings, and a layer on a table you load yourself |
| [`examples/terrain`](examples/terrain/) | A raster in GeoServer's `/data` folder (OS Terrain 50, which geodata-sync's `examples/terrain` makes) drawn with a colour ramp |

## How to

**Publish a table:** a `Layer` with `table: roads` (in the Project's `database.schema`) or `table: { schema: field_data, name: sites }`. The Project names the database, as GeoServer reaches it (`jndi`) and as configops does (`connection`, to see whether a table exists yet). Until the table exists, `plan` shows the layer as `!` (can't be done yet) and `apply` leaves it, and its place in groups, for later.

**After loading new data,** refresh GeoServer: `configops refresh -C my-project data_layers.roads` works the layer's bounds out again and clears its cached tiles, and those of the groups that hold it. `geodata sync --loaded loaded.txt` writes what it loaded, for `configops refresh --from loaded.txt`.

**Keep the logins out of the files.** A project names Secrets (`credentials: { secret: geoserver-admin }`); configops reads each from a git-ignored `secrets.yaml` in the project folder, from `CONFIGOPS_<SECRET>_<KEY>` environment variables, or from files `$CONFIGOPS_SECRETS_DIR/<secret>/<key>` (how Kubernetes mounts Secrets).

**See what would change, or what's loaded:**

```bash
configops plan   -C my-project    # exit code 2 when there are changes
configops status -C my-project    # layers: published, drifted, tiles cached
configops plan   -C my-project --geoserver https://test.example.org/geoserver   # the same files against another GeoServer
```

**Catch drift.** Run `configops plan` on a schedule: it exits 2 when GeoServer no longer matches the files, say a layer changed in the web UI. `configops apply` puts GeoServer back; or keep the change by running `configops import` (below) and committing it.

**Run it in Kubernetes**, beside GeoServer: [docs/kubernetes.md](docs/kubernetes.md) has a Job that applies the project and an hourly drift check, from an image with your project in it.

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

Files in GeoServer's `/data` folder (GeoTIFFs, GeoPackages, shapefiles, FlatGeobuf, PMTiles, GeoParquet, CSV, image mosaics and pyramids, app-schema mappings) are `Store`s with `path:`; SQL Server and Oracle are `Store`s too.

**Publish a SQL view:** a Layer with `sql: { schema, query, key, geometry }` instead of a table.

**Cache tiles:** `cache: true` on a layer or group, or `cache: { gridsets, formats, metaTiles, blobStore, ... }`; a `Gridset` adds your own (British National Grid in uk-basemap) and a `BlobStore` keeps tiles in a folder on the data volume or in S3.

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
 project files (YAML, SLD) ──► configops plan / apply / import / status ──► GeoServer or GeoServer Cloud (REST API)
                                                                                    │
 geodata-sync, or your own loads ──► PostGIS tables ──── GeoServer reads ───────────┤
                                 └─► configops refresh: bounds, cached tiles        ▼
                                                                     WMS / WFS / WCS / tiles
```

## Repository layout

| Path | What |
| --- | --- |
| `configops/`, `pyproject.toml`, `Dockerfile` | The `configops` command and its image |
| `examples/` | Example projects, and `compose.yaml`: a GeoServer and a database to try them on |
| `docs/spec.md` | The file format and every command |
| `docs/kubernetes.md`, `docs/kubernetes/` | Running configops in Kubernetes |
| `docs/coverage.md` | Every page of GeoServer's web UI, and what the tool covers |
| `docs/geoserver-cloud-notes.md` | What we learned about GeoServer Cloud's REST API, GeoWebCache and services: quirks, shapes, timing |
| `tests/` | Unit tests; `tests/e2e/` runs GeoServer Cloud, or standard GeoServer, in Docker for the e2e tests |

## Testing

CI runs on every pull request and push to `main`: shellcheck and a schema check of the Kubernetes example (lint), the unit tests (test), the image build (configops-image), and the e2e tests against GeoServer Cloud in Docker, with its gateway, REST, WMS, WFS, WCS and tile cache services (e2e-cloud), and against standard GeoServer with the same extensions (e2e-standard).

Locally (with `docker`, `curl`, and GDAL's command-line tools, which some e2e tests use to make files):

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                      # e2e tests skip without their services

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

Data you publish keeps its own licence: give a layer the attribution it asks for (`attribution:`). OS OpenData is released under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) and needs "Contains OS data © Crown copyright and database right &lt;year&gt;", as the examples' OS layers say.
