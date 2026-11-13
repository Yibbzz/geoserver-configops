#!/usr/bin/env bash
# Start the real GeoServer Cloud for the e2e tests (pytest -m e2e): its gateway, REST, WMS, WFS, WCS
# and tile cache services (about 3 GB of memory).
#   tests/e2e/up.sh            start and wait until healthy
#   tests/e2e/up.sh standard   the same, with standard GeoServer (the version GeoServer Cloud is built on) instead
#   tests/e2e/up.sh down       stop and remove everything
set -euo pipefail
cd "$(dirname "$0")"
export GEOSERVER_TAG=3.0.2.1  # GeoServer Cloud
export POSTGIS_IMAGE=postgis/postgis:18-3.7
export RABBITMQ_IMAGE=rabbitmq:4.4-alpine
export STANDARD_GEOSERVER_VERSION=3.0.2  # the newest standard GeoServer; tests/versions.py keeps both up to date
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
  echo "GeoServer Cloud ready at http://localhost:${E2E_GATEWAY_PORT:-59090}/geoserver-cloud"
fi
