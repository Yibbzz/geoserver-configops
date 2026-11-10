# CLAUDE.md

Notes for agents working in this repo: what it is, how the owner wants work done, how to test, what has been learned the hard way, and what to pick up next.

## What this is

A Helm chart for [GeoServer Cloud](https://github.com/geoserver/geoserver-cloud) plus `gisdata`, a small Python CLI that keeps open GIS data and its GeoServer publishing in YAML files ("config as code", OpenTofu-style `plan`/`apply`). The file format and every command are specified in `docs/spec/datasets.md`; keep that spec in step with the code.

| Path | What |
| --- | --- |
| `gisdata/` | The CLI. `validate`, `discover`, `sync`, `plan`, `apply`, `status`, `import` (`cli.py` wires them) |
| `gisdata/sources/` | Source types: `os_downloads`, `ckan`, `url`, `wfs`, `ogcapi` (OGC API Features) |
| `gisdata/geoserver.py` | Our own thin GeoServer REST + GeoWebCache client (deliberately not the `geoservercloud` PyPI package) |
| `gisdata/ingest.py`, `sync.py` | Loading into PostGIS: stage, check, swap in one transaction, backups, ingest log |
| `gisdata/plan.py` | `plan`/`apply`: compare files with live GeoServer, make the changes in dependency order |
| `templates/gisdata.yaml` | Project ConfigMap, `gisdata-apply-<revision>` Job, `gisdata-sync` CronJob |
| `examples/uk-basemap/` | The worked example and the chart's default project |
| `examples/smoke/` | Minimal whole project, deployed by the kind test |
| `export-/import-geoserver-config.sh`, `geoserver-config/` | Older catalog scripts, superseded by `gisdata`; due for removal (see Next) |

## How the owner wants work done

- **No Claude attribution anywhere**: no `Co-Authored-By` trailers, no "Generated with Claude Code" in commits, squash messages or PR bodies.
- **One branch and PR per piece of work**, squash-merged. Merge with an explicit title and body so GitHub adds nothing:
  `gh api -X PUT repos/Yibbzz/geoserver_cloud/pulls/<n>/merge -f merge_method=squash -f commit_title="<title> (#<n>)" -f commit_message="<body>"`, then delete the branch. `gh pr edit` fails on this repo (Projects classic error); use `gh api` PATCH instead.
- **Merge only when all five CI jobs are green**: lint, test, gisdata-image, e2e-rest, kind-deploy. Check the kind job's log when it matters: tests can skip silently.
- **Lightweight above all.** The owner worries about maintenance: prefer small in-repo modules over new dependencies, no speculative features, cut what isn't pulling its weight. `discover` (`gisdata/sources/`) may be cut later if it becomes a burden.
- **Plain, short English** in docs, messages and output. Test against the real thing (real GeoServer Cloud, real PostGIS, real data sources) before calling something done; several bugs were only found that way.
- Force-pushes are blocked by the session's safety rules: after a squash-merge of a stacked PR, merge `main` into the next branch rather than rebasing.

## Testing

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # GDAL's ogr2ogr/ogrinfo needed too (apt install gdal-bin)
.venv/bin/python -m pytest -q                                            # unit tests; DB and GeoServer tests skip
```

- **PostGIS integration** (`tests/test_gisdata_sync_integration.py`): `docker run -d --rm --name gisdata-sync-dev -p 55432:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=gis postgis/postgis:18-3.7`, then `TEST_DB_HOST=localhost TEST_DB_PORT=55432 pytest`.
- **Real GeoServer Cloud** (`-m e2e`): `tests/e2e/up.sh` starts the REST service, PostGIS and RabbitMQ in Docker; then
  `E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_DB_PORT=55433 E2E_GWC_URL=http://localhost:59090/geoserver-cloud/gwc/rest pytest -m e2e`. `tests/e2e/up.sh down` stops it. Clean up any workspaces you create by hand: `tests/test_e2e_rest.py` exports the whole catalog.
- **Whole chart on kind** (`tests/kind/deploy-test.sh`, run by CI): installs everything, deploys `examples/smoke` through the apply Job, checks `plan` is clean, WMS draws the layer, sync runs, and the e2e tests pass through the gateway.

## GeoServer Cloud quirks (learned the hard way)

- PUT on a missing object fails: look first, then PUT or POST (`GeoServer.upsert`).
- Styles are created by POSTing the style file itself with `?name=`; JSON style bodies are refused.
- Layer group mode "opaque" is `OPAQUE_CONTAINER` in the API.
- GeoWebCache: through the gateway at `<url>/gwc/rest`; the REST service reached directly serves it under `/geoserver-cloud/gwc/rest` (`GISDATA_GWC_URL`). Pick the format with the `Accept` header, not an extension; write XML (its JSON writing fails); keep the `:` in `ws:name` unencoded.
- GeoServer Cloud caches every new layer by itself, so `cache: false` (the spec's default) means apply deletes that cache.
- The tile cache is its own service that starts after REST: `--wait` checks both.
- Kubernetes mounts a ConfigMap as symlinks into `..data`, so project folders are symlinked directories: the loader must follow them (`os.walk(followlinks=True)`).
- GeoJSON always counts as WGS84: data in another CRS needs `tables.<layer>.crs`.

## Current state (10 Nov 2026)

Every command in the spec is built and merged (PRs #6 to #13). **PR #14 is open**: the `gisdata-apply-<revision>` Job, values restructured under one `gisdata:` key, and fixes the deploy attempts surfaced (`--wait` covers the database and GeoWebCache; ConfigMap symlinked folders; single-file downloads keep their name). Its last run had the apply Job, plan, WMS and sync all passing in kind, with one remaining failure in the old round-trip test, fixed in the latest push (`c35ce3d`). Check CI is green, then merge it.

## Next

1. **Merge PR #14** once green.
2. **Real deploy of `examples/uk-basemap` on the dev VM** (Proxmox), never the owner's laptop (a ThinkPad, 7.6 GB, which ran out of memory). Check where you are first: `hostname`, `systemd-detect-virt`.
   - Memory: 8 GB free for the cluster runs a reduced set: `--set geoserver.services.webui.enabled=false --set geoserver.services.wfs.enabled=false --set geoserver.services.wcs.enabled=false` (gateway, REST, WMS and the tile cache remain, with Postgres, RabbitMQ and gisdata). All seven GeoServer services need about 12 GB (768 Mi limit each, Postgres 2 Gi, gisdata up to 2 Gi).
   - Steps: install k3d (`curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | USE_SUDO=false K3D_INSTALL_DIR=$HOME/.local/bin bash`); `k3d cluster create gscloud --agents 0 --k3s-arg "--disable=traefik@server:0"`; check the node can pull (`docker exec k3d-gscloud-server-0 crictl pull rancher/mirrored-pause:3.6`); `docker build -t gisdata:local . && k3d image import gisdata:local -c gscloud`; create the secrets from `geoserver-secrets.example.yaml` with real passwords (`geoserver-admin` must hold GeoServer's admin login, `admin`/`geoserver` on a fresh install); then
     `helm upgrade --install geoserver . -n gscloud -f values.yaml -f tests/kind/values-kind.yaml --set gisdata.image.repository=gisdata --set gisdata.image.tag=local --set gisdata.image.pullPolicy=Never` (plus the service switches above). Skip Helm's `--wait` and watch pods yourself; a lost connection left the release stuck in pending-install once.
   - Follow `kubectl -n gscloud logs -f job/gisdata-apply-1`: it loads OS Open Roads (~1 GB), Boundary-Line (~800 MB), the EA flood zones (415k features over OGC API) and Surrey conservation areas, then publishes them. Then check layers and the `uk-basemap` group draw through WMS, run `gisdata status`, and run a sync Job from the CronJob.
   - If the VM runs NordVPN (or anything rewriting DNS) and the node can't resolve names: k3d's DNS fix and k3s's firewall rules can both break DNS inside the node. Recreate with `K3D_FIX_DNS=0` and, if needed, write a reachable DNS server into the node's `/etc/resolv.conf` and restart CoreDNS.
3. **Retire the old catalog scripts** (`export-/import-geoserver-config.sh`, `geoserver-config/`, `tests/test_config_scripts.py`, `tests/test_e2e_rest.py`), after `gisdata import test` carries over the `test` workspace.
4. Optional: a scheduled CI job running `gisdata plan` against the cluster, failing on exit code 2 (drift).

Backlog, only when needed: API keys for `wfs`/`ogc-api-features` (unlocks the OS NGD API), private CKAN files, default attribution for sources other than OS, servers that can't page WFS (BGS), naming the project (`gisdata` and the `apiVersion` group are working names).

## Environment

- The owner's laptop kubeconfig has EKS contexts (`my-eks-cluster-2` etc.) that aren't reachable without AWS credentials. Never deploy to them.
- Secrets for local runs: `GISDATA_<SECRET>_<KEY>` environment variables (e.g. `GISDATA_GEOSERVER_PGCONFIG_SECRET_HOST`), a git-ignored `secrets.yaml` next to the project, or files under `$GISDATA_SECRETS_DIR/<secret>/<key>` (how the chart mounts them).
