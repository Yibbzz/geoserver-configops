"""Round-trip the catalog scripts against a fake GeoServer REST API.

import-geoserver-config.sh pushes geoserver-config/ into the fake server, then
export-geoserver-config.sh pulls it back out; the files should match.
"""

import http.server
import json
import os
import shutil
import subprocess
import threading

import pytest

from conftest import ROOT

if not (shutil.which("curl") and shutil.which("jq")):
    pytest.skip("curl and jq are needed", allow_module_level=True)

SLD = "application/vnd.ogc.sld+xml"

# Collection path -> (list key, item key, key inside a posted body)
COLLECTIONS = {
    "workspaces": ("workspaces", "workspace", "workspace"),
    "datastores": ("dataStores", "dataStore", "dataStore"),
    "featuretypes": ("featureTypes", "featureType", "featureType"),
    "styles": ("styles", "style", "style"),
}


class FakeGeoServer(http.server.BaseHTTPRequestHandler):
    """Stores resources by path; PUT only updates, POST to a collection creates."""

    store: dict  # (path, "json" | "sld") -> bytes
    log: list

    def _path(self):
        path = self.path.split("?", 1)[0].removeprefix("/rest/")
        kind = "sld" if path.endswith(".sld") or self.headers.get("Content-Type") == SLD else "json"
        return path.removesuffix(".json").removesuffix(".sld"), kind

    def _reply(self, code, body=b""):
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        return self.rfile.read(int(self.headers["Content-Length"]))

    def do_GET(self):
        path, kind = self._path()
        self.log.append(("GET", path))
        collection = path.rsplit("/", 1)[-1]
        if collection in COLLECTIONS:
            list_key, item_key, _ = COLLECTIONS[collection]
            prefix = path + "/"
            names = sorted({
                p.removeprefix(prefix) for p, k in self.store
                if p.startswith(prefix) and "/" not in p.removeprefix(prefix) and k == "json"
            })
            body = {list_key: {item_key: [{"name": n} for n in names]} if names else ""}
            return self._reply(200, json.dumps(body).encode())
        if (path, kind) in self.store:
            return self._reply(200, self.store[(path, kind)])
        self._reply(404)

    def do_PUT(self):
        path, kind = self._path()
        self.log.append(("PUT", path))
        body = self._body()
        # An SLD body updates an existing style; anything else must already exist
        if (path, "json") not in self.store:
            return self._reply(404)
        self.store[(path, kind)] = body
        self._reply(200)

    def do_POST(self):
        path, kind = self._path()
        self.log.append(("POST", path))
        body = self._body()
        _, _, body_key = COLLECTIONS[path.rsplit("/", 1)[-1]]
        name = json.loads(body)[body_key]["name"]
        self.store[(f"{path}/{name}", kind)] = body
        self._reply(201)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    handler = type("Handler", (FakeGeoServer,), {"store": {}, "log": []})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/rest", handler
    srv.shutdown()


def run(script, url, config_dir):
    env = {**os.environ, "GEOSERVER_URL": url, "GEOSERVER_PASSWORD": "x", "CONFIG_DIR": str(config_dir)}
    return subprocess.run([str(ROOT / script)], env=env, capture_output=True, text=True, check=True)


def test_import_creates_in_dependency_order(server):
    url, handler = server
    run("import-geoserver-config.sh", url, ROOT / "geoserver-config")

    creates = [path for method, path in handler.log if method == "POST"]
    kinds = [p.rsplit("/", 1)[-1] for p in creates]
    order = ["workspaces", "datastores", "featuretypes", "styles"]
    assert kinds == sorted(kinds, key=order.index), "parents must be created before children"
    assert "workspaces/test/datastores/postgres/featuretypes" in creates

    # Running it again updates in place instead of creating
    handler.log.clear()
    run("import-geoserver-config.sh", url, ROOT / "geoserver-config")
    assert not [m for m, _ in handler.log if m == "POST"]


def test_import_then_export_round_trips(server, tmp_path):
    url, _ = server
    source = ROOT / "geoserver-config"
    run("import-geoserver-config.sh", url, source)
    run("export-geoserver-config.sh", url, tmp_path)

    original = {p.relative_to(source) for p in source.rglob("*") if p.is_file()}
    exported = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()}
    assert original == exported

    for rel in original:
        if rel.name == "workspaces.json":
            continue  # the fake server's listing has no hrefs
        if rel.suffix == ".json":
            assert json.loads((tmp_path / rel).read_text()) == json.loads((source / rel).read_text()), rel
        else:
            assert (tmp_path / rel).read_bytes() == (source / rel).read_bytes(), rel


def test_scripts_need_a_password(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "GEOSERVER_PASSWORD"}
    for script in ("import-geoserver-config.sh", "export-geoserver-config.sh"):
        result = subprocess.run([str(ROOT / script)], env=env, capture_output=True, text=True)
        assert result.returncode != 0
        assert "GEOSERVER_PASSWORD" in result.stderr
