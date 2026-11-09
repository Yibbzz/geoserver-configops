# GeoServer Cloud: open data, config as code

A Helm deployment of [GeoServer Cloud](https://github.com/geoserver/geoserver-cloud) that:

- **keeps the GeoServer catalog in git**: workspaces, datastores, layers and styles are exported to JSON/SLD files and can be re-imported into a fresh cluster;
- **keeps open data up to date**: a nightly job pulls datasets from open data APIs (currently the Ordnance Survey Downloads API), checks for new versions, and loads them into PostGIS with staging, validation and backups.

> **Status: early proof of concept.** It works end to end on a local k3d cluster with OS OpenData. The longer-term aim is a tool where you choose datasets, add your API keys, deploy, and get a version-controlled GIS data server. Expect breaking changes.

## How it fits together

```
 Open data APIs ──► ingest CronJob ──► PostGIS (data_layers schema) ──► GeoServer Cloud ──► WMS / WFS / WCS / tiles
  (OS Downloads)    (auto-update-development/)                      ▲      (gateway, wms, wfs,
                                                                     │       wcs, gwc, rest, webui)
                          geoserver-config/  ◄── export / import ────┘
                          (catalog as files in git)
```

| Path | What it is |
| --- | --- |
| `Chart.yaml`, `values.yaml`, `templates/` | Umbrella Helm chart: GeoServer Cloud 3.0 services, PostGIS (`postgis/postgis` image) and RabbitMQ (official image) from this chart's own templates, the JNDI datasource and the ingest CronJob |
| `charts/` | Vendored camptocamp `application` chart, which templates the GeoServer services |
| `values-local-registry.yaml` | Override to pull images from a local k3d registry instead of Docker Hub |
| `geoserver-secrets.example.yaml` | Template for the Kubernetes secrets the chart expects |
| `export-geoserver-config.sh`, `import-geoserver-config.sh` | Catalog export and import over the GeoServer REST API |
| `geoserver-config/` | The exported catalog (example: a `test` workspace with OS Open Roads, Open Greenspace and Boundary-Line) |
| `auto-update-development/` | The ingest job: Python pipeline, Dockerfile, and a docker-compose dev environment |

## Quick start

Prerequisites: a Kubernetes cluster (k3d, kind, or managed), `helm`, `kubectl`, `docker`, `jq`, and a free [OS Data Hub](https://osdatahub.os.uk/) API key for OS data.

1. **Create the secrets.**

   ```bash
   cp geoserver-secrets.example.yaml geoserver-secrets.yaml   # git-ignored
   # edit the passwords and add your OS API key
   kubectl apply -f geoserver-secrets.yaml
   ```

2. **Build and push the ingest image** to a registry your cluster can pull from.

   ```bash
   docker build -t <registry>/geoserver-ingest:latest auto-update-development
   docker push <registry>/geoserver-ingest:latest
   ```

3. **Install the chart.**

   ```bash
   helm upgrade --install geoserver . -f values.yaml \
     --set ingest.image.repository=<registry>/geoserver-ingest
   ```

   On k3d with a local registry, mirror the GeoServer images first with `./pull_all_images.sh`, then add `-f values-local-registry.yaml`.

4. **Open GeoServer.**

   ```bash
   kubectl port-forward svc/geoserver-gsc-gateway 8080:8080
   # http://localhost:8080/geoserver-cloud/
   ```

   Change the default GeoServer admin password (`admin` / `geoserver`) straight away.

## Data ingest

The `geoserver-data-ingest` CronJob runs nightly (`ingest.schedule` in `values.yaml`). For each dataset it:

1. skips it if the version matches the last successful load;
2. downloads and verifies the archive (MD5 when the source provides one);
3. loads it into the staging schema with `ogr2ogr`;
4. checks row counts and geometry validity;
5. backs up the live table into the backup schema (keeps the newest `ingest.backupRetention` copies);
6. swaps the new data into `data_layers`, dropping invalid geometries, then adds a spatial index;
7. records the result in `public.os_ingest_log`.

Run it on demand:

```bash
kubectl create job ingest-manual --from=cronjob/geoserver-data-ingest
kubectl logs -f job/ingest-manual
```

Choose which OS products to load with `ALLOWLIST` / `BLOCKLIST` in `auto-update-development/ingest_data/sources/os_api.py`.

### Adding a data source

Create `auto-update-development/ingest_data/sources/<name>.py` with a `get_datasets()` function that returns a list of dicts:

```python
{
    "id": "OpenRoads",           # stable ID, also the table name (lowercased)
    "name": "OS Open Roads",
    "version": "2026-04",        # a change triggers a reload
    "format": "GeoPackage",      # GeoPackage, ESRI® Shapefile, GML, MapInfo® TAB
    "url": "https://...",        # zip archive
    "md5": "...",                # optional
    "srs": "EPSG:27700",         # optional, source CRS
    "headers": {"key": "..."},   # optional, e.g. API key
}
```

Then add it to `SOURCES` in `main.py`.

### Local development

```bash
cd auto-update-development
cp .env.example .env    # add OS_API_KEY
docker compose up -d    # dev container + PostGIS
docker compose exec dev python ingest_data/main.py
```

A VS Code devcontainer config is included.

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

The first command is built: `pip install -e .`, then `gisdata validate examples/uk-basemap` checks a project's files against the spec, with every problem reported as `file:line`.

## Testing

CI (`.github/workflows/ci.yaml`) runs on every pull request and push to `main`:

| Layer | What runs |
| --- | --- |
| Helm chart | `helm lint`, `kubeconform` schema checks on the rendered manifests, and `tests/test_chart.py` (renders the chart and checks services, images, the ingest job and that every secret it uses is in `geoserver-secrets.example.yaml`) |
| Ingest pipeline | Unit tests with the OS API faked (`tests/test_os_api.py`, `tests/test_pipeline_unit.py`), and end-to-end runs against a real PostGIS (`tests/test_pipeline_integration.py`) |
| Config scripts | `shellcheck`, and an import-then-export round trip against a fake GeoServer REST API (`tests/test_config_scripts.py`) |
| Ingest image | Docker build and a smoke test |
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
