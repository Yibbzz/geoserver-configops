import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def helm_template(*args: str) -> list[dict]:
    """Render the chart and return the parsed manifests."""
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    result = subprocess.run(
        ["helm", "template", "geoserver", str(ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def by_kind(manifests: list[dict], kind: str) -> dict[str, dict]:
    return {m["metadata"]["name"]: m for m in manifests if m["kind"] == kind}


def containers(manifest: dict) -> list[dict]:
    spec = manifest["spec"]
    if manifest["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["spec"]["containers"]


# --- e2e: the shared data volume (/data in GeoServer), reached as a local folder or through a pod


class DataVolume:
    """Files on GeoServer's /data: E2E_DATA_DIR, a folder mounted there (tests/e2e/up.sh), or
    E2E_DATA_POD, namespace/pod of a GeoServer service that mounts it (the kind test, the dev cluster)."""

    def __init__(self):
        import os

        self.local = os.environ.get("E2E_DATA_DIR")
        self.pod = os.environ.get("E2E_DATA_POD")

    @property
    def available(self) -> bool:
        return bool(self.local or self.pod)

    def _exec(self, script: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
        ns, pod = self.pod.split("/", 1)
        return subprocess.run(["kubectl", "-n", ns, "exec", "-i", pod, "--", "sh", "-c", script],
                              input=stdin, capture_output=True, check=True)

    def put(self, source: Path, target: str) -> None:
        """Copy a file or folder to /data/<target>, replacing what's there."""
        import io
        import tarfile

        self.remove(target)
        if self.local:
            dest = Path(self.local) / target
            dest.parent.mkdir(parents=True, exist_ok=True)
            (shutil.copytree if source.is_dir() else shutil.copy)(source, dest)
            for p in [dest, *dest.rglob("*")] if dest.is_dir() else [dest]:
                p.chmod(0o777 if p.is_dir() else 0o666)  # GeoServer writes indexes and caches beside the files
            return
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            tar.add(source, arcname=Path(target).name)
        parent = str(Path("/data") / target).rsplit("/", 1)[0]
        self._exec(f"mkdir -p {parent} && tar xf - -C {parent} && chmod -R a+rwX /data/{target}", buffer.getvalue())

    def remove(self, target: str) -> None:
        if self.local:
            path = Path(self.local) / target
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
            return
        self._exec(f"rm -rf /data/{target}")

    def exists(self, target: str) -> bool:
        if self.local:
            return (Path(self.local) / target).exists()
        try:
            self._exec(f"test -e /data/{target}")
            return True
        except subprocess.CalledProcessError:
            return False


@pytest.fixture(scope="session")
def data_volume() -> DataVolume:
    volume = DataVolume()
    if not volume.available:
        pytest.skip("E2E_DATA_DIR or E2E_DATA_POD not set")
    return volume


# --- e2e: what the tests write to GeoServer and read back, and which tests pass, for
# tests/test_e2e_coverage.py (which runs last) to work out which fields are proved.

LIVE_EVENTS: list[tuple[str, str, object]] = []  # ("write" or "read", URL path, body)
PASSED: set[str] = set()


@pytest.fixture(scope="session", autouse=True)
def record_live_requests():
    import os

    if not os.environ.get("E2E_GEOSERVER_URL"):
        yield
        return
    import re
    from urllib.parse import urlparse

    import requests

    from tests.geoserver_coverage import DOCUMENTS, rest_path

    original = requests.Session.request

    def request(self, method, url, *args, **kwargs):
        response = original(self, method, url, *args, **kwargs)
        path = urlparse(url).path
        if response.ok and any(re.search(p, rest_path(path)) for p, _, _ in DOCUMENTS):
            if method.upper() in ("PUT", "POST"):
                body = kwargs.get("json") if kwargs.get("json") is not None else kwargs.get("data")
                LIVE_EVENTS.append(("write", path, body))
            elif method.upper() == "GET":
                LIVE_EVENTS.append(("read", path, response.text))
        return response

    requests.Session.request = request
    yield
    requests.Session.request = original


def pytest_runtest_logreport(report):
    if report.when == "call" and report.passed:
        PASSED.add(report.nodeid)


def pytest_collection_modifyitems(items):
    """Tests marked `late` run after the others, and `last` after those (stable: the rest keep their order)."""
    items.sort(key=lambda item: 2 if item.get_closest_marker("last") else 1 if item.get_closest_marker("late") else 0)
