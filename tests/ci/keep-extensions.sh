#!/usr/bin/env bash
# After a run whose GeoServer started and passed: its extensions (the zips and the Maven jars it downloaded) become
# the fallback copies the next runs install when a download server is down (tests/e2e/standard-start.sh). Only
# whole zips are kept. CI saves the folder in its cache.
#   tests/ci/keep-extensions.sh <GeoServer version>
set -euo pipefail
cd "$(dirname "$0")/../e2e"
from="standard-libs/$1" to="standard-libs-fallback/$1"
[ -d "$from" ] || { echo "no $from"; exit 0; }
mkdir -p "$to/jars"
kept=0
for zip in "$from"/*.zip; do
  [ -e "$zip" ] || continue
  if python3 -c 'import sys, zipfile; sys.exit(zipfile.ZipFile(sys.argv[1]).testzip() is not None)' "$zip" 2>/dev/null; then
    cp "$zip" "$to/"
    kept=$((kept + 1))
  fi
done
for jar in "$from"/jars/*.jar; do
  [ -e "$jar" ] && cp "$jar" "$to/jars/"
done
echo "kept $kept extension zips and $(find "$to/jars" -name '*.jar' | wc -l) jars in $to"
