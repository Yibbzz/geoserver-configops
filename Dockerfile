# configops: ghcr.io/yibbzz/geoserver-configops. docs/kubernetes.md runs it as a Job and a CronJob.
FROM python:3.14-slim

# What configops needs comes from requirements.lock: exact versions, each checked against its hash, so the image
# built today and the one built next month hold the same code. After changing pyproject.toml's dependencies:
#   uv pip compile pyproject.toml --generate-hashes --python-version 3.14 --python-platform linux -o requirements.lock
WORKDIR /app
COPY requirements.lock pyproject.toml README.md LICENSE ./
COPY configops/ configops/
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && pip install --no-cache-dir --no-deps . && rm -rf /app

# The project files go here (docs/kubernetes/Dockerfile copies them in), the Secrets under /etc/configops/secrets
WORKDIR /project
ENV CONFIGOPS_SECRETS_DIR=/etc/configops/secrets
USER 65534
ENTRYPOINT ["configops"]
CMD ["plan", "-C", "/project"]
