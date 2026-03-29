# Dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn python-jose[cryptography] httpx pyyaml
COPY auth_sidecar.py .
CMD ["uvicorn", "auth_sidecar:app", "--host", "0.0.0.0", "--port", "8081"]