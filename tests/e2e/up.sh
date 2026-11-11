#!/usr/bin/env bash
# Start the real GeoServer Cloud REST service for the e2e tests (pytest -m e2e).
# Image tags and jndi.yml come from the Helm chart, so this tests what the chart deploys.
#   tests/e2e/up.sh            start and wait until healthy
#   tests/e2e/up.sh down       stop and remove everything
set -euo pipefail
cd "$(dirname "$0")"
ROOT=../..

# Read image versions from values.yaml and the jndi.yml from the rendered chart
eval "$(python3 - "$ROOT" <<'PY'
import subprocess, sys, yaml
root = sys.argv[1]
values = yaml.safe_load(open(f"{root}/values.yaml"))
pg, mq = values["postgresql"]["image"], values["rabbitmq"]["image"]
print(f'export GEOSERVER_TAG={values["common-image-stuff"]["tag"]}')
print(f'export POSTGIS_IMAGE={pg["repository"]}:{pg["tag"]}')
print(f'export RABBITMQ_IMAGE={mq["repository"]}:{mq["tag"]}')
rendered = subprocess.run(["helm", "template", "geoserver", root, "--show-only", "templates/jndi-configmap.yaml"],
                          capture_output=True, text=True, check=True).stdout
open("jndi.yml", "w").write(yaml.safe_load(rendered)["data"]["jndi.yml"])
PY
)"
if [ "${1:-}" = "down" ]; then
  docker compose down -v
  exit 0
fi
echo "GeoServer Cloud $GEOSERVER_TAG, $POSTGIS_IMAGE, $RABBITMQ_IMAGE"
docker compose up -d --wait --wait-timeout 420 || { docker compose logs rest | tail -50; exit 1; }
echo "REST API ready at http://localhost:${E2E_REST_PORT:-59090}/rest"
