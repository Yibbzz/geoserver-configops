# What the tool covers

Every page of GeoServer's web UI, and whether `configops` can define it in files and apply it with `plan`/`apply`. The aim is everything that can be configured, as code. Update this page in the same PR as any change to what's covered.

| Status | Meaning |
| --- | --- |
| **Yes** | In the files: `plan` shows drift, `apply` sets it, `import` writes it |
| **Partly** | Some of it; the notes say what's missing |
| **Not yet** | Possible through GeoServer Cloud's REST API; not built |
| **Can't** | GeoServer Cloud's REST API has no way to set it |
| **Chart** | Set in the Helm chart's `values.yaml`, not in the project files |
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
| Workspace service settings (its own WMS, WFS…) | Can't | No REST endpoint in GeoServer Cloud |
| Workspace administrators | Not yet | GeoServer ACL's workspace admin rules; the ACL service is off in the chart |

### Stores

| Store type | Status | Notes |
| --- | --- | --- |
| PostGIS through the JNDI datasource | Yes | Made for you: one store per schema the layers use |
| PostGIS with a password | Yes | `Store` `type: postgis`, login from a Secret |
| Oracle, SQL Server | Not yet | |
| GeoPackage, Shapefile, directory of shapefiles, FlatGeobuf, PMTiles | Yes | `Store` with `path:` on the shared data volume (`values-data-volume.yaml`); PMTiles also over `url:` |
| GeoParquet, property files | Not yet | |
| Remote WFS (cascaded) | Yes | `Store` `type: wfs` |
| Remote WMS, WMTS (cascaded) | Yes | `Store` `type: wms`, `wmts` |
| Cloud-Optimised GeoTIFF (HTTP, S3, Azure, Google) | Yes | `Store` `type: cog` |
| GeoTIFF on disk | Yes | `Store` `type: geotiff` with `path:` |
| Image mosaic, world image, ArcGrid | Yes | `Store` `type: imagemosaic`, `worldimage`, `arcgrid` |
| Image pyramid | Not yet | |
| App-schema (complex features) | Not yet | |
| Store description, enabled | Yes | For `Store`s; the JNDI PostGIS stores have none |

### Layers

| Tab and setting | Status | Notes |
| --- | --- | --- |
| **Data:** name, title, abstract, keywords | Yes | |
| Enabled, advertised | Yes | |
| Declared CRS | Yes | `crs`, forced on the layer |
| Bounding boxes | Yes | Worked out from the data on every apply and after every load |
| Metadata links | Yes | `metadataLinks` |
| Data links | Not yet | |
| Restrict features (CQL filter) | Yes | `filter` |
| Attributes shown | Partly | `attributes`; `import` doesn't write them (REST can't tell a narrowed list from the full one) |
| SQL views | Yes | `sql: { schema, query, key, geometry }` |
| Raster band and coverage parameters | Not yet | GeoServer works them out from the file |
| **Publishing:** default style, other styles | Yes | |
| Queryable | Yes | |
| Opaque | Yes | `opaque` |
| Attribution title | Yes | From the file, or the source's licence for OS data |
| Attribution link and logo | Yes | `attribution: { title, link, logo }` |
| Rendering buffer, HTTP cache headers | Yes | `buffer`, `httpCache` |
| Per-layer WFS settings (max features, decimals, other CRSs) | Yes | `wfs` |
| Per-layer WMS and WCS settings | Not yet | |
| **Dimensions:** time, elevation | Yes | `dimensions` |
| Custom dimensions | Not yet | |
| **Tile caching:** on or off, gridsets, image formats | Yes | `cache:` |
| Metatiling, gutter, expiry | Yes | `cache: { metaTiles, gutter, expireCache, expireClients }` |
| Parameter filters | Not yet | GeoWebCache adds the STYLES one by itself |
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
| Root layer (EO groups) | Not yet | The EO mode isn't offered |

### Styles

| Setting | Status | Notes |
| --- | --- | --- |
| SLD, CSS, MBStyle files | Yes | Files in `styles/`, uploaded as written |
| Workspace styles | Yes | |
| Global styles | Yes | `Style` `global: true`; GeoServer's built-in ones are used without defining them |
| Legend graphic | Yes | `Style` `legend` |

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
| WPS | Chart | Off in the chart (`geoserver.services.wps.enabled`) |
| OGC API Features | Not yet | |
| Turning a service on or off | Partly | `enabled:` in a `Service`; whether the service runs at all is `geoserver.services.<name>.enabled` in the chart |

## Settings

| Page | Status | Notes |
| --- | --- | --- |
| Global | Yes | `Settings`: proxy address, decimals, charset, verbose, error handling, web UI mode and the rest |
| Contact information | Yes | `Settings` `contact:` |
| JAI, coverage access | Partly | Through `Settings` `global.geoserver:`; not listed in the schema |
| Image processing | Partly | As JAI |
| Logging | Chart | GeoServer Cloud logs through Spring Boot, set in the chart's environment |

## Tile caching

| Page | Status | Notes |
| --- | --- | --- |
| Tile layers | Yes | Each layer's and group's `cache:` |
| Caching defaults | Not yet | |
| Gridsets (your own) | Yes | `Gridset`, e.g. British National Grid |
| Disk quota | Can't | `/gwc/rest/diskquota` answers 404 in GeoServer Cloud |
| Blob stores: a folder, S3 | Yes | `BlobStore` `type: file` (on the data volume) or `s3`; `cache.blobStore` |
| Blob stores: Azure, Google Cloud Storage | Not yet | In the images; not tested |
| Seeding, truncating | Partly | Cached tiles are cleared after each load; seeding isn't built |

## Security

| Page | Status | Notes |
| --- | --- | --- |
| Users, groups, roles | Yes | `User`, `Group`, `Role`, in GeoServer's default user/group and role services; passwords from Secrets |
| Data rules | Yes | `AccessRule` `layer:` |
| Service rules | Yes | `AccessRule` `service:` |
| REST rules | Yes | `AccessRule` `rest:`, kept in order |
| Catalog mode | Yes | `Settings` `global.catalogMode` |
| Settings (active role service, encryption) | Not yet | |
| Authentication: providers, filters | Not yet | REST has them, but a filter does nothing outside a filter chain |
| Filter chains, role services | Can't | No REST endpoint in GeoServer Cloud (404) |
| Other user/group and role services (LDAP, JDBC) | Not yet | |
| Master password | Yes | `Settings` `global.masterPassword` from a Secret; `import` doesn't write it |
| Password policies | Can't | No REST endpoint in GeoServer Cloud |
| GeoServer ACL | Chart | Off in the chart (`geoserver.services.acl.enabled`) |

## Not configuration

Layer preview, demos, server status, the reprojection console and the importer's web pages are tools, not settings: nothing to keep in files.

## Deployment (the Helm chart)

Which services run, their images, memory and CPU, health checks, ingress, PostGIS and RabbitMQ, the JNDI datasource, and configops's apply Job, sync and drift CronJobs are all in `values.yaml`.
