# configops: ghcr.io/yibbzz/geoserver-configops. docs/kubernetes.md runs it as a Job and a CronJob.
FROM python:3.14-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY configops/ configops/
RUN pip install --no-cache-dir . && rm -rf /app

# The project files go here (docs/kubernetes/Dockerfile copies them in), the Secrets under /etc/configops/secrets
WORKDIR /project
ENV CONFIGOPS_SECRETS_DIR=/etc/configops/secrets
USER 65534
ENTRYPOINT ["configops"]
CMD ["plan", "-C", "/project"]
