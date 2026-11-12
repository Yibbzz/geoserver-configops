#!/usr/bin/env bash
# Start the real GeoServer Cloud REST service for the e2e tests (pytest -m e2e).
# Image tags and jndi.yml come from the Helm chart, so this tests what the chart deploys.
#   tests/e2e/up.sh            start and wait until healthy
#   tests/e2e/up.sh standard   the same, with standard GeoServer (the version GeoServer Cloud is built on) instead
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
export STANDARD_GEOSERVER_VERSION=3.0.2  # what GeoServer Cloud ${GEOSERVER_TAG} is built on (/rest/about/version)
if [ "${1:-}" = "down" ]; then
  docker compose --profile cloud --profile standard down -v
  exit 0
fi
PROFILE=cloud SERVICE=rest
[ "${1:-}" = "standard" ] && PROFILE=standard SERVICE=geoserver
echo "$PROFILE: GeoServer Cloud $GEOSERVER_TAG / GeoServer $STANDARD_GEOSERVER_VERSION, $POSTGIS_IMAGE"
mkdir -p data standard-libs  # made by Docker they would be root's, and the tests couldn't write to them
mkdir -p gcs/e2e-tiles  # the fake Google Cloud Storage starts with this bucket
docker compose --profile "$PROFILE" up -d --wait --wait-timeout 900 || { docker compose --profile "$PROFILE" logs "$SERVICE" | tail -50; exit 1; }
docker compose exec -T ldap ldapadd -x -c -H ldap://localhost -D cn=admin,dc=example,dc=org -w admin -f /e2e/users.ldif >/dev/null || true  # -c: already there is fine
if [ "$PROFILE" = standard ]; then
  echo "REST API ready at http://localhost:${E2E_STANDARD_PORT:-59091}/geoserver/rest"
else
  echo "REST API ready at http://localhost:${E2E_REST_PORT:-59090}/rest"
fi
