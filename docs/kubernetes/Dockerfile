# configops with your project folder in it, for docs/kubernetes/configops.yaml:
#   docker build -f docs/kubernetes/Dockerfile --build-arg PROJECT=my-project -t registry.example.org/my-project-configops:1 .
#
# The project is copied through a stage of its own that drops the files holding secret values (secrets.yaml,
# .env): on a laptop they sit in the project folder, and they must not reach the registry in a layer. The
# cluster gets them as Kubernetes Secrets (docs/kubernetes.md).
# A version of configops, not `latest`: a new release reaches the cluster when this line (or --build-arg) says so
ARG CONFIGOPS_VERSION=0.1.0

FROM busybox:1.37 AS project
ARG PROJECT
COPY ${PROJECT}/ /project/
RUN find /project \( -name 'secrets.yaml' -o -name 'secrets.yml' -o -name '.env' -o -name '*.env' \) -type f -delete

FROM ghcr.io/yibbzz/geoserver-configops:${CONFIGOPS_VERSION}
COPY --from=project --chown=65534 /project/ /project/
