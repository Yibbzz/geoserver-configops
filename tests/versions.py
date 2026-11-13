"""The GeoServer Cloud and standard GeoServer versions the e2e tests run, and the newest released.

    python tests/versions.py           what the repo pins, and the newest releases with images
    python tests/versions.py update    pin the newest: tests/e2e/up.sh, examples/compose.yaml, the README's
                                       "Tested on" line, and docs/geoserver-fields.yaml (read again from
                                       GeoServer's source when GeoServer Cloud moves to a new GeoServer)

`update` changes nothing when the repo is up to date. In GitHub Actions it writes `changed`, `branch`
and `title` to $GITHUB_OUTPUT for .github/workflows/versions.yaml, which opens a pull request and runs
CI on it: the pull request merges only when every test passes on the new versions.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("VERSIONS_ROOT") or Path(__file__).resolve().parent.parent)
UP = "tests/e2e/up.sh"
COMPOSE = "examples/compose.yaml"
README = "README.md"
FIELDS = "docs/geoserver-fields.yaml"
TESTED = re.compile(r"<!-- tested-on -->.*?<!-- /tested-on -->", re.S)


def get(url: str, accept: str = "application/json", method: str = "GET") -> bytes | None:
    headers = {"User-Agent": "configops-versions", "Accept": accept}
    if "api.github.com" in url and os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers, method=method), timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version))


def releases(repo: str) -> list[str]:
    """Released versions (no drafts or pre-releases), newest first, without a leading v."""
    data = json.loads(get(f"https://api.github.com/repos/{repo}/releases?per_page=50"))
    found = [r["tag_name"].removeprefix("v") for r in data if not r["draft"] and not r["prerelease"]]
    return sorted({v for v in found if re.fullmatch(r"\d+(\.\d+)+", v)}, key=key, reverse=True)


def pom_value(repo: str, tag: str, path: str, name: str) -> str:
    text = get(f"https://raw.githubusercontent.com/{repo}/{tag}/{path}", accept="*/*").decode()
    return re.search(rf"<{name}>([^<]+)</{name}>", text).group(1)


def cloud_image(version: str) -> bool:
    return get(f"https://hub.docker.com/v2/repositories/geoservercloud/geoserver-cloud-rest/tags/{version}") is not None


def standard_image(version: str) -> bool:
    return get(f"https://docker.osgeo.org/v2/geoserver/manifests/{version}",
               accept="application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json",
               method="HEAD") is not None


def newest() -> dict[str, str]:
    """The newest GeoServer Cloud and standard GeoServer with images, and what GeoServer Cloud's is built on."""
    cloud = next(v for v in releases("geoserver/geoserver-cloud") if cloud_image(v))
    base = pom_value("geoserver/geoserver-cloud", f"v{cloud}", "src/pom.xml", "gs.version").removesuffix("-CLOUD")
    gwc = pom_value("geoserver/geoserver", base, "src/pom.xml", "gwc.version")
    standard = next(v for v in releases("geoserver/geoserver") if standard_image(v))
    return {"cloud": cloud, "standard": standard, "geoserver": base, "geowebcache": gwc}


def pinned() -> dict[str, str]:
    up = (ROOT / UP).read_text()
    fields = (ROOT / FIELDS).read_text()
    return {"cloud": re.search(r"^export GEOSERVER_TAG=(\S+)", up, re.M).group(1),
            "standard": re.search(r"^export STANDARD_GEOSERVER_VERSION=(\S+)", up, re.M).group(1),
            "geoserver": re.search(r"^  geoserver: (\S+)", fields, re.M).group(1),
            "geowebcache": re.search(r"^  geowebcache: (\S+)", fields, re.M).group(1)}


def tested_line(v: dict[str, str]) -> str:
    return (f"<!-- tested-on -->Tested on **GeoServer Cloud {v['cloud']}** and **GeoServer {v['standard']}**: every pull "
            f"request runs the e2e tests on both, and a daily job (`.github/workflows/versions.yaml`) tries each new "
            f"release.<!-- /tested-on -->")


def sub(path: str, pattern: str, new: str) -> None:
    file = ROOT / path
    text, count = re.subn(pattern, new, file.read_text(), count=1, flags=re.M | re.S)
    if count != 1:
        raise SystemExit(f"{path}: {pattern!r} not found")
    file.write_text(text)


def write(v: dict[str, str], old: dict[str, str]) -> None:
    sub(UP, r"^export GEOSERVER_TAG=\S+", f"export GEOSERVER_TAG={v['cloud']}")
    sub(UP, r"^export STANDARD_GEOSERVER_VERSION=\S+", f"export STANDARD_GEOSERVER_VERSION={v['standard']}")
    sub(COMPOSE, r"image: docker\.osgeo\.org/geoserver:\S+", f"image: docker.osgeo.org/geoserver:{v['standard']}")
    sub(README, TESTED.pattern, tested_line(v))
    if (v["geoserver"], v["geowebcache"]) != (old["geoserver"], old["geowebcache"]):
        sub(FIELDS, r"^  geoserver: \S+", f"  geoserver: {v['geoserver']}")
        sub(FIELDS, r"^  geowebcache: \S+", f"  geowebcache: {v['geowebcache']}")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import geoserver_coverage as coverage

        coverage.MANIFEST = ROOT / FIELDS
        coverage.refresh()  # fields GeoServer added come in as `unsorted`, and the tests say so until they're sorted


def main(argv: list[str]) -> int:
    old, new = pinned(), newest()
    for name in ("cloud", "standard", "geoserver", "geowebcache"):
        mark = "" if old[name] == new[name] else "   <- newer"
        print(f"{name:12} pinned {old[name]:10} newest {new[name]}{mark}")
    changed = (old["cloud"], old["standard"]) != (new["cloud"], new["standard"]) and \
        key(new["cloud"]) >= key(old["cloud"]) and key(new["standard"]) >= key(old["standard"])
    if argv[:1] == ["update"]:
        if changed:
            write({**old, **new}, old)
            print("updated")
        else:
            print("up to date")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as out:
                out.write(f"changed={'true' if changed else 'false'}\n")
                out.write(f"branch=versions/cloud-{new['cloud']}-geoserver-{new['standard']}\n")
                out.write(f"title=GeoServer Cloud {new['cloud']} and GeoServer {new['standard']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
