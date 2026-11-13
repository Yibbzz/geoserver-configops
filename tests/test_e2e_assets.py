"""The files beside the configuration, seen working in GeoServer's services: an image a style points to
(drawn by WMS, and drawn again after it changes), a font (listed by GeoServer), and FreeMarker templates
for the workspace and for one layer (in GetFeatureInfo). Files the project drops are unmanaged, and
removed with --prune.

Needs GeoServer's OGC services as well as REST (tests/e2e/up.sh starts them all):

    E2E_GEOSERVER_URL=http://localhost:59090/geoserver-cloud/rest E2E_SERVICES_URL=http://localhost:59090/geoserver-cloud \
      E2E_DB_PORT=55433 E2E_DATA_DIR=tests/e2e/data E2E_REST_CONTAINER=gscloud-e2e-rest-1 E2E_WMS_CONTAINER=gscloud-e2e-wms-1 pytest -m e2e
"""

import os
import struct
import subprocess
import time
import zlib

import psycopg2
import pytest
import requests

from configops.cli import main
from configops.geoserver import GeoServer

pytestmark = pytest.mark.e2e

URL = os.environ.get("E2E_GEOSERVER_URL")
SERVICES = os.environ.get("E2E_SERVICES_URL")
if not URL or not SERVICES:
    pytest.skip("E2E_GEOSERVER_URL or E2E_SERVICES_URL not set", allow_module_level=True)

WS = "configops_assets"
PASSWORD = os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver")
DB = dict(host=os.environ.get("E2E_DB_HOST", "localhost"), port=int(os.environ.get("E2E_DB_PORT", 55433)),
          dbname="postgres", user="postgres", password=os.environ.get("E2E_DB_PASSWORD", "postgres"))
RED, BLUE = (255, 0, 0), (0, 0, 255)

PROJECT = f"""\
apiVersion: configops/v1alpha1
kind: Project
metadata: {{ name: e2e-assets }}
spec:
  geoserver: {{ url: {URL.removesuffix("/rest")}, credentials: {{ secret: e2egs }} }}
  database: {{ jndi: java:comp/env/jdbc/mypostgis, connection: {{ secret: e2edb }} }}
  workspace: {WS}
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: {{ name: zones }}
spec: {{ table: {{ schema: e2e_assets, name: zones }}, style: tiled }}
"""

SLD = """<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld" xmlns:xlink="http://www.w3.org/1999/xlink">
<NamedLayer><Name>tiled</Name><UserStyle><FeatureTypeStyle><Rule><PolygonSymbolizer><Fill><GraphicFill><Graphic>
<ExternalGraphic><OnlineResource xlink:type="simple" xlink:href="icons/tile.png"/><Format>image/png</Format></ExternalGraphic><Size>16</Size>
</Graphic></GraphicFill></Fill></PolygonSymbolizer></Rule></FeatureTypeStyle></UserStyle></NamedLayer></StyledLayerDescriptor>
"""


