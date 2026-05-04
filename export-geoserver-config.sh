#!/bin/bash
# export-geoserver-config.sh
BASE_URL="http://localhost:9090/rest"
AUTH="admin:geoserver"
OUT_DIR="./geoserver-config"

mkdir -p $OUT_DIR

# Workspaces
WORKSPACES=$(curl -s -u $AUTH -H "Accept: application/json" $BASE_URL/workspaces.json)
echo $WORKSPACES | jq '.' > $OUT_DIR/workspaces.json

for ws in $(echo $WORKSPACES | jq -r '.workspaces.workspace[].name'); do
  echo "Exporting workspace: $ws"
  
  # Workspace detail
  mkdir -p $OUT_DIR/workspaces/$ws
  curl -s -u $AUTH -H "Accept: application/json" \
    $BASE_URL/workspaces/$ws.json \
    | jq '.' > $OUT_DIR/workspaces/$ws/workspace.json

  # Datastores
  DATASTORES=$(curl -s -u $AUTH -H "Accept: application/json" \
    $BASE_URL/workspaces/$ws/datastores.json)

  for ds in $(echo $DATASTORES | jq -r '.dataStores.dataStore[].name // empty'); do
    echo "  Exporting datastore: $ds"
    mkdir -p $OUT_DIR/workspaces/$ws/datastores/$ds/featuretypes

    curl -s -u $AUTH -H "Accept: application/json" \
      $BASE_URL/workspaces/$ws/datastores/$ds.json \
      | jq '.' > $OUT_DIR/workspaces/$ws/datastores/$ds/datastore.json

    # Feature types
    FTS=$(curl -s -u $AUTH -H "Accept: application/json" \
      $BASE_URL/workspaces/$ws/datastores/$ds/featuretypes.json)

    for ft in $(echo $FTS | jq -r '.featureTypes.featureType[].name // empty'); do
      echo "    Exporting featuretype: $ft"
      mkdir -p $OUT_DIR/workspaces/$ws/datastores/$ds/featuretypes/$ft

      curl -s -u $AUTH -H "Accept: application/json" \
        $BASE_URL/workspaces/$ws/datastores/$ds/featuretypes/$ft.json \
        | jq '.' > $OUT_DIR/workspaces/$ws/datastores/$ds/featuretypes/$ft/featuretype.json
    done
  done
done

# Styles
mkdir -p $OUT_DIR/styles
STYLES=$(curl -s -u $AUTH -H "Accept: application/json" $BASE_URL/styles.json)

for style in $(echo $STYLES | jq -r '.styles.style[].name // empty'); do
  echo "Exporting style: $style"
  
  curl -s -u $AUTH -H "Accept: application/json" \
    $BASE_URL/styles/$style.json \
    | jq '.' > $OUT_DIR/styles/$style.json

  # SLD content
  curl -s -u $AUTH -H "Accept: application/vnd.ogc.sld+xml" \
    $BASE_URL/styles/$style.sld \
    > $OUT_DIR/styles/$style.sld
done

echo "Done - config exported to $OUT_DIR"
echo ""
echo "Structure:"
find $OUT_DIR -type f | sort