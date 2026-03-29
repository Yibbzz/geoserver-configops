# app.py
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response
import httpx, time, yaml, logging
from jose import jwt, JWTError
from urllib.parse import parse_qs, urlparse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI()

KEYCLOAK_JWKS_URL = (
    "http://keycloak.default.svc.cluster.local:80"
    "/realms/geoserver-poc/protocol/openid-connect/certs"
)
PASSTHROUGH_PATHS = [
    "/geoserver-cloud/web",
    "/geoserver-cloud/ows",  # optional, useful later
    "/geoserver-cloud/wms",  # optional, useful later
    "/geoserver-cloud/rest",
    "/geoserver-cloud/j_spring_security_check",
]

_jwks = {"data": None, "fetched_at": 0}
_policy = {"data": None, "loaded_at": 0}

def get_jwks():
    now = time.time()
    if _jwks["data"] is None or now - _jwks["fetched_at"] > 3600:
        log.info("Fetching JWKS from Keycloak")
        r = httpx.get(KEYCLOAK_JWKS_URL, timeout=10)
        r.raise_for_status()
        _jwks["data"] = r.json()
        _jwks["fetched_at"] = now
    return _jwks["data"]

def get_policy():
    now = time.time()
    if _policy["data"] is None or now - _policy["loaded_at"] > 60:
        with open("/etc/policy/policy.yaml") as f:
            _policy["data"] = yaml.safe_load(f)
        _policy["loaded_at"] = now
    return _policy["data"]

def get_layers(uri: str) -> list[str]:
    params = parse_qs(urlparse(uri).query, keep_blank_values=True)
    # WMS uses LAYERS, WMTS uses layer — handle both
    raw = params.get("LAYERS", params.get("layers",
          params.get("layer", params.get("LAYER", [""]))))[0]
    return [l.strip() for l in raw.split(",") if l.strip()]

def is_allowed(groups: list[str], layers: list[str], policy: dict) -> bool:
    # No LAYERS param = GetCapabilities or non-layer request
    # Allow authenticated users through — GeoServer will advertise
    # only what exists, and layer requests will still be checked
    if not layers:
        return True
    allowed = set()
    for g in groups:
        perms = policy.get("groups", {}).get(g, {}).get("layers", [])
        if "*" in perms:
            return True
        allowed.update(perms)
    denied = [l for l in layers if l not in allowed]
    if denied:
        log.warning(f"Groups {groups} denied access to layers {denied}")
    return len(denied) == 0

@app.get("/check")
async def check(request: Request):
    uri = request.headers.get("X-Forwarded-Uri", "")
    auth = request.headers.get("Authorization", "NONE")
    log.info(f">>> AUTH HEADER: '{auth}' | URI: {uri[:60]}")  # add this line
        
    # Allow certain paths through without auth
    for path in PASSTHROUGH_PATHS:
        if uri.startswith(path):
            log.info(f"Passthrough path, skipping auth: {uri}")
            return Response(status_code=200)
    
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        log.info("Request rejected — no Bearer token")
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth.removeprefix("Bearer ")

    try:
        jwks = get_jwks()
        claims = jwt.decode(
            token, jwks,
            algorithms=["RS256"],
            options={"verify_aud": False}  # tighten this in prod with your audience
        )
    except JWTError as e:
        log.warning(f"JWT validation failed: {e}")
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    groups = claims.get("groups", [])
    uri = request.headers.get("X-Forwarded-Uri", "")
    layers = get_layers(uri)
    policy = get_policy()

    log.info(f"User groups={groups} requesting layers={layers} uri={uri}")

    if not is_allowed(groups, layers, policy):
        raise HTTPException(status_code=403, detail=f"Groups {groups} not permitted for layers {layers}")

    return Response(status_code=200)

@app.get("/healthz")
async def health():
    return {"status": "ok"}