#!/usr/bin/env bash
# Mirror the GeoServer Cloud images into a local registry (e.g. k3d).
# Only needed with values-local-registry.yaml; the default values pull from Docker Hub.
set -euo pipefail

REGISTRY="${REGISTRY:-k3d-k3d-registry.localhost:5000}"
TAG="${TAG:-2.28.2.3}"
ACL_TAG="${ACL_TAG:-2.4.0}"

IMAGES=(
  "geoserver-cloud-gateway"
  "geoserver-cloud-webui"
  "geoserver-cloud-rest"
  "geoserver-cloud-wfs"
  "geoserver-cloud-wms"
  "geoserver-cloud-wcs"
  "geoserver-cloud-wps"
  "geoserver-cloud-gwc"
  "geoserver-acl"
  "geoserver-cloud-config"
  "geoserver-cloud-discovery"
)

for img in "${IMAGES[@]}"; do
  tag="$TAG"
  [ "$img" = "geoserver-acl" ] && tag="$ACL_TAG"
  echo "Processing ${img}:${tag}"
  docker pull "geoservercloud/${img}:${tag}"
  docker tag "geoservercloud/${img}:${tag}" "${REGISTRY}/geoservercloud/${img}:${tag}"
  docker push "${REGISTRY}/geoservercloud/${img}:${tag}"
done
