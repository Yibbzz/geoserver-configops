#!/bin/bash
# import-geoserver-config.sh

# !!RUN THIS BEFORE RUNNING THIS SCRIPT!! - kubectl port-forward deployment/geoserver-gsc-rest 9090:8080 & sleep 2

BASE_URL="http://localhost:9090/rest"
AUTH="admin:geoserver"
CONFIG_DIR="./geoserver-config"

# 1. Workspaces
for ws_file in $CONFIG_DIR/workspaces/*/workspace.json; do
  ws=$(cat $ws_file | jq -r '.workspace.name')
  echo "Upserting workspace: $ws"
  curl -s -u $AUTH -X PUT \
    -H "Content-Type: application/json" \
    -d @$ws_file \
    $BASE_URL/workspaces/$ws
  echo ""
done

# 2. Datastores
for ds_file in $CONFIG_DIR/workspaces/*/datastores/*/datastore.json; do
  ws=$(echo $ds_file | awk -F'/' '{print $4}')
  ds=$(echo $ds_file | awk -F'/' '{print $6}')
  echo "Upserting datastore: $ws/$ds"
  curl -s -u $AUTH -X PUT \
    -H "Content-Type: application/json" \
    -d @$ds_file \
    $BASE_URL/workspaces/$ws/datastores/$ds
  echo ""
done

# 3. Feature types
for ft_file in $CONFIG_DIR/workspaces/*/datastores/*/featuretypes/*/featuretype.json; do
  ws=$(echo $ft_file | awk -F'/' '{print $4}')
  ds=$(echo $ft_file | awk -F'/' '{print $6}')
  ft=$(echo $ft_file | awk -F'/' '{print $8}')
  echo "Upserting featuretype: $ws/$ds/$ft"
  curl -s -u $AUTH -X PUT \
    -H "Content-Type: application/json" \
    -d @$ft_file \
    $BASE_URL/workspaces/$ws/datastores/$ds/featuretypes/$ft
  echo ""
done

# 4. Style metadata
for style_file in $CONFIG_DIR/styles/*.json; do
  style_name=$(cat $style_file | jq -r '.style.name')
  echo "Upserting style: $style_name"
  curl -s -u $AUTH -X PUT \
    -H "Content-Type: application/json" \
    -d @$style_file \
    $BASE_URL/styles/$style_name
  echo ""
done

# 5. SLD content
for sld_file in $CONFIG_DIR/styles/*.sld; do
  style_name=$(basename $sld_file .sld)
  echo "Uploading SLD: $style_name"
  curl -s -u $AUTH -X PUT \
    -H "Content-Type: application/vnd.ogc.sld+xml" \
    -d @$sld_file \
    $BASE_URL/styles/$style_name
  echo ""
done

echo "Done - config imported"