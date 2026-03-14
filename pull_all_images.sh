REGISTRY="k3d-k3d-registry.localhost:5000"
TAG="2.28.2.3"
ACL_TAG="2.4.0"

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
  if [ "$img" = "geoserver-acl" ]; then
    echo "Processing ACL image with tag ${ACL_TAG}"
    docker pull geoservercloud/${img}:${ACL_TAG}
    docker tag  geoservercloud/${img}:${ACL_TAG} ${REGISTRY}/geoservercloud/${img}:${ACL_TAG}
    docker push ${REGISTRY}/geoservercloud/${img}:${ACL_TAG}
  else
    echo "Processing ${img} with tag ${TAG}"
    docker pull geoservercloud/${img}:${TAG}
    docker tag  geoservercloud/${img}:${TAG} ${REGISTRY}/geoservercloud/${img}:${TAG}
    docker push ${REGISTRY}/geoservercloud/${img}:${TAG}
  fi
done
