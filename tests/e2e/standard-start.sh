#!/bin/bash
# Entrypoint for the standard GeoServer in compose.yaml: install the extensions GeoServer Cloud ships (and Oracle's), then drop
# the snapshot copies of core libraries that community extensions carry (gt-main-35-SNAPSHOT beside gt-main-35.2),
# so each library is on the classpath once, and start GeoServer as the image does.
set -euo pipefail
LIBS=${ADDITIONAL_LIBS_DIR%/}
FALLBACK=/opt/fallback-libs  # tests/e2e/standard-libs-fallback/<version>: the last good run's copies (CI keeps them in its cache)
EXTENSIONS=$(echo "$STABLE_EXTENSIONS,$COMMUNITY_EXTENSIONS" | tr ',' ' ')

# A zip cut short (a dropped download, or wget -c resuming onto the previous night's build of the same name) installs
# broken jars: removed, so the image's installer fetches it whole
drop_broken() {
  for zip in "$LIBS"/*.zip; do
    [ -e "$zip" ] || continue
    unzip -tq "$zip" >/dev/null 2>&1 || { echo "broken, removed: $(basename "$zip")"; rm -f "$zip"; }
  done
}
drop_broken
bash /opt/install-extensions.sh
drop_broken
# What the download servers couldn't give (build.geoserver.org, which serves the community extensions, was down on
# 18 Nov 2026), from the fallback copies
for ext in $EXTENSIONS; do
  compgen -G "$LIBS/geoserver-*-$ext-plugin.zip" >/dev/null && continue
  copies=("$FALLBACK"/geoserver-*-"$ext"-plugin.zip)
  copy=${copies[0]}
  if [ -e "$copy" ]; then
    cp "$copy" "$LIBS/"
    unzip -q -o -d "$GEOSERVER_LIB_DIR" "$copy" "*.jar"
    echo "$ext: not downloaded; installed the fallback copy $(basename "$copy")"
  else
    echo "WARNING: $ext could not be downloaded, and there is no fallback copy"
  fi
done

# A jar from a Maven repository, kept in the libraries folder (and from the fallback copies when the repository is down)
fetch_jar() {
  local url=$1 name=$2
  mkdir -p "$LIBS/jars"
  if [ ! -s "$LIBS/jars/$name" ] && ! curl -fsSL -o "$LIBS/jars/$name.part" "$url"; then
    rm -f "$LIBS/jars/$name.part"
    if [ -s "$FALLBACK/jars/$name" ]; then
      echo "$name: not downloaded; the fallback copy"
      cp "$FALLBACK/jars/$name" "$LIBS/jars/$name"
    else
      echo "WARNING: $name could not be downloaded, and there is no fallback copy"
      return 0
    fi
  fi
  [ -e "$LIBS/jars/$name.part" ] && mv "$LIBS/jars/$name.part" "$LIBS/jars/$name"
  cp "$LIBS/jars/$name" "$GEOSERVER_LIB_DIR/$name"
}
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
# GeoServer Cloud ships GeoTools' CSV store, which GeoServer has as no extension: add the plugin, at the GeoTools version
# GeoServer runs
for jar in gt-main-*.jar; do gt=${jar#gt-main-}; gt=${gt%.jar}; done
[ -e "gt-csv-$gt.jar" ] || fetch_jar "https://repo.osgeo.org/repository/release/org/geotools/gt-csv/$gt/gt-csv-$gt.jar" "gt-csv-$gt.jar"
# The GeoParquet community extension needs GeoTools' DuckDB module, which GeoServer 3 has and 2.28 doesn't (nor
# does the extension's zip): add it at the same GeoTools version
if compgen -G "gt-geoparquet-*.jar" >/dev/null && ! compgen -G "gt-duckdb-*.jar" >/dev/null; then
  fetch_jar "https://repo.osgeo.org/repository/release/org/geotools/gt-duckdb/$gt/gt-duckdb-$gt.jar" "gt-duckdb-$gt.jar"
fi
[ -e opencsv-5.12.0.jar ] || fetch_jar https://repo1.maven.org/maven2/com/opencsv/opencsv/5.12.0/opencsv-5.12.0.jar opencsv-5.12.0.jar  # GeoServer Cloud's
# startup.sh runs the installer again: with no extensions named it installs nothing
STABLE_EXTENSIONS="" COMMUNITY_EXTENSIONS="" exec bash /opt/startup.sh
