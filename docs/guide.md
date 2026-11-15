# Guide

How to do each thing with configops. [spec.md](spec.md) describes every kind, field and command; the [README](../README.md) has the short version.

## How to

**Publish a table:** a `Layer` with `table: roads` (in the Project's `database.schema`) or `table: { schema: field_data, name: sites }`. The Project names the database, as GeoServer reaches it (`jndi`) and as configops does (`connection`, to see whether a table exists yet). Until the table exists, `plan` shows the layer as `!` (can't be done yet) and `apply` leaves it, and its place in groups, for later.

**After loading new data,** refresh GeoServer: `configops refresh -C my-project data_layers.roads` works the layer's bounds out again and clears its cached tiles, and those of the groups that hold it. `dataops sync --loaded loaded.txt` writes what it loaded, for `configops refresh --from loaded.txt`.

**Keep the logins out of the files.** A project names Secrets (`credentials: { secret: geoserver-admin }`); configops reads each from a git-ignored `secrets.yaml` in the project folder, from `CONFIGOPS_<SECRET>_<KEY>` environment variables, or from files `$CONFIGOPS_SECRETS_DIR/<secret>/<key>` (how Kubernetes mounts Secrets).

**See what would change, or what's loaded:**

```bash
configops plan   -C my-project    # exit code 2 when there are changes
configops status -C my-project    # layers: published, drifted, tiles cached
configops plan   -C my-project --geoserver https://test.example.org/geoserver   # the same files against another GeoServer
```

**Catch drift.** Run `configops plan` on a schedule: it exits 2 when GeoServer no longer matches the files, say a layer changed in the web UI. `configops apply` puts GeoServer back; or keep the change by running `configops import` (below) and committing it.

**Run it in Kubernetes**, beside GeoServer: [docs/kubernetes.md](kubernetes.md) has a Job that applies the project and an hourly drift check, from an image with your project in it.

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

All of GeoServer's configuration can be kept in the files on standard GeoServer: [docs/coverage.md](coverage.md) goes page by page, and [docs/geoserver-fields.yaml](geoserver-fields.yaml) field by field. GeoServer Cloud has no way to set these, so they work only on standard GeoServer:

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
 GeoServer DataOps, or your own loads ──► PostGIS tables ──── GeoServer reads ───────────┤
                                 └─► configops refresh: bounds, cached tiles        ▼
                                                                     WMS / WFS / WCS / tiles
```
