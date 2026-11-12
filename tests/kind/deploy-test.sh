#!/usr/bin/env bash
# Deploy the whole chart into the current kind cluster and check it works end to end:
# every pod becomes ready, OGC services answer through the gateway, the e2e tests
# (pytest -m e2e) pass against the real deployment, and the configops apply Job deploys a project (examples/smoke) that then draws through WMS.
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

# configops logs in to GeoServer with its default admin login, which this test never changes
kubectl -n "$NS" create secret generic geoserver-admin --from-literal=username=admin --from-literal=password=geoserver \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Building the configops image and loading it into kind..."
docker build -q -t configops:kind .
kind load docker-image configops:kind --name "$CLUSTER"
kubectl -n "$NS" apply -f tests/kind/smoke-files.yaml   # the smoke project's data file, served in the cluster

echo "Installing the chart (pulls about 7 GB of images on first run)..."
helm upgrade --install "$RELEASE" . -n "$NS" -f values.yaml -f tests/kind/values-kind.yaml \
  -f values-data-volume.yaml -f values-app-schema.yaml --set dataVolume.size=2Gi \
  --set configops.project=examples/smoke \
  --set configops.image.repository=configops --set configops.image.tag=kind --set configops.image.pullPolicy=Never \
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

echo "== configops apply Job: deploys examples/smoke"
if ! kubectl -n "$NS" wait --for=condition=complete job/configops-apply-1 --timeout=15m; then
  kubectl -n "$NS" logs job/configops-apply-1 --tail=100 || true
  echo "FAIL the configops apply Job didn't complete"; exit 1
fi
kubectl -n "$NS" logs job/configops-apply-1
echo "== configops plan from outside: nothing left to do"
CONFIGOPS_GEOSERVER_ADMIN_USERNAME=admin CONFIGOPS_GEOSERVER_ADMIN_PASSWORD=geoserver \
CONFIGOPS_GEOSERVER_PGCONFIG_SECRET_HOST=localhost CONFIGOPS_GEOSERVER_PGCONFIG_SECRET_PORT=15432 \
CONFIGOPS_GEOSERVER_PGCONFIG_SECRET_DATABASE=postgres CONFIGOPS_GEOSERVER_PGCONFIG_SECRET_USERNAME=postgres \
CONFIGOPS_GEOSERVER_PGCONFIG_SECRET_PASSWORD="$PG_PASS" \
  configops plan -C examples/smoke --geoserver "$BASE"
echo "== The deployed layer draws through WMS"
type=$(curl -s -o /tmp/gscloud-smoke -w '%{content_type}' \
  "$BASE/wms?service=WMS&version=1.1.1&request=GetMap&layers=smoke:squares&styles=&srs=EPSG:27700&bbox=0,0,700000,1300000&width=256&height=256&format=image/png")
[ "$type" = "image/png" ] || { echo "FAIL GetMap smoke:squares returned $type"; head -c 400 /tmp/gscloud-smoke; exit 1; }
echo "ok   GetMap smoke:squares -> image/png"
echo "== configops sync, run from its CronJob"
kubectl -n "$NS" create job sync-now --from=cronjob/configops-sync
if ! kubectl -n "$NS" wait --for=condition=complete job/sync-now --timeout=10m; then
  kubectl -n "$NS" logs job/sync-now --tail=100 || true
  echo "FAIL the configops sync Job didn't complete"; exit 1
fi
kubectl -n "$NS" logs job/sync-now

echo "== Drift check, run from its CronJob: GeoServer matches the files"
kubectl -n "$NS" create job drift-now --from=cronjob/configops-drift
if ! kubectl -n "$NS" wait --for=condition=complete job/drift-now --timeout=5m; then
  kubectl -n "$NS" logs job/drift-now --tail=100 || true
  echo "FAIL the drift check found drift, or couldn't run"; exit 1
fi
kubectl -n "$NS" logs job/drift-now

echo "== TEMPORARY master password diagnostic"
python3 tests/kind/masterpw-diagnose.py "$BASE" "$NS" || true
echo "Diagnostic run only: skipping the e2e tests"; exit 0

echo "== e2e tests (pytest -m e2e) against the deployment"
# The whole chart is here: the tests also check WMS, WFS, WCS and the tile cache (E2E_SERVICES_URL), and put
# their files on the data volume through the REST service's pod (E2E_DATA_POD)
REST_POD=$(kubectl -n "$NS" get pod -l app.kubernetes.io/component=rest -o jsonpath='{.items[0].metadata.name}')
E2E_GEOSERVER_URL=$BASE/rest E2E_SERVICES_URL=$BASE E2E_DATA_POD="$NS/$REST_POD" \
E2E_DB_HOST=localhost E2E_DB_PORT=15432 E2E_DB_PASSWORD="$PG_PASS" E2E_DB_HOST_IN_GEOSERVER="$RELEASE-postgresql" \
  python3 -m pytest -m e2e -v -p no:cacheprovider

echo "All deployment checks passed."
