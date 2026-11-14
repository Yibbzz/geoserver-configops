# Development

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

CI runs on every pull request and push to `main`: shellcheck and a schema check of the Kubernetes example (lint), the unit tests (test), the image build (configops-image), and the e2e tests against GeoServer Cloud in Docker, with its gateway, REST, WMS, WFS, WCS and tile cache services (e2e-cloud), against standard GeoServer with the same extensions (e2e-standard), and against GeoServer 2.28, the older series still maintained (e2e-standard-2).

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
tests/e2e/up.sh down                  # tests/e2e/up.sh standard-2: GeoServer 2.28 instead, with the same env
```

## New GeoServer versions

`.github/workflows/versions.yaml` runs daily: when GeoServer Cloud or GeoServer (the newest, or the newest 2.28) has a new release (`python tests/versions.py` shows what's pinned and what's newest), it pins it (`tests/e2e/up.sh`, `examples/compose.yaml`, the README's "Tested on" line, and `docs/geoserver-fields.yaml`, read again from GeoServer's source when GeoServer Cloud moves to a new GeoServer), opens a pull request and runs CI on it. A green pull request can be merged; a red one says what changed, such as settings the new GeoServer added that `docs/geoserver-fields.yaml` hasn't sorted yet.

## Releasing

Set the version in `pyproject.toml` and `configops/__init__.py`, and `CONFIGOPS_VERSION` in `docs/kubernetes/Dockerfile`, merge, then push the tag `v<version>`: the Release workflow publishes the package to PyPI (`geoserver-configops`), the image to `ghcr.io/yibbzz/geoserver-configops`, and a GitHub release.
