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
