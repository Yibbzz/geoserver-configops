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
# cog-google and the GCS blob store bring Google's client libraries in different versions, and which one Tomcat
# loads depends on the folder's order: keep only the newest of each of those. (Not of everything: Jackson 2 and 3
# share names.)
for jar in google-*.jar gax-*.jar grpc-*.jar proto-google-*.jar gapic-*.jar; do
  [ -e "$jar" ] || continue
  base=$(echo "$jar" | sed -E 's/-[0-9][0-9A-Za-z.]*\.jar$//')
  [ "$base" = "${jar%.jar}" ] && continue  # no version in the name
  newest=$(printf '%s\n' "$base"-[0-9]*.jar | sort -V | tail -n 1)
  [ "$jar" != "$newest" ] && [ -e "$newest" ] && rm -f "$jar"
done
# startup.sh runs the installer again: with no extensions named it installs nothing
STABLE_EXTENSIONS="" COMMUNITY_EXTENSIONS="" exec bash /opt/startup.sh
