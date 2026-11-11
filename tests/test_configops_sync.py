"""`configops sync` without a database: schedules, which layers load as which tables, and set-up errors."""

import textwrap
from datetime import datetime, timedelta

import pytest

from configops import ingest, sync
from configops.cli import main
from configops.loader import default_table, load
from configops.schedule import cron_matches, due

NOW = datetime(2026, 11, 10, 2, 0, 0)  # a Tuesday


# --- schedules


@pytest.mark.parametrize("update, last, expected", [
    ("manual", None, False),
    (None, None, False),
    ("daily", None, True),
    ("daily", NOW - timedelta(hours=23, minutes=59, seconds=30), True),  # yesterday's run, a little later
    ("daily", NOW - timedelta(hours=20), False),
    ("weekly", NOW - timedelta(days=6, hours=23, minutes=30), True),
    ("weekly", NOW - timedelta(days=5), False),
    ("monthly", NOW - timedelta(days=30), True),
    ("monthly", NOW - timedelta(days=20), False),
    ("0 1 * * *", NOW - timedelta(hours=2), True),  # 01:00 came since
    ("0 3 * * *", NOW - timedelta(hours=2), False),
    ("0 3 * * 1", NOW - timedelta(days=2), True),  # Monday 03:00 came since Sunday
    ("0 3 * * 1", NOW - timedelta(hours=22), False),
])
def test_due(update, last, expected):
    assert due(update, last, NOW) is expected


@pytest.mark.parametrize("expr, when, expected", [
    ("*/15 * * * *", datetime(2026, 11, 10, 2, 30), True),
    ("*/15 * * * *", datetime(2026, 11, 10, 2, 31), False),
    ("0 2 1-7 * *", datetime(2026, 11, 3, 2, 0), True),
    ("0 2 * * 0", datetime(2026, 11, 8, 2, 0), True),  # Sunday as 0
    ("0 2 * * 7", datetime(2026, 11, 8, 2, 0), True),  # and as 7
    ("0 2 15 * 1", datetime(2026, 11, 9, 2, 0), True),  # day or weekday, as in cron
    ("0 2 1,15 6 *", datetime(2026, 11, 15, 2, 0), False),
])
def test_cron(expr, when, expected):
    assert cron_matches(expr, when) is expected


# --- tables


def test_default_table_names():
    assert default_table("os-open-roads", "road_link") == "os_open_roads_road_link"
    assert default_table("x", "Conservation Areas") == "x_conservation_areas"
    assert default_table("2026", "a") == "t_2026_a"
    assert len(default_table("d" * 50, "l" * 50)) == 63


class FakeDataset:
    def __init__(self, name, tables=None):
        self.name, self.spec = name, {"tables": tables} if tables is not None else {}


FOUND = [("/tmp/a.gpkg", "road_link", "EPSG:27700"), ("/tmp/a.gpkg", "road_node", "EPSG:27700"), ("/tmp/b.shp", "b", None)]


def test_every_layer_by_default_with_project_crs_only_where_missing():
    layers = sync.plan_layers(FakeDataset("roads"), FOUND, "EPSG:27700")
    assert [(x.layer, x.table, x.crs) for x in layers] == [
        ("road_link", "roads_road_link", None), ("road_node", "roads_road_node", None), ("b", "roads_b", "EPSG:27700")]


def test_tables_pick_name_and_override_crs():
    layers = sync.plan_layers(FakeDataset("roads", {"road_node": {"name": "nodes", "crs": "EPSG:4326"}}), FOUND, "EPSG:27700")
    assert [(x.source, x.layer, x.table, x.crs) for x in layers] == [("/tmp/a.gpkg", "road_node", "nodes", "EPSG:4326")]


def test_a_layer_the_source_lacks():
    with pytest.raises(ingest.LoadError, match=r"names motorway, which the source doesn't have \(it has: b, road_link, road_node\)"):
        sync.plan_layers(FakeDataset("roads", {"motorway": {}}), FOUND, "EPSG:27700")


def test_two_layers_one_table():
    with pytest.raises(ingest.LoadError, match="same table: x"):
        sync.plan_layers(FakeDataset("roads", {"road_link": {"name": "x"}, "road_node": {"name": "x"}}), FOUND, "EPSG:27700")


# --- GDAL's errors


def test_gdal_errors_keep_what_matters():
    stderr = textwrap.dedent("""\
        Warning 1: something harmless
        ERROR 1: HTTP error code : 400
        ERROR 1: Terminating translation prematurely after failed
        translation of layer x (use -skipfailures to skip errors)
        """)
    assert ingest.gdal_errors(stderr) == "HTTP error code : 400"
    pg = 'ERROR 1: COPY statement failed.\nERROR:  date/time field value out of range: "0000/00/00"\nCONTEXT:  COPY t, line 23, column datemod: "0000/00/00"'
    assert ingest.gdal_errors(pg).startswith("COPY statement failed.; date/time field value out of range")
    assert ingest.BAD_DATE.search(pg)
    assert not ingest.BAD_DATE.search("ERROR 1: HTTP error code : 400")


def test_staging_names_are_unique_and_leave_room_for_gdals_names():
    names = {ingest.staging_name("r" * 63) for _ in range(50)}
    assert len(names) == 50
    assert all(len(n + "_ogc_fid_seq") <= 63 for n in names)


def test_suffixed_names_fit():
    assert ingest.suffixed("roads", "20261110_020000") == "roads_20261110_020000"
    assert len(ingest.suffixed("r" * 63, "20261110_020000")) == 63  # PostgreSQL's limit


# --- set-up errors

