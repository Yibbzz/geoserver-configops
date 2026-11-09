#!/usr/bin/env bash
# Import a catalog exported by export-geoserver-config.sh back into GeoServer.
# Resources are upserted in dependency order: workspaces, datastores,
# feature types, styles.
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
shopt -s nullglob

BASE_URL="${GEOSERVER_URL:-http://localhost:9090/rest}"
AUTH="${GEOSERVER_USER:-admin}:${GEOSERVER_PASSWORD:?Set GEOSERVER_PASSWORD}"
CONFIG_DIR="${CONFIG_DIR:-./geoserver-config}"

# PUT updates an existing resource; if it doesn't exist yet, POST it to the parent collection.
upsert() {
  local path="$1" parent="$2" file="$3" type="${4:-application/json}"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -u "$AUTH" -X PUT \
    -H "Content-Type: $type" --data-binary "@$file" "$BASE_URL/$path")
  if [ "$code" = "404" ]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' -u "$AUTH" -X POST \
      -H "Content-Type: $type" --data-binary "@$file" "$BASE_URL/$parent")
  fi
  echo "  $path -> HTTP $code"
}

# 1. Workspaces
for f in "$CONFIG_DIR"/workspaces/*/workspace.json; do
  ws=$(basename "$(dirname "$f")")
  echo "Upserting workspace: $ws"
  upsert "workspaces/$ws" "workspaces" "$f"
done

# 2. Datastores
for f in "$CONFIG_DIR"/workspaces/*/datastores/*/datastore.json; do
  ds_dir=$(dirname "$f")
  ds=$(basename "$ds_dir")
  ws=$(basename "$(dirname "$(dirname "$ds_dir")")")
  echo "Upserting datastore: $ws/$ds"
  upsert "workspaces/$ws/datastores/$ds" "workspaces/$ws/datastores" "$f"
done

# 3. Feature types
for f in "$CONFIG_DIR"/workspaces/*/datastores/*/featuretypes/*/featuretype.json; do
  ft_dir=$(dirname "$f")
  ft=$(basename "$ft_dir")
  ds_dir=$(dirname "$(dirname "$ft_dir")")
  ds=$(basename "$ds_dir")
  ws=$(basename "$(dirname "$(dirname "$ds_dir")")")
  echo "Upserting featuretype: $ws/$ds/$ft"
  upsert "workspaces/$ws/datastores/$ds/featuretypes/$ft" "workspaces/$ws/datastores/$ds/featuretypes" "$f"
done

# 4. Style metadata, then 5. SLD content
for f in "$CONFIG_DIR"/styles/*.json; do
  style=$(jq -r '.style.name' "$f")
  echo "Upserting style: $style"
  upsert "styles/$style" "styles" "$f"
done
for f in "$CONFIG_DIR"/styles/*.sld; do
  style=$(basename "$f" .sld)
  echo "Uploading SLD: $style"
  upsert "styles/$style" "styles" "$f" "application/vnd.ogc.sld+xml"
done

echo "Done - config imported"
