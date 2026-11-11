# GeoServer Cloud: open data, config as code

A Helm chart for [GeoServer Cloud](https://github.com/geoserver/geoserver-cloud), plus `gisdata`, a small command that keeps your GIS data and what GeoServer publishes in YAML files:

- **choose open datasets** from the Ordnance Survey, CKAN portals such as data.gov.uk, WFS and OGC API Features services, or plain links;
- **load them into PostGIS** and keep them up to date: a nightly job checks each one for a new version and loads it with staging, checks and backups;
- **publish them** as GeoServer layers, styles and layer groups, with an OpenTofu-style `plan` and `apply`.

> **Status: early.** It runs end to end on k3s 1.37 with Helm 4, and on kind in CI. Expect breaking changes.

## Quick start

On a local [k3d](https://k3d.io) cluster, with `docker`, `kubectl` and `helm`. This deploys [`examples/quickstart`](examples/quickstart/project.yaml): one dataset from data.gov.uk, published as one layer.

```bash
# 1. A cluster, and the gisdata image built from this repo
k3d cluster create gscloud --agents 0 --k3s-arg "--disable=traefik@server:0"
docker build -t gisdata:local .
k3d image import gisdata:local -c gscloud

# 2. Secrets: set the Postgres and RabbitMQ passwords; geoserver-admin holds
#    GeoServer's admin login, admin / geoserver on a fresh install
cp geoserver-secrets.example.yaml geoserver-secrets.yaml      # git-ignored
kubectl create namespace gscloud
kubectl -n gscloud apply -f geoserver-secrets.yaml

# 3. Install
helm upgrade --install geoserver . -n gscloud -f values.yaml \
  --set gisdata.project=examples/quickstart \
  --set gisdata.image.repository=gisdata --set gisdata.image.tag=local --set gisdata.image.pullPolicy=Never
```

GeoServer takes a few minutes to start. Then the `gisdata-apply-1` Job loads the data and publishes it:

```bash
kubectl -n gscloud logs -f job/gisdata-apply-1
#   + workspace quickstart
#   + store quickstart:data_layers
#   + table data_layers.conservation_areas_conservation_areas   (first sync of dataset conservation-areas)
#   + layer quickstart:conservation_areas
#   ...
# Applied 4 change(s).

kubectl -n gscloud port-forward svc/geoserver-gsc-gateway 8080:8080
```

Open the layer: <http://localhost:8080/geoserver-cloud/wms?service=WMS&version=1.3.0&request=GetMap&layers=quickstart:conservation_areas&styles=&crs=EPSG:27700&bbox=484000,155000,496000,167000&width=512&height=512&format=image/png>

Then change GeoServer's admin password, and put the new one in the `geoserver-admin` Secret.

**Memory.** The whole chart, with all seven GeoServer services, runs in 8 GB: on an 8 GB VM it used about 5 GB, with 2 GB still free while loading 7 million road features. Their memory limits add up to about 12 GB, so on a smaller machine leave out the services you don't need, for example `--set geoserver.services.webui.enabled=false --set geoserver.services.wfs.enabled=false --set geoserver.services.wcs.enabled=false` (gateway, REST, WMS and the tile cache remain).

**Elsewhere.** On another cluster, push the image to a registry it can pull from (`docker build -t <registry>/gisdata .`, `docker push`) and set `gisdata.image.repository` to it.

## A project

A `gisdata` project is a folder of YAML files. This is the quick start's, in full:

```yaml
apiVersion: gisdata/v1alpha1
kind: Project               # where GeoServer and the database are
metadata: { name: quickstart }
spec:
  geoserver:
    url: http://geoserver-gsc-gateway:8080/geoserver-cloud
    credentials: { secret: geoserver-admin }
  database:
    jndi: java:comp/env/jdbc/mypostgis
    connection: { secret: geoserver-pgconfig-secret }
  workspace: quickstart
  crs: EPSG:27700
---
apiVersion: gisdata/v1alpha1
kind: Source                # where data comes from
metadata: { name: data-gov-uk }
spec: { type: ckan, url: https://data.gov.uk }
---
apiVersion: gisdata/v1alpha1
kind: Dataset               # what to load, and how often to check for a new version
metadata: { name: conservation-areas }
spec:
  source: data-gov-uk
  product: conservation-areas125
  update: monthly
  tables:
    conservation_areas: {}
---
apiVersion: gisdata/v1alpha1
kind: Layer                 # what GeoServer publishes
metadata: { name: conservation-areas }
spec:
  dataset: conservation-areas
  table: conservation_areas
  title: Conservation areas (Surrey Heath)
  style: polygon
```

Styles are SLD files in the project's `styles/` folder, and `LayerGroup`s combine layers. [docs/spec/datasets.md](docs/spec/datasets.md) describes every field and command.

## Examples

| Project | What it holds |
| --- | --- |
| [`examples/quickstart`](examples/quickstart/) | One dataset (Surrey Heath conservation areas from data.gov.uk, 10 polygons) and its layer. Loads in seconds |
| [`examples/uk-basemap`](examples/uk-basemap/) | The worked example, and the chart's default: OS Open Roads (7.3M features), OS Boundary-Line, the Environment Agency's flood zones (415k polygons) and the conservation areas; 6 layers with their own styles, a basemap group drawn as one layer, tile caching, and a layer on a table you load yourself. Downloads about 3 GB; the first load takes 20 minutes or so |
| [`examples/smoke`](examples/smoke/) | CI's test project: its data file is served inside the test cluster |

## How to

**Install `gisdata` on your machine** (it also needs GDAL's `ogr2ogr`, `apt install gdal-bin`):

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

**Add a dataset.** Find it, write a Dataset and a Layer, check, then upgrade the release:

```bash
gisdata discover os-downloads                                  # OS products
gisdata discover data-gov-uk --search "flood zones" -C my-project
gisdata discover data-gov-uk <product> -C my-project           # its files and the layers inside
gisdata validate my-project                                    # problems come out as file:line
helm upgrade geoserver . -n gscloud -f values.yaml --set gisdata.project=my-project ...
```

Each `helm upgrade` starts a `gisdata-apply-<revision>` Job that makes GeoServer match the files. The project folder must be inside this repository, since Helm only reads files from the chart.

**See what would change, or what's loaded,** from your machine through port-forwards. Outside the cluster, `gisdata` reads the secrets from a git-ignored `secrets.yaml` in the project folder (or `GISDATA_<SECRET>_<KEY>` environment variables):

```yaml
# my-project/secrets.yaml
geoserver-pgconfig-secret: { host: localhost, port: "5432", database: postgres, username: postgres, password: ... }
geoserver-admin: { username: admin, password: ... }
```

```bash
kubectl -n gscloud port-forward svc/geoserver-gsc-gateway 8080:8080 &
kubectl -n gscloud port-forward svc/geoserver-postgresql 5432:5432 &
gisdata plan   -C my-project --geoserver http://localhost:8080/geoserver-cloud    # exit code 2 when there are changes
gisdata status -C my-project --geoserver http://localhost:8080/geoserver-cloud    # datasets, layers, drift, tile caches
```

**Load new data now** instead of waiting for the nightly job:

```bash
kubectl -n gscloud create job sync-now --from=cronjob/gisdata-sync
kubectl -n gscloud logs -f job/sync-now
```

**Publish a table you load yourself:** a Layer with `table: { schema: field_data, name: sites }` and no `dataset`. Until the table exists, `plan` shows it as `!` (can't be done yet) and `apply` leaves it, and its place in groups, for later.

**Bring in an existing GeoServer workspace:** `gisdata import <workspace> -C my-project` writes the files that describe it, so `plan` on them has nothing to do.

## How it fits together

```
 project files (YAML, SLD) ──► gisdata-project ConfigMap
                                       │
              gisdata-apply Job ◄──────┤ (every helm install/upgrade)
              gisdata-sync CronJob ◄───┘ (nightly)
                       │
 Open data sources ──► │ load: stage, check, back up, swap in
  (OS, CKAN, WFS, …)   ▼
                PostGIS (data_layers) ──► GeoServer Cloud ──► WMS / WFS / WCS / tiles
                                          (gateway, rest, wms, wfs, wcs, gwc, webui)
                       publish: workspace, stores, styles, layers, groups ──┘
```

When `sync` finds a new version, it loads every table into staging, leaves out invalid geometries, checks the row counts, then swaps them all in at once, moving the old ones to the `backup` schema (the newest `backups` copies are kept). Then it refreshes GeoServer: layer bounds, and cached tiles cleared.

## Repository layout

The repository's root is the Helm chart, and the `gisdata` command sits beside it.

| Path | What |
| --- | --- |
| `Chart.yaml`, `values.yaml`, `templates/` | The Helm chart: GeoServer Cloud's services, PostGIS, RabbitMQ, the JNDI datasource, and the gisdata Job and CronJob |
| `charts/application/` | Camptocamp's `application` chart, vendored; it templates the GeoServer services |
| `geoserver-secrets.example.yaml` | The Secrets the chart expects |
| `gisdata/`, `pyproject.toml`, `Dockerfile` | The `gisdata` command and its image |
| `examples/` | Example projects |
| `docs/spec/datasets.md` | The file format and every command |
| `tests/` | Unit tests; `tests/e2e/` runs GeoServer Cloud's REST service in Docker; `tests/kind/` deploys the whole chart |

## Testing

CI runs on every pull request and push to `main`: `helm lint` and schema checks of the rendered chart (lint), the unit tests and a real-PostGIS `sync` (test), the image build (gisdata-image), the e2e tests against GeoServer Cloud's REST service in Docker (e2e-rest), and the whole chart deployed on kind with `examples/smoke` applied, synced and drawn (kind-deploy).

Locally (with `helm`, `jq`, `curl` and `ogr2ogr`):

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                      # integration and e2e tests skip without their services

docker run -d --rm --name test-postgis -p 55432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=gis postgis/postgis:18-3.7
TEST_DB_HOST=localhost TEST_DB_PORT=55432 .venv/bin/pytest -m integration

tests/e2e/up.sh                       # the real REST service; about 1 GB of RAM
E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_DB_PORT=55433 \
  E2E_GWC_URL=http://localhost:59090/geoserver-cloud/gwc/rest .venv/bin/pytest -m e2e
tests/e2e/up.sh down

kind create cluster --name gscloud    # the whole chart; about 8 GB of RAM
tests/kind/deploy-test.sh
```

## Upgrading the vendored chart

```bash
rm -rf charts/application
helm pull camptocamp/application --version <version> --untar -d charts
git diff
```

## Licence and data attribution

The chart is BSD 2-Clause (see `LICENSE.txt`) and builds on Camptocamp's work; see `NOTICE.md` for upstream credits and the licences of the vendored charts.

Data you load keeps its own licence. OS OpenData is released under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) and needs the attribution "Contains OS data © Crown copyright and database right &lt;year&gt;"; `gisdata` adds it to OS layers by itself.
