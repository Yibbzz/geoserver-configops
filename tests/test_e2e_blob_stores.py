"""Blob stores against the real GeoServer Cloud REST service and its GeoWebCache: a folder on the
shared data volume, Azure (Azurite) and Google Cloud Storage (fake-gcs-server), both started by
tests/e2e/up.sh; layers' tiles seeded into them, drift, and import back to a clean plan.

    E2E_GEOSERVER_URL=http://localhost:59090/rest E2E_DB_PORT=55433 E2E_DATA_DIR=tests/e2e/data \\
      E2E_GWC_URL=http://localhost:59090/geoserver-cloud/gwc/rest pytest -m e2e
"""

import os
import time
from pathlib import Path

import psycopg2
import requests
import pytest

from configops.cli import main
from configops.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
if not URL or not os.environ.get("E2E_DATA_DIR"):
    pytest.skip("E2E_GEOSERVER_URL or E2E_DATA_DIR not set", allow_module_level=True)

WS = "configops_blobs"
STORE = "e2e-tiles-on-volume"
AZURITE_KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="  # Azurite's well-known key
GCS = f"http://localhost:{os.environ.get('E2E_GCS_PORT', '54443')}"
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=int(os.environ.get("E2E_DB_PORT", 55433)),
          dbname="postgres", user="postgres", password=os.environ.get("E2E_DB_PASSWORD", "postgres"))


def sql(statement):
    conn = psycopg2.connect(**DB)
    with conn, conn.cursor() as cur:
        cur.execute(statement)
    conn.close()


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD, gwc_url=os.environ.get("E2E_GWC_URL"))
    gs.delete_workspace(WS)
    sql("CREATE EXTENSION IF NOT EXISTS postgis; DROP SCHEMA IF EXISTS e2e_blobs CASCADE; CREATE SCHEMA e2e_blobs;"
        "CREATE TABLE e2e_blobs.points (id serial PRIMARY KEY, geom geometry(Point, 27700));"
        "INSERT INTO e2e_blobs.points (geom) VALUES (ST_SetSRID(ST_MakePoint(500000, 150000), 27700))")
    yield gs
    gs.delete_workspace(WS)
    for store in (STORE, "e2e-azure", "e2e-gcs"):
        gs.gwc_request("DELETE", f"blobstores/{store}")
    sql("DROP SCHEMA IF EXISTS e2e_blobs CASCADE")


@pytest.fixture
def env(monkeypatch):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": str(DB["port"]), "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": DB["user"],
                       "E2EDB_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    for secret in ("AZURE", "E2E_AZURE_CREDENTIALS"):  # the files' Secret, and the one import names
        monkeypatch.setenv(f"CONFIGOPS_{secret}_ACCOUNTNAME", "devstoreaccount1")
        monkeypatch.setenv(f"CONFIGOPS_{secret}_ACCOUNTKEY", AZURITE_KEY)
    if os.environ.get("E2E_GWC_URL"):
        monkeypatch.setenv("CONFIGOPS_GWC_URL", os.environ["E2E_GWC_URL"])


def project_yaml() -> str:
    return f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-blobs }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
"""


@pytest.fixture
def project(tmp_path, env):
    (tmp_path / "project.yaml").write_text(project_yaml() + f"""---
apiVersion: configops/v1alpha1
kind: BlobStore
metadata: {{ name: {STORE} }}
spec: {{ type: file, path: e2e-gwc }}
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: points }}
spec: {{ table: {{ schema: e2e_blobs, name: points }}, cache: {{ gridsets: [EPSG:900913], blobStore: {STORE} }} }}
---
apiVersion: configops/v1alpha1
kind: BlobStore
metadata: {{ name: e2e-azure }}
spec: {{ type: azure, container: e2e-tiles, prefix: e2e, endpoint: "http://azurite:10000/devstoreaccount1", credentials: {{ secret: azure }} }}
---
apiVersion: configops/v1alpha1
kind: BlobStore
metadata: {{ name: e2e-gcs }}
spec: {{ type: gcs, bucket: e2e-tiles, prefix: e2e, project: e2e, endpoint: "http://fakegcs:4443" }}
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: points-azure }}
spec:
  table: {{ schema: e2e_blobs, name: points }}
  cache: {{ gridsets: [EPSG:4326], blobStore: e2e-azure, seed: {{ zoom: [0, 2] }} }}
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: points-gcs }}
spec:
  table: {{ schema: e2e_blobs, name: points }}
  cache: {{ gridsets: [EPSG:4326], blobStore: e2e-gcs, seed: {{ zoom: [0, 2] }} }}
""")
    return tmp_path


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def test_apply_drift_and_import_round_trip(gs, project, tmp_path_factory, capsys, env):
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    assert configops(capsys, "plan", "-C", str(project))[0] == 0
    assert gs.tile_layer(f"{WS}:points")["blobStoreId"] == STORE
    gs.gwc_request("PUT", f"blobstores/{STORE}", f'<FileBlobStore default="false"><id>{STORE}</id><enabled>true</enabled>'
                   "<baseDirectory>/data/elsewhere</baseDirectory><fileSystemBlockSize>4096</fileSystemBlockSize></FileBlobStore>")
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2 and 'path: "/data/elsewhere" -> "/data/e2e-gwc"' in out, out
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    imported = tmp_path_factory.mktemp("imported")
    (imported / "project.yaml").write_text(project_yaml())
    assert configops(capsys, "import", WS, "-C", str(imported))[0] == 0
    assert "kind: BlobStore" in (imported / f"{WS}.yaml").read_text()
    code, out = configops(capsys, "plan", "-C", str(imported))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out


def wait_for(check, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(2)
    return False


def test_azure_and_google_cloud_storage_are_seeded(gs, project, capsys, env):
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    assert configops(capsys, "plan", "-C", str(project))[0] == 0
    assert gs.tile_layer(f"{WS}:points_azure")["blobStoreId"] == "e2e-azure"

    def gcs_tiles():
        items = requests.get(f"{GCS}/storage/v1/b/e2e-tiles/o", timeout=10).json().get("items", [])
        return [i["name"] for i in items if i["name"].endswith(".png")]
    assert wait_for(lambda: len(gcs_tiles()) >= 3), gcs_tiles()  # zoom 0 to 2 around one point: a tile each

    def seeding():
        return gs.gwc_request("GET", f"seed/{WS}:points_azure.json").json().get("long-array-array")
    assert wait_for(lambda: seeding() == []), seeding()  # done, and with no tasks failed

    gs.gwc_request("PUT", "blobstores/e2e-azure", '<AzureBlobStore default="false"><id>e2e-azure</id><enabled>true</enabled>'
                   "<container>other</container><accountName>devstoreaccount1</accountName>"
                   f"<accountKey>{AZURITE_KEY}</accountKey><serviceURL>http://azurite:10000/devstoreaccount1</serviceURL>"
                   "<useHTTPS>false</useHTTPS></AzureBlobStore>")
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2 and 'container: "other" -> "e2e-tiles"' in out, out
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    assert configops(capsys, "plan", "-C", str(project))[0] == 0
