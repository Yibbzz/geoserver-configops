#!/bin/bash
# Entrypoint for the standard GeoServer in compose.yaml: install the extensions GeoServer Cloud ships, then drop
# the snapshot copies of core libraries that community extensions carry (gt-main-35-SNAPSHOT beside gt-main-35.2),
# so each library is on the classpath once, and start GeoServer as the image does.
set -euo pipefail
bash /opt/install-extensions.sh
cd "$GEOSERVER_LIB_DIR"
for jar in *-SNAPSHOT.jar; do
  base=$(echo "$jar" | sed -E 's/-[0-9][0-9.]*(-SNAPSHOT)?\.jar$//')
  for release in "$base"-[0-9]*.jar; do
    if [ -e "$release" ] && [[ "$release" != *SNAPSHOT* ]]; then
      rm -f "$jar"
      break
    fi
  done
done
# startup.sh runs the installer again: with no extensions named it installs nothing
STABLE_EXTENSIONS="" COMMUNITY_EXTENSIONS="" exec bash /opt/startup.sh