def png(rgb: tuple, size: int = 16) -> bytes:
    """A square image of one colour."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def sql(statement):
    conn = psycopg2.connect(**DB)
    with conn, conn.cursor() as cur:
        cur.execute(statement)
    conn.close()


@pytest.fixture(scope="module")
def gs():
    gs = GeoServer(URL.removesuffix("/rest"), "admin", PASSWORD)
    gs.delete_workspace(WS)
    sql("CREATE EXTENSION IF NOT EXISTS postgis; DROP SCHEMA IF EXISTS e2e_assets CASCADE; CREATE SCHEMA e2e_assets;"
        "CREATE TABLE e2e_assets.zones (id serial PRIMARY KEY, name text, geom geometry(Polygon, 27700));"
        "INSERT INTO e2e_assets.zones (name, geom) VALUES ('everywhere', ST_MakeEnvelope(0, 0, 1000, 1000, 27700))")
    yield gs
    gs.delete_workspace(WS)
    sql("DROP SCHEMA IF EXISTS e2e_assets CASCADE")


@pytest.fixture
def project(tmp_path, monkeypatch, gs):
    for key, value in {"E2EGS_USERNAME": "admin", "E2EGS_PASSWORD": PASSWORD, "E2EDB_HOST": DB["host"],
                       "E2EDB_PORT": str(DB["port"]), "E2EDB_DATABASE": "postgres", "E2EDB_USERNAME": DB["user"],
                       "E2EDB_PASSWORD": DB["password"]}.items():
        monkeypatch.setenv(f"CONFIGOPS_{key}", value)
    (tmp_path / "project.yaml").write_text(PROJECT)
    (tmp_path / "styles" / "icons").mkdir(parents=True)
    (tmp_path / "styles" / "tiled.sld").write_text(SLD)
    (tmp_path / "styles" / "icons" / "tile.png").write_bytes(png(RED))
    return tmp_path


def configops(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def eventually(fetch, check, seconds=90):
    """Other services hear of changes over the message bus: wait for them."""
    end = time.time() + seconds
    while True:
        value = fetch()
        if check(value) or time.time() > end:
            return value
        time.sleep(3)


def colour(tmp_path) -> tuple:
    """The colour in the middle of a map of the layer."""
    r = requests.get(f"{SERVICES}/{WS}/wms", timeout=60, params={
        "service": "WMS", "version": "1.1.1", "request": "GetMap", "layers": f"{WS}:zones", "styles": "", "srs": "EPSG:27700",
        "bbox": "100,100,900,900", "width": 64, "height": 64, "format": "image/png"})
    assert r.headers["content-type"] == "image/png", r.text[:300]
    (tmp_path / "map.png").write_bytes(r.content)
    out = subprocess.run(["gdallocationinfo", "-valonly", str(tmp_path / "map.png"), "32", "32"], capture_output=True, text=True, check=True)
    return tuple(int(v) for v in out.stdout.split()[:3])


def feature_info() -> str:
    return requests.get(f"{SERVICES}/{WS}/wms", timeout=60, params={
        "service": "WMS", "version": "1.1.1", "request": "GetFeatureInfo", "layers": f"{WS}:zones", "query_layers": f"{WS}:zones",
        "styles": "", "srs": "EPSG:27700", "bbox": "100,100,900,900", "width": 64, "height": 64, "x": 32, "y": 32,
        "info_format": "text/html"}).text


def test_a_styles_image_is_drawn_and_drawn_again_when_it_changes(gs, project, capsys):
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0, out
    assert f"+ file workspaces/{WS}/styles/icons/tile.png" in out
    assert configops(capsys, "plan", "-C", str(project)) == (0, "No changes: GeoServer matches the files.\n")
    assert eventually(lambda: colour(project), lambda c: c == RED) == RED

    # WMS holds the image in memory: after the upload apply has every service read it again
    (project / "styles" / "icons" / "tile.png").write_bytes(png(BLUE))
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2 and f"~ file workspaces/{WS}/styles/icons/tile.png" in out, out
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    assert eventually(lambda: colour(project), lambda c: c == BLUE) == BLUE

    # an image the project no longer has is unmanaged, like a layer would be; --prune removes it
    gs.request("PUT", f"resource/workspaces/{WS}/styles/icons/old.png", png(RED), content_type="image/png")
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 0 and f"? file workspaces/{WS}/styles/icons/old.png" in out, out
    assert configops(capsys, "apply", "-C", str(project), "--prune")[0] == 0
    assert gs.request("GET", f"resource/workspaces/{WS}/styles/icons/old.png", ok=(200, 404)).status_code == 404
    assert configops(capsys, "plan", "-C", str(project)) == (0, "No changes: GeoServer matches the files.\n")


def test_a_font_is_loaded(gs, project, capsys):
    """A font in styles/ goes to GeoServer's own styles folder and is in its list of fonts after apply."""
    pod, container = os.environ.get("E2E_DATA_POD"), os.environ.get("E2E_REST_CONTAINER")
    if not pod and not container:
        pytest.skip("E2E_DATA_POD or E2E_REST_CONTAINER not set: the font is one of the GeoServer image's, under a new name")
    if pod:
        ns, name = pod.split("/", 1)
        run = ["kubectl", "-n", ns, "exec", name, "--"]
    else:
        run = ["docker", "exec", container]
    # GeoServer Cloud's images have Noto fonts, standard GeoServer's DejaVu: either, its family renamed in place
    # in the font's name table (the same length, so nothing moves)
    for path, family, new in (("/usr/share/fonts/truetype/noto/NotoSansOgham-Regular.ttf", "Noto Sans Ogham", "Configops Ogham"),
                              ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVu Sans", "Configops D")):
        found = subprocess.run([*run, "cat", path], capture_output=True)
        if found.returncode == 0:
            font = found.stdout
            for encoding in ("ascii", "utf-16-be"):
                font = font.replace(family.encode(encoding), new.encode(encoding))
            break
    else:
        pytest.fail("neither font is in GeoServer's image")

    def fonts():
        return [name for name in gs.get("fonts")["fonts"] if name.startswith("Configops")]

    assert not fonts()
    (project / "styles" / "configops-ogham.ttf").write_bytes(font)
    try:
        code, out = configops(capsys, "apply", "-C", str(project))
        assert code == 0 and "+ file styles/configops-ogham.ttf" in out, out
        assert eventually(fonts, bool, seconds=30)
        assert configops(capsys, "plan", "-C", str(project)) == (0, "No changes: GeoServer matches the files.\n")
    finally:
        gs.request("DELETE", "resource/styles/configops-ogham.ttf", ok=(200, 404))
        gs.request("POST", "reset")


def test_templates_for_the_workspace_and_for_a_layer(gs, project, capsys):
    (project / "templates" / "zones").mkdir(parents=True)
    (project / "templates" / "header.ftl").write_text("<html><body><h1>Assets workspace</h1>\n")
    (project / "templates" / "zones" / "content.ftl").write_text("<#list features as f><p>Zone ${f.name.value}</p></#list>\n")
    assert configops(capsys, "validate", str(project))[0] == 0
    code, out = configops(capsys, "apply", "-C", str(project))
    assert code == 0 and f"+ template {WS} header.ftl" in out and f"+ template {WS}:zones content.ftl" in out, out
    assert configops(capsys, "plan", "-C", str(project)) == (0, "No changes: GeoServer matches the files.\n")
    info = eventually(feature_info, lambda t: "Zone everywhere" in t)
    assert "<h1>Assets workspace</h1>" in info and "<p>Zone everywhere</p>" in info, info

    (project / "templates" / "zones" / "content.ftl").write_text("<#list features as f><p>Area ${f.name.value}</p></#list>\n")
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 2 and f"~ template {WS}:zones content.ftl" in out, out
    assert configops(capsys, "apply", "-C", str(project))[0] == 0
    assert "<p>Area everywhere</p>" in eventually(feature_info, lambda t: "Area everywhere" in t)

    # templates the project no longer has: unmanaged, and gone with --prune (GeoServer's own are used again)
    (project / "templates" / "zones" / "content.ftl").unlink()
    (project / "templates" / "header.ftl").unlink()
    code, out = configops(capsys, "plan", "-C", str(project))
    assert code == 0 and f"? template workspaces/{WS}/header.ftl" in out and f"? template workspaces/{WS}/e2e_assets/zones/content.ftl" in out, out
    assert configops(capsys, "apply", "-C", str(project), "--prune")[0] == 0
    info = eventually(feature_info, lambda t: "Assets workspace" not in t)
    assert "Assets workspace" not in info and "Area everywhere" not in info and "everywhere" in info, info
    assert configops(capsys, "plan", "-C", str(project)) == (0, "No changes: GeoServer matches the files.\n")


def test_import_writes_the_images_and_templates(gs, project, tmp_path_factory, capsys):
    """What apply uploaded, import writes back as the same files: plan on them has nothing to do."""
    (project / "templates" / "zones").mkdir(parents=True)
    (project / "templates" / "footer.ftl").write_text("<p>Assets footer</p></body></html>\n")
    (project / "templates" / "zones" / "content.ftl").write_text("<p>Zones</p>\n")
    assert configops(capsys, "apply", "-C", str(project), "--prune")[0] == 0  # what earlier tests left goes
    target = tmp_path_factory.mktemp("imported")
    (target / "project.yaml").write_text(PROJECT.split("\n---\n")[0] + "\n")
    code, out = configops(capsys, "import", WS, "-C", str(target))
    assert code == 0 and "3 image(s) and template(s) in styles/ and templates/" in out, out
    for name in ("styles/icons/tile.png", "templates/footer.ftl", "templates/zones/content.ftl"):
        assert (target / name).read_bytes() == (project / name).read_bytes(), name
    assert configops(capsys, "validate", str(target))[0] == 0
    code, out = configops(capsys, "plan", "-C", str(target))
    assert (code, out.strip()) == (0, "No changes: GeoServer matches the files."), out
    # again, into the files it wrote: nothing to write
    code, out = configops(capsys, "import", WS, "-C", str(target))
    assert code == 0 and "No changes: the files already match GeoServer." in out, out
