# gisdata: the nightly `gisdata sync` job (and the CLI, for one-off runs in the cluster).
FROM python:3.14-slim

# GDAL's command-line tools (ogr2ogr, ogrinfo) do the reading and loading
RUN apt-get update \
    && apt-get install -y --no-install-recommends gdal-bin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY gisdata/ gisdata/
RUN pip install --no-cache-dir . && rm -rf /app

# The Helm chart mounts the project files here and the Secrets under /etc/gisdata/secrets
WORKDIR /project
ENV GISDATA_SECRETS_DIR=/etc/gisdata/secrets
USER 65534
ENTRYPOINT ["gisdata"]
CMD ["sync", "-C", "/project"]
