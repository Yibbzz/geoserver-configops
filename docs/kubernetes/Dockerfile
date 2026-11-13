# configops with your project folder in it, for docs/kubernetes/configops.yaml:
#   docker build -f docs/kubernetes/Dockerfile --build-arg PROJECT=my-project -t registry.example.org/my-project-configops:1 .
FROM ghcr.io/yibbzz/geoserver-configops:latest
ARG PROJECT
COPY --chown=65534 ${PROJECT}/ /project/
