<p align="center"><img src="docs/logo.svg" alt="GeoServer ConfigOps" width="120"></p>

<h1 align="center">GeoServer ConfigOps</h1>

<p align="center">GeoServer configuration as code, with <code>plan</code> and <code>apply</code>.<br>For <a href="https://github.com/geoserver/geoserver-cloud">GeoServer Cloud</a> and standard GeoServer.</p>

![GeoServer Cloud 3.0.2.1 coverage](https://img.shields.io/badge/GeoServer_Cloud_3.0.2.1-93%25-brightgreen) ![GeoServer 3.0.2 coverage](https://img.shields.io/badge/GeoServer_3.0.2-100%25-brightgreen) ![GeoServer 2.28.6 coverage](https://img.shields.io/badge/GeoServer_2.28.6-98%25-brightgreen)

Of the 662 settings GeoServer can configure, the e2e tests prove configops sets 618 on GeoServer Cloud (44 more it can't set), 662 on standard GeoServer and 653 on GeoServer 2.28 (9 more it can't set) ([field by field](docs/geoserver-fields.yaml)).

<!-- tested-on -->Tested on **GeoServer Cloud 3.0.2.1**, **GeoServer 3.0.2** and **GeoServer 2.28.6**: every pull request runs the e2e tests on all three, and a daily job (`.github/workflows/versions.yaml`) tries each new release.<!-- /tested-on -->

`configops` keeps what GeoServer publishes, and how it is set up, in YAML files under version control:

- **Publish** layers, styles and layer groups from PostGIS tables, SQL views, Cloud-Optimised GeoTIFFs, files, and cascaded WMS, WMTS and WFS, with tile caching.
- **Configure** global and service settings, and security: users, groups, roles, access rules, authentication.
- **Detect drift.** `plan` shows where GeoServer no longer matches the files; `apply` puts it back, and `import` brings a change made in the web UI into the files.

It talks to GeoServer's REST API, so it works with a GeoServer you already run. Nothing is installed in GeoServer.

> **Status: 0.x.** The file format may still change between releases.

## Install

```bash
pip install geoserver-configops                            # Python 3.10+
docker run --rm ghcr.io/yibbzz/geoserver-configops --help  # or the image
```

## Example

[`examples/city-maps/project.yaml`](examples/city-maps/project.yaml): two PostGIS tables published as layers, one with only some of its columns and readable only by one role, both as a cached basemap.

```yaml
apiVersion: configops/v1alpha1
kind: Project
metadata: { name: city-maps }
spec:
  geoserver:
    url: https://maps.example.org/geoserver-cloud
    credentials: { secret: geoserver-admin }        # never in the files: a Secret, an env var or secrets.yaml
  database:
    jndi: java:comp/env/jdbc/postgis                # how GeoServer reaches PostGIS
    connection: { secret: postgis }                 # how configops checks a table exists
  workspace: city
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: roads }
spec: { table: roads, title: Roads, style: roads }  # styles/roads.sld
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: parcels }
spec:
  table: parcels
  title: Land parcels
  attributes: [parcel_id, area]                     # publish only these columns
---
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata: { name: basemap }
spec: { title: Basemap, layers: [parcels, roads], cache: true }
---
apiVersion: configops/v1alpha1
kind: Role
metadata: { name: surveyor }
spec: {}
---
apiVersion: configops/v1alpha1
kind: AccessRule
metadata: { name: parcels-for-surveyors }
spec: { layer: { workspace: city, layer: parcels, access: read }, roles: [SURVEYOR] }
```

```console
$ configops plan -C examples/city-maps
  + workspace city
  + role SURVEYOR
  + rule layers city.parcels.r   (roles: SURVEYOR)
  + store city:public   (PostGIS through java:comp/env/jdbc/postgis)
  + style city:roads   (styles/roads.sld)
  + layer city:roads   (public.roads)
  + layer city:parcels   (public.parcels)
  + group city:basemap   (2 members)

Plan: 8 to add, 0 to change, 0 to delete, 0 unmanaged.

$ configops apply -C examples/city-maps
$ configops plan  -C examples/city-maps
No changes: GeoServer matches the files.
```

To try it without a database, [`examples/quickstart`](examples/quickstart/) publishes a Sentinel-2 image and a cascaded WMS on a GeoServer started with `docker compose -f examples/compose.yaml up -d --wait`.

## Common tasks

**Publish data GeoServer reads where it is**, such as a Cloud-Optimised GeoTIFF or a remote WMS:

```yaml
apiVersion: configops/v1alpha1
kind: Store
metadata: { name: sentinel-2 }
spec: { type: cog, url: "https://example.org/imagery/TCI.tif" }
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: imagery }
spec: { store: sentinel-2, title: Sentinel-2 true colour, cache: true }
```

**Keep logins out of the files.** A file names a Secret; its value comes from a Kubernetes Secret, an environment variable or a git-ignored `secrets.yaml`. `plan` never prints one.

```yaml
apiVersion: configops/v1alpha1
kind: Store
metadata: { name: partner-wms }
spec:
  type: wms
  url: https://partner.example.org/wms
  credentials: { secret: partner-login }          # keys: username, password
```

**Check for drift on a schedule.** `plan` exits 0 when GeoServer matches the files, 2 when it doesn't, and 1 on an error (3 when all that is left are layers waiting for their tables):

```yaml
# .github/workflows/drift.yaml
on: { schedule: [{ cron: "0 * * * *" }] }
jobs:
  drift:
    runs-on: ubuntu-latest
    container: ghcr.io/yibbzz/geoserver-configops:0.2.0
    steps:
      - uses: actions/checkout@v7
      - run: configops plan -C . --fail-on-unmanaged   # also fails on a user or rule added in the web UI
        env:
          CONFIGOPS_GEOSERVER_ADMIN_USERNAME: ${{ secrets.GEOSERVER_USER }}
          CONFIGOPS_GEOSERVER_ADMIN_PASSWORD: ${{ secrets.GEOSERVER_PASSWORD }}
```

**Apply exactly the plan that was reviewed:**

```bash
configops plan  -C my-project --digest            # ... Plan digest: 3f9c2a7be1d04c55
configops apply -C my-project --expect 3f9c2a7be1d04c55
```

**Adopt an existing GeoServer.** `import` writes the files that describe it, so the next `plan` has nothing to do:

```bash
configops import city --settings --security -C my-project
```

**Run it in Kubernetes** as a Job that applies and a CronJob that checks for drift: [docs/kubernetes.md](docs/kubernetes.md).

More in the [guide](docs/guide.md): SQL views, tile caching and gridsets, service settings, users and rules, refreshing after a data load.

## Built to be safe to run

- **Nothing changes without a plan.** `plan` only reads. `apply` prints each change before making it and stops at the first failure.
- **Secrets stay secret.** Passwords, keys and tokens come from Secrets and are never printed, also when they differ.
- **Access rules come first.** A rule is made before the layer it protects and removed after it.
- **One apply at a time.** `apply` takes a lock in GeoServer, so a pipeline and a person can't run through each other.
- **Deleting is opt-in and limited.** Only `--prune` deletes, and it stops above `--max-deletes` (10 by default).
- **Checked before it is sent.** `configops validate` reads the files without a network: the schema, names that would clash, styles GeoServer couldn't parse.

## What to know

- `apply` is not a transaction. If it stops part-way, what it already changed stays; running it again finishes.
- Only the fields a file sets are managed. Taking a field out of a file leaves GeoServer's value as it is.
- GeoServer creates a layer enabled, whatever it is asked. A new layer with `enabled: false` or `attributes` is switched off in the next request; give it an `AccessRule` if that moment matters.
- Some settings exist only on standard GeoServer (group styles, Oracle stores, the tile cache disk quota, the Content Security Policy, CORS). On GeoServer Cloud `plan` marks them `!` with the reason and leaves them out: [details](docs/guide.md#what-cant-be-done-as-code).

## Documentation

| | |
| --- | --- |
| [docs/guide.md](docs/guide.md) | How to do each thing, with examples |
| [docs/spec.md](docs/spec.md) | The file format and every command |
| [docs/kubernetes.md](docs/kubernetes.md) | Running it beside GeoServer in a cluster |
| [docs/coverage.md](docs/coverage.md) | GeoServer's web UI page by page, and what is covered |
| [docs/geoserver-cloud-notes.md](docs/geoserver-cloud-notes.md) | What we learned about GeoServer Cloud's REST API and tile cache |
| [docs/development.md](docs/development.md) | Tests, CI, new GeoServer versions, releasing |

Loading data into PostGIS is a separate tool, [geodata-sync](https://github.com/Yibbzz/geodata-sync); configops publishes the tables, whoever loads them.

## Licence

MIT (see `LICENSE`); see `NOTICE.md` for credits. Data you publish keeps its own licence: the examples' Ordnance Survey layers contain OS data © Crown copyright and database right, under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
