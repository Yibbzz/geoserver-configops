"""Secret values, looked up by name (spec: "Secrets").

On a laptop or in CI: environment variable CONFIGOPS_<SECRET>_<KEY> (upper
case, anything else as `_`), else a git-ignored secrets.yaml next to the
project:

    os-api:
      apiKey: ...

In Kubernetes, each Secret is mounted as a folder of files
(docs/kubernetes.md): $CONFIGOPS_SECRETS_DIR/<secret>/<key>. The environment still wins,
so a value can be overridden.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

from .loader import SECRET_FILES


def env_name(secret: str, key: str) -> str:
    return "CONFIGOPS_" + re.sub(r"[^A-Z0-9]+", "_", f"{secret}_{key}".upper()).strip("_")


SECRETS_DIR = "CONFIGOPS_SECRETS_DIR"
_warned: set[Path] = set()


def lookup(secret: str, key: str, root: Path | None = None) -> str | None:
    value = os.environ.get(env_name(secret, key))
    if value:
        return value
    mounted = os.environ.get(SECRETS_DIR)
    if mounted:
        path = Path(mounted) / secret / key
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    if root is None:
        return None
    for name in sorted(SECRET_FILES):
        path = Path(root) / name
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            if path not in _warned:  # said once: otherwise every secret in it just reads as missing
                _warned.add(path)
                print(f"configops: warning: {path} can't be read as YAML, so no secret is taken from it: {str(e).splitlines()[0]}",
                      file=sys.stderr)
            continue
        entry = data.get(secret) if isinstance(data, dict) else None
        if isinstance(entry, dict) and entry.get(key) is not None:
            return str(entry[key])
    return None
