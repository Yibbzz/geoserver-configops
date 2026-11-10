#!/usr/bin/env bash
# Deploy the whole chart into the current kind cluster and check it works end to end:
# every pod becomes ready, OGC services answer through the gateway, the catalog
# import/export round trip (tests/test_e2e_rest.py) passes against the real deployment,
# and the gisdata apply Job deploys a project (examples/smoke) that then draws through WMS.
#
#   kind create cluster --name gscloud
#   tests/kind/deploy-test.sh        # needs helm, kubectl, curl, jq and the Python test deps
set -euo pipefail
cd "$(dirname "$0")/../.."
NS=gscloud-test
CLUSTER=${KIND_CLUSTER:-gscloud}
RELEASE=geoserver   # the secrets and RabbitMQ host assume this release name
PIDS=()

diagnose() {
  echo "::group::Diagnostics"
  kubectl -n "$NS" get pods -o wide || true
  kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -40 || true
  for pod in $(kubectl -n "$NS" get pods -o name 2>/dev/null); do
    echo "--- $pod"; kubectl -n "$NS" logs "$pod" --all-containers --tail=60 || true
  done
  echo "::endgroup::"
}
on_exit() {
  local status=$?
  [ "$status" -ne 0 ] && diagnose
  for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done
  exit "$status"
}
trap on_exit EXIT

kubectl get nodes -o wide
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

# Secrets from the example file, with generated passwords
PG_PASS=$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')
MQ_PASS=$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')
sed -e "s/password: \"CHANGE_ME\"/password: \"$PG_PASS\"/" geoserver-secrets.example.yaml \
  | awk -v mq="$MQ_PASS" '/name: geoserver-rabbitmq-secret/{r=1} r && /password: "CHANGE_ME"/{sub(/CHANGE_ME/, mq); r=0} {print}' \
  | kubectl -n "$NS" apply -f -

# gisdata logs in to GeoServer with its default admin login, which this test never changes
kubectl -n "$NS" create secret generic geoserver-admin --from-literal=username=admin --from-literal=password=geoserver \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Building the gisdata image and loading it into kind..."
docker build -q -t gisdata:kind .
kind load docker-image gisdata:kind --name "$CLUSTER"
kubectl -n "$NS" apply -f tests/kind/smoke-files.yaml   # the smoke project's data file, served in the cluster

echo "Installing the chart (pulls about 7 GB of images on first run)..."
helm upgrade --install "$RELEASE" . -n "$NS" -f values.yaml -f tests/kind/values-kind.yaml \
  --set gisdata.project=examples/smoke \
  --set gisdata.image.repository=gisdata --set gisdata.image.tag=kind --set gisdata.image.pullPolicy=Never \
  --wait --timeout 25m
kubectl -n "$NS" get pods

kubectl -n "$NS" port-forward svc/geoserver-gsc-gateway 18080:8080 >/dev/null 2>&1 & PIDS+=($!)
kubectl -n "$NS" port-forward svc/geoserver-postgresql 15432:5432 >/dev/null 2>&1 & PIDS+=($!)
BASE=http://localhost:18080/geoserver-cloud
for _ in $(seq 1 60); do curl -fs -o /dev/null "$BASE/wms?service=WMS&request=GetCapabilities" && break; sleep 5; done

echo "== OGC services through the gateway"
for svc in "wms?service=WMS&request=GetCapabilities" "wfs?service=WFS&request=GetCapabilities" \
           "wcs?service=WCS&request=GetCapabilities" "gwc/service/wmts?service=WMTS&request=GetCapabilities"; do
  code=$(curl -s -o /tmp/gscloud-caps -w '%{http_code}' "$BASE/$svc")
  grep -q "Capabilities" /tmp/gscloud-caps || { echo "FAIL $svc -> HTTP $code"; head -c 400 /tmp/gscloud-caps; exit 1; }
  echo "ok   $svc"
done

echo "== gisdata apply Job: deploys examples/smoke"
if ! kubectl -n "$NS" wait --for=condition=complete job/gisdata-apply-1 --timeout=15m; then
  kubectl -n "$NS" logs job/gisdata-apply-1 --tail=100 || true
  echo "FAIL the gisdata apply Job didn't complete"; exit 1
fi
kubectl -n "$NS" logs job/gisdata-apply-1
echo "== gisdata plan from outside: nothing left to do"
GISDATA_GEOSERVER_ADMIN_USERNAME=admin GISDATA_GEOSERVER_ADMIN_PASSWORD=geoserver \
GISDATA_GEOSERVER_PGCONFIG_SECRET_HOST=localhost GISDATA_GEOSERVER_PGCONFIG_SECRET_PORT=15432 \
GISDATA_GEOSERVER_PGCONFIG_SECRET_DATABASE=postgres GISDATA_GEOSERVER_PGCONFIG_SECRET_USERNAME=postgres \
GISDATA_GEOSERVER_PGCONFIG_SECRET_PASSWORD="$PG_PASS" \
  gisdata plan -C examples/smoke --geoserver "$BASE"
echo "== The deployed layer draws through WMS"
type=$(curl -s -o /tmp/gscloud-smoke -w '%{content_type}' \
  "$BASE/wms?service=WMS&version=1.1.1&request=GetMap&layers=smoke:squares&styles=&srs=EPSG:27700&bbox=0,0,700000,1300000&width=256&height=256&format=image/png")
[ "$type" = "image/png" ] || { echo "FAIL GetMap smoke:squares returned $type"; head -c 400 /tmp/gscloud-smoke; exit 1; }
echo "ok   GetMap smoke:squares -> image/png"
echo "== gisdata sync, run from its CronJob"
kubectl -n "$NS" create job sync-now --from=cronjob/gisdata-sync
if ! kubectl -n "$NS" wait --for=condition=complete job/sync-now --timeout=10m; then
  kubectl -n "$NS" logs job/sync-now --tail=100 || true
  echo "FAIL the gisdata sync Job didn't complete"; exit 1
fi
kubectl -n "$NS" logs job/sync-now

echo "== Catalog round trip (tests/test_e2e_rest.py) against the deployment"
E2E_GEOSERVER_URL=$BASE/rest E2E_DB_HOST=localhost E2E_DB_PORT=15432 E2E_DB_PASSWORD="$PG_PASS" \
  python3 -m pytest -m e2e -v -p no:cacheprovider

echo "== A layer renders through WMS"
type=$(curl -s -o /tmp/gscloud-map -w '%{content_type}' \
  "$BASE/wms?service=WMS&version=1.1.1&request=GetMap&layers=test:openroads&styles=&srs=EPSG:27700&bbox=0,0,700000,1300000&width=256&height=256&format=image/png")
[ "$type" = "image/png" ] || { echo "FAIL GetMap returned $type"; head -c 400 /tmp/gscloud-map; exit 1; }
echo "ok   GetMap test:openroads -> image/png"

echo "All deployment checks passed."
