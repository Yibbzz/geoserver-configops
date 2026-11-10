# GeoServer Cloud: open data, config as code

A Helm deployment of [GeoServer Cloud](https://github.com/geoserver/geoserver-cloud) that:

- **keeps the GeoServer catalog in git**: workspaces, datastores, layers and styles are exported to JSON/SLD files and can be re-imported into a fresh cluster;
- **keeps open data up to date**: the datasets you choose are described in YAML (a `gisdata` project), and a nightly job checks each one for a new version and loads it into PostGIS with staging, validation and backups. Sources: the Ordnance Survey Downloads API, CKAN portals such as data.gov.uk, WFS and OGC API Features services, and plain links.

> **Status: early proof of concept.** It works end to end on a local k3d cluster with OS OpenData. The longer-term aim is a tool where you choose datasets, add your API keys, deploy, and get a version-controlled GIS data server. Expect breaking changes.

## How it fits together

```
 gisdata project (YAML) ──► gisdata-project ConfigMap
                                    │
 Open data sources ──► gisdata-sync CronJob ──► PostGIS (data_layers schema) ──► GeoServer Cloud ──► WMS / WFS / WCS / tiles
  (OS, CKAN, WFS, …)    (gisdata sync)                                     ▲      (gateway, wms, wfs,
                                                                           │       wcs, gwc, rest, webui)
                          geoserver-config/  ◄── export / import ──────────┘
                          (catalog as files in git)
```

| Path | What it is |
| --- | --- |
| `Chart.yaml`, `values.yaml`, `templates/` | Umbrella Helm chart: GeoServer Cloud 3.0 services, PostGIS (`postgis/postgis` image) and RabbitMQ (official image) from this chart's own templates, the JNDI datasource, and the `gisdata sync` CronJob with the project's ConfigMap |
| `charts/` | Vendored camptocamp `application` chart, which templates the GeoServer services |
| `values-local-registry.yaml` | Override to pull images from a local k3d registry instead of Docker Hub |
| `geoserver-secrets.example.yaml` | Template for the Kubernetes secrets the chart expects |
| `export-geoserver-config.sh`, `import-geoserver-config.sh` | Catalog export and import over the GeoServer REST API |
| `geoserver-config/` | The exported catalog (example: a `test` workspace with OS Open Roads, Open Greenspace and Boundary-Line) |
| `gisdata/`, `pyproject.toml`, `Dockerfile` | The `gisdata` command (validate, discover, sync) and its image |
| `examples/uk-basemap/` | A worked `gisdata` project, and the one the chart deploys by default |
| `docs/spec/datasets.md` | The project file format and the commands |

## Quick start

Prerequisites: a Kubernetes cluster (k3d, kind, or managed), `helm`, `kubectl`, `docker` and `jq`.

1. **Create the secrets.**

   ```bash
   cp geoserver-secrets.example.yaml geoserver-secrets.yaml   # git-ignored
   # edit the passwords (an OS Data Hub key is optional: OpenData needs none)
   kubectl apply -f geoserver-secrets.yaml
   ```

2. **Build and push the gisdata image** to a registry your cluster can pull from.

   ```bash
   docker build -t <registry>/gisdata:latest .
   docker push <registry>/gisdata:latest
   ```

3. **Install the chart.** `gisdata.project` is the `gisdata` project to deploy; it must be inside this directory, since Helm reads files only from the chart.

   ```bash
   helm upgrade --install geoserver . -f values.yaml \
     --set gisdata.image.repository=<registry>/gisdata \
     --set gisdata.project=examples/uk-basemap
   ```

   Once GeoServer is up, the `gisdata-apply-<revision>` Job applies the project: workspace, styles, first loads of the datasets, layers, groups. A first apply downloads data, so it can take a while; Helm doesn't wait for it. Follow it with `kubectl logs -f job/gisdata-apply-1`.

   On k3d with a local registry, mirror the GeoServer images first with `./pull_all_images.sh`, then add `-f values-local-registry.yaml`.

4. **Open GeoServer.**

   ```bash
   kubectl port-forward svc/geoserver-gsc-gateway 8080:8080
   # http://localhost:8080/geoserver-cloud/
   ```

   Change the default GeoServer admin password (`admin` / `geoserver`) straight away, and put the new one in the `geoserver-admin` Secret, which `gisdata` logs in with.

## Changing what's published

Edit the project's files and run `helm upgrade`: each upgrade starts a new `gisdata-apply-<revision>` Job, which makes GeoServer match them (`gisdata.apply` in `values.yaml`; `prune: true` also removes what the project's workspace holds that the files don't mention). From a laptop, `gisdata plan` shows what it would change first.

## Keeping data up to date

The `gisdata-sync` CronJob runs `gisdata sync` nightly (`gisdata.sync.schedule` in `values.yaml`) on the project in `gisdata.project`. Each Dataset's own `update` schedule decides whether it is checked that night. For each one it:

1. asks the source for its current version and skips it if that matches the last load (and its tables are still there);
2. downloads it (checking OS's MD5) and loads the layers the Dataset names into staging tables with `ogr2ogr`;
3. leaves out invalid geometries and checks row counts, so nothing goes live unless every table passes;
4. swaps every table in at once, in one transaction, moving the old ones into the backup schema (the newest `backups` copies are kept);
5. records the result in `public.os_ingest_log`.

The project's files reach the job in the `gisdata-project` ConfigMap, which the chart builds from `gisdata.project` (YAML and style files only; a `secrets.yaml` never goes in). The Kubernetes Secrets in `gisdata.secrets` are mounted as files for it.

Run it on demand, for every dataset or just some:

```bash
kubectl create job sync-now --from=cronjob/gisdata-sync
kubectl logs -f job/sync-now
```

### Working on a project locally

```bash
pip install -e .
gisdata validate my-project/                       # check the files
gisdata discover os-downloads OpenRoads            # what a source offers, and the layers inside
gisdata sync -C my-project/ roads                  # load one dataset now
```

Secrets come from `GISDATA_<SECRET>_<KEY>` environment variables or a git-ignored `secrets.yaml` next to the project. See [docs/spec/datasets.md](docs/spec/datasets.md) for the file format and every command.

## Config as code

The GeoServer catalog lives in the `pgconfig` schema in Postgres. To version it:

```bash
kubectl port-forward deployment/geoserver-gsc-rest 9090:8080 &
export GEOSERVER_PASSWORD=...           # GEOSERVER_URL, GEOSERVER_USER, CONFIG_DIR are optional
./export-geoserver-config.sh            # cluster -> geoserver-config/
git diff geoserver-config/
./import-geoserver-config.sh            # geoserver-config/ -> cluster (create or update)
```

Datastores use the JNDI datasource `java:comp/env/jdbc/mypostgis` (`templates/jndi-configmap.yaml`), so no database passwords are stored in the exported files.

## Where this is heading

The goal is a version-controlled way to choose open datasets, keep them up to date and publish them, described in YAML and applied with an OpenTofu-style `plan`/`apply`. The draft format is in [docs/spec/datasets.md](docs/spec/datasets.md), with a worked example in [examples/uk-basemap/](examples/uk-basemap/).

Seven commands are built (see [Keeping data up to date](#keeping-data-up-to-date)). `gisdata validate examples/uk-basemap` checks a project's files against the spec, with every problem reported as `file:line`, and `gisdata discover` lists what a source offers and the layers inside a product: OS (`gisdata discover os-downloads OpenRoads`), any CKAN portal such as data.gov.uk (`gisdata discover ckan --url https://data.gov.uk --search "conservation areas"`), a WFS or OGC API Features service (`gisdata discover wfs --url https://.../wfs`), or a plain link (`gisdata discover url https://.../file.zip`). `gisdata sync` loads new versions of a project's datasets into PostGIS: stage, check, back up, swap in, on each Dataset's own schedule. `gisdata plan` shows what it would take to make GeoServer match the files (workspaces, stores, styles, layers, layer groups, and first loads of missing tables), and `gisdata apply` does it, tile caching and attribution included; `--prune` also removes what the project's workspace holds that the files don't mention. After loading new data, `sync` also refreshes GeoServer: layer bounds, and cached tiles cleared. `gisdata status` shows each dataset (loaded, when, next check, last error) and layer (published, drifted, cached), and `gisdata import <workspace>` writes the files for an existing GeoServer workspace, the way in for an existing install.

## Testing

CI (`.github/workflows/ci.yaml`) runs on every pull request and push to `main`:

| Layer | What runs |
| --- | --- |
| Helm chart | `helm lint`, `kubeconform` schema checks on the rendered manifests, and `tests/test_chart.py` (renders the chart and checks services, images, the sync job and its project ConfigMap, and that every secret it uses is in `geoserver-secrets.example.yaml`) |
| gisdata | Unit tests with sources faked (`tests/test_gisdata_*.py`), and `sync` end to end against a real PostGIS (`tests/test_gisdata_sync_integration.py`) |
| Config scripts | `shellcheck`, and an import-then-export round trip against a fake GeoServer REST API (`tests/test_config_scripts.py`) |
| gisdata image | Docker build, and a smoke test that validates the example project inside it |
| Real GeoServer REST | `tests/e2e/up.sh` starts GeoServer Cloud's REST service, PostGIS and RabbitMQ in Docker with the chart's images and `jndi.yml`; `tests/test_e2e_rest.py` imports `geoserver-config/`, re-imports, exports and checks nothing drifts |
| Full deployment | `tests/kind/deploy-test.sh` installs the whole chart on a kind cluster, waits for every pod, checks WMS, WFS, WCS and WMTS through the gateway, runs the same round trip against it, and renders a layer with WMS GetMap |

Run them locally (needs `helm`, `jq`, `curl`, and `ogr2ogr` for the integration tests):

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                      # integration and e2e tests skip without their services

docker run -d --rm --name test-postgis -p 55432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=gis postgis/postgis:18-3.7
TEST_DB_HOST=localhost TEST_DB_PORT=55432 .venv/bin/pytest -m integration

tests/e2e/up.sh                       # real REST service; about 1 GB of RAM
E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_DB_PORT=55433 .venv/bin/pytest -m e2e
tests/e2e/up.sh down

kind create cluster --name gscloud    # full deployment; needs about 8 GB of RAM
tests/kind/deploy-test.sh
```

## Upgrading the vendored charts

```bash
rm -rf charts/application
helm pull camptocamp/application --version <version> --untar -d charts
git diff
```

## Licence and data attribution

The chart is BSD 2-Clause (see `LICENSE.txt`) and builds on Camptocamp's work; see `NOTICE.md` for upstream credits and the licences of the vendored charts.

Data you load keeps its own licence. OS OpenData is released under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) and needs the attribution "Contains OS data © Crown copyright and database right &lt;year&gt;".
