#!/usr/bin/env bash
# Export the GeoServer catalog (workspaces, datastores, feature types, styles)
# to JSON/SLD files so it can be version controlled.
#
# Needs the REST service reachable, e.g.:
#   kubectl port-forward deployment/geoserver-gsc-rest 9090:8080 &
#
# Settings (environment variables):
#   GEOSERVER_URL       default http://localhost:9090/rest
#   GEOSERVER_USER      default admin
#   GEOSERVER_PASSWORD  required
#   CONFIG_DIR          default ./geoserver-config
set -euo pipefail

BASE_URL="${GEOSERVER_URL:-http://localhost:9090/rest}"
AUTH="${GEOSERVER_USER:-admin}:${GEOSERVER_PASSWORD:?Set GEOSERVER_PASSWORD}"
OUT_DIR="${CONFIG_DIR:-./geoserver-config}"

get() { curl -sf -u "$AUTH" -H "Accept: ${2:-application/json}" "$BASE_URL/$1"; }

mkdir -p "$OUT_DIR"

# Workspaces
WORKSPACES=$(get workspaces.json)
echo "$WORKSPACES" | jq '.' > "$OUT_DIR/workspaces.json"

for ws in $(echo "$WORKSPACES" | jq -r '.workspaces.workspace[]?.name'); do
  echo "Exporting workspace: $ws"
  mkdir -p "$OUT_DIR/workspaces/$ws"
  get "workspaces/$ws.json" | jq '.' > "$OUT_DIR/workspaces/$ws/workspace.json"

  # Datastores
  DATASTORES=$(get "workspaces/$ws/datastores.json")
  for ds in $(echo "$DATASTORES" | jq -r '.dataStores.dataStore[]?.name // empty'); do
    echo "  Exporting datastore: $ds"
    mkdir -p "$OUT_DIR/workspaces/$ws/datastores/$ds/featuretypes"
    get "workspaces/$ws/datastores/$ds.json" | jq '.' > "$OUT_DIR/workspaces/$ws/datastores/$ds/datastore.json"

    # Feature types
    FTS=$(get "workspaces/$ws/datastores/$ds/featuretypes.json")
    for ft in $(echo "$FTS" | jq -r '.featureTypes.featureType[]?.name // empty'); do
      echo "    Exporting featuretype: $ft"
      mkdir -p "$OUT_DIR/workspaces/$ws/datastores/$ds/featuretypes/$ft"
      get "workspaces/$ws/datastores/$ds/featuretypes/$ft.json" | jq '.' \
        > "$OUT_DIR/workspaces/$ws/datastores/$ds/featuretypes/$ft/featuretype.json"
    done
  done
done

# Styles
mkdir -p "$OUT_DIR/styles"
STYLES=$(get styles.json)
for style in $(echo "$STYLES" | jq -r '.styles.style[]?.name // empty'); do
  echo "Exporting style: $style"
  get "styles/$style.json" | jq '.' > "$OUT_DIR/styles/$style.json"
  get "styles/$style.sld" "application/vnd.ogc.sld+xml" > "$OUT_DIR/styles/$style.sld"
done

echo "Done - config exported to $OUT_DIR"