PROJECT = """\
apiVersion: configops/v1alpha1
kind: Project
metadata: { name: t }
spec:
  geoserver: { url: http://gs:8080/geoserver-cloud, credentials: { secret: gs } }
  database: { jndi: java:comp/env/jdbc/x, connection: { secret: pg } }
  workspace: t
---
apiVersion: configops/v1alpha1
kind: Source
metadata: { name: web }
spec: { type: url }
---
apiVersion: configops/v1alpha1
kind: Dataset
metadata: { name: roads }
spec: { source: web, url: "https://x/roads.zip" }
"""


@pytest.fixture
def proj(tmp_path, monkeypatch):
    (tmp_path / "project.yaml").write_text(PROJECT)
    for key in sync.DB_KEYS:
        monkeypatch.delenv(f"CONFIGOPS_PG_{key.upper()}", raising=False)
    return tmp_path


def test_missing_database_secret_says_what_to_set(proj, capsys):
    assert main(["sync", "-C", str(proj)]) == 2
    err = capsys.readouterr().err
    assert "the database secret pg is missing host, database, username, password" in err
    assert "$CONFIGOPS_PG_HOST" in err


def test_database_from_secrets_file_with_default_schemas(proj):
    (proj / "secrets.yaml").write_text("pg: { host: db, database: gis, username: u, password: p }\n")
    db = sync.database(load(proj).of_kind("Project")[0], proj)
    assert (db.host, db.port, db.live, db.staging, db.backup) == ("db", 5432, "data_layers", "staging", "backup")


def test_invalid_files_stop_sync(proj, capsys):
    (proj / "project.yaml").write_text(PROJECT.replace("source: web", "source: nowhere"))
    assert main(["sync", "-C", str(proj)]) == 2
    assert "no Source named 'nowhere'" in capsys.readouterr().err


def test_unknown_dataset(proj, capsys):
    assert main(["sync", "-C", str(proj), "rails"]) == 2
    assert "no Dataset named rails (known: roads)" in capsys.readouterr().err


def test_unreachable_database(proj, capsys, monkeypatch):
    for key, value in {"HOST": "127.0.0.1", "PORT": "1", "DATABASE": "x", "USERNAME": "u", "PASSWORD": "p"}.items():
        monkeypatch.setenv(f"CONFIGOPS_PG_{key}", value)
    assert main(["sync", "-C", str(proj)]) == 1
    assert "can't connect to the database at 127.0.0.1:1" in capsys.readouterr().err


# --- refreshing GeoServer after a load

REFRESH_PROJECT = PROJECT + """\
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: road-links }
spec: { dataset: roads, table: roads }
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: other }
spec: { table: { schema: x, name: y } }
---
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata: { name: inner }
spec: { layers: [road-links] }
---
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata: { name: outer }
spec: { layers: [inner, other] }
---
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata: { name: unrelated }
spec: { layers: [other] }
"""


class RefreshGeoServer:
    def __init__(self, published=True, cached=(), fail=False):
        self.published, self.cached, self.fail, self.calls = published, set(cached), fail, []

    def featuretype(self, ws, store, name):
        return {"featureType": {}} if self.published else None

    def recalculate(self, ws, store, name):
        if self.fail:
            from configops.geoserver import GeoServerError
            raise GeoServerError("PUT", "x", 500, "boom")
        self.calls.append(("recalculate", f"{ws}:{name}", store))

    def tile_layer(self, name):
        return {"enabled": True} if name in self.cached else None

    def truncate(self, name):
        self.calls.append(("truncate", name))


def refresh_files(tmp_path):
    (tmp_path / "project.yaml").write_text(REFRESH_PROJECT)
    return load(tmp_path)


def test_refresh_bounds_and_tiles_of_the_layers_and_groups_holding_them(tmp_path):
    gs = RefreshGeoServer(cached={"t:road_links", "t:outer", "t:unrelated"})
    db = ingest.Database("h", 5432, "d", "u", "p")
    note = sync.refresh(gs, refresh_files(tmp_path), db, "roads")
    assert gs.calls == [("recalculate", "t:road_links", "data_layers"), ("truncate", "t:road_links"), ("truncate", "t:outer")]
    assert note == "GeoServer: bounds of 1 layer(s) worked out again, cached tiles of 2 layer(s) or group(s) cleared"


def test_refresh_skips_unpublished_layers_and_reports_trouble(tmp_path):
    db = ingest.Database("h", 5432, "d", "u", "p")
    assert sync.refresh(RefreshGeoServer(published=False), refresh_files(tmp_path), db, "roads") == ""
    note = sync.refresh(RefreshGeoServer(fail=True), refresh_files(tmp_path), db, "roads")
    assert note.startswith("GeoServer not refreshed: GeoServer answered PUT x with HTTP 500")


def test_refresh_seeds_again_what_the_files_seed(tmp_path):
    (tmp_path / "project.yaml").write_text(REFRESH_PROJECT.replace(
        "spec: { dataset: roads, table: roads }",
        "spec: { dataset: roads, table: roads, cache: { gridsets: [EPSG:900913, EPSG:4326], seed: { zoom: [0, 5], gridsets: [EPSG:900913] } } }"))
    gs = RefreshGeoServer(cached={"t:road_links"})
    gs.seed = lambda name, gridset, fmt, zoom, bounds=None, threads=1: gs.calls.append(("seed", name, gridset, fmt, zoom))
    note = sync.refresh(gs, load(tmp_path), ingest.Database("h", 5432, "d", "u", "p"), "roads")
    assert gs.calls[-2:] == [("truncate", "t:road_links"), ("seed", "t:road_links", "EPSG:900913", "image/png", [0, 5])]
    assert note.endswith("cleared; seeding 1 again")
