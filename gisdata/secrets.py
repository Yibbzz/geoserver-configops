"""Secret values, looked up by name (spec: "Secrets").

On a laptop or in CI: environment variable GISDATA_<SECRET>_<KEY> (upper
case, anything else as `_`), else a git-ignored secrets.yaml next to the
project:

    os-api:
      apiKey: ...

Kubernetes Secrets come later, with the in-cluster commands.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from .loader import SECRET_FILES


def env_name(secret: str, key: str) -> str:
    return "GISDATA_" + re.sub(r"[^A-Z0-9]+", "_", f"{secret}_{key}".upper()).strip("_")


def lookup(secret: str, key: str, root: Path | None = None) -> str | None:
    value = os.environ.get(env_name(secret, key))
    if value:
        return value
    if root is None:
        return None
    for name in sorted(SECRET_FILES):
        path = Path(root) / name
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, UnicodeDecodeError):
            continue
        entry = data.get(secret) if isinstance(data, dict) else None
        if isinstance(entry, dict) and entry.get(key) is not None:
            return str(entry[key])
    return None
