"""Store resources and the layers on them (gisdata/stores.py) against a fake GeoServer."""

import copy

from gisdata import ingest
from gisdata.loader import load
from gisdata.plan import Planner, ordered
from gisdata.validate import validate

PROJECT = """\
apiVersion: gisdata/v1alpha1
kind: Project
metadata: { name: t }
spec:
  geoserver: { url: http://gs:8080/geoserver-cloud, credentials: { secret: gs } }
  database: { jndi: java:comp/env/jdbc/x, connection: { secret: pg } }
  workspace: uk
"""

STORES = """\
apiVersion: gisdata/v1alpha1
kind: Store
metadata: { name: imagery }
spec: { type: cog, url: "s3://bucket/imagery.tif", description: Aerial }
---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: { name: tiles }
spec: { type: wmts, url: "https://tiles.example/wmts" }
---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: { name: warehouse }
spec:
  type: postgis
  connection: { host: db.example, port: 5432, database: gis, schema: public }
  credentials: { secret: warehouse-login }
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: { name: aerial }
spec: { store: imagery, title: Aerial photos }
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: { name: basemap }
spec: { store: tiles, resource: osm, cache: true }
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: { name: parcels }
spec: { store: warehouse, resource: parcels, style: polygon }
"""


class FakeGeoServer:
    """Stores, their resources and layers, kept as GeoServer's JSON by REST path."""

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.offers = {"imagery": ["imagery"], "tiles": ["osm", "terrain"], "warehouse": ["parcels", "roads"]}
        self.tiles: dict[str, dict] = {}

    def get(self, path, params=None):
        if params:  # ?list=: what a store offers
            return {"list": {"string": self.offers[path.split("/")[3]]}}
        return copy.deepcopy(self.objects.get(path))

    def upsert(self, path, collection, body):
        (key, value), = body.items()
        current = (self.objects.get(path) or {}).get(key) or {}
        self.objects[path] = {key: {**current, **copy.deepcopy(value)}}
        parts = path.split("/")
        if len(parts) == 6:  # a resource: GeoServer publishes it as a layer
            ws, kind, store, name = parts[1], parts[2], parts[3], parts[5]
            href = "" if kind == "wmtsstores" else f"http://gs/rest/{path}.json"
            self.objects[f"workspaces/{ws}/layers/{name}"] = {"layer": {
                "name": name, "queryable": True, "resource": {"href": href}, "defaultStyle": {"name": "raster"}}}

    def request(self, method, path, body=None, *args, **kwargs):
        (key, value), = body.items()
        self.objects[path][key].update(value)

    def update_layer(self, ws, name, style=None, styles=None, queryable=None, enabled=None, attribution=None):
        layer = self.objects[f"workspaces/{ws}/layers/{name}"]["layer"]
        if style is not None:
            layer["defaultStyle"] = {"name": style}
        if queryable is not None:
            layer["queryable"] = queryable

    def layer(self, ws, name):
        return self.get(f"workspaces/{ws}/layers/{name}")

    def tile_layer(self, name):
        return self.tiles.get(name)

    def put_tile_layer(self, name, gridsets, formats):
        self.tiles[name] = {"gridSubsets": [{"gridSetName": g} for g in gridsets], "mimeFormats": formats}

    def delete_tile_layer(self, name):
        self.tiles.pop(name, None)

    def workspace(self, ws):
        return {"workspace": {"name": ws}}

    def datastore(self, ws, name):
        return None

    def names(self, path, key):
        return []


def plan(tmp_path, gs, extra=STORES):
    (tmp_path / "project.yaml").write_text(PROJECT + "---\n" + extra)
    return ordered(Planner(load(tmp_path), tmp_path, gs, None, ingest.Database("h", 5432, "d", "u", "p")).plan())


def lines(changes):
    return [(c.action, c.kind, c.target) for c in changes]


def test_stores_then_their_layers_then_nothing_to_do(tmp_path, monkeypatch):
    monkeypatch.setenv("GISDATA_WAREHOUSE_LOGIN_USERNAME", "reader")
    monkeypatch.setenv("GISDATA_WAREHOUSE_LOGIN_PASSWORD", "secret-pw")
    gs = FakeGeoServer()
    changes = plan(tmp_path, gs)
    assert lines(changes) == [("+", "store", "uk:imagery"), ("+", "store", "uk:tiles"), ("+", "store", "uk:warehouse"),
                              ("+", "layer", "uk:aerial"), ("+", "layer", "uk:basemap"), ("+", "layer", "uk:parcels")]
    for c in changes:
        c.run()
    assert plan(tmp_path, gs) == []

    cog = gs.objects["workspaces/uk/coveragestores/imagery"]["coverageStore"]
    assert cog["type"] == "GeoTIFF" and cog["metadata"]["entry"]["cogSettings"]["rangeReaderSettings"] == "S3"
    params = {e["@key"]: e["$"] for e in gs.objects["workspaces/uk/datastores/warehouse"]["dataStore"]["connectionParameters"]["entry"]}
    assert params == {"host": "db.example", "port": "5432", "database": "gis", "schema": "public", "dbtype": "postgis",
                      "user": "reader", "passwd": "secret-pw"}
    # the only coverage the store offers, found without being named
    assert gs.objects["workspaces/uk/coveragestores/imagery/coverages/aerial"]["coverage"]["nativeCoverageName"] == "imagery"
    assert gs.objects["workspaces/uk/wmtsstores/tiles/layers/basemap"]["wmtsLayer"]["nativeName"] == "osm"
    assert "uk:basemap" in gs.tiles


def test_drift_on_a_store_and_a_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("GISDATA_WAREHOUSE_LOGIN_USERNAME", "reader")
    monkeypatch.setenv("GISDATA_WAREHOUSE_LOGIN_PASSWORD", "secret-pw")
    gs = FakeGeoServer()
    for c in plan(tmp_path, gs):
        c.run()
    gs.objects["workspaces/uk/coveragestores/imagery"]["coverageStore"]["url"] = "s3://bucket/other.tif"
    gs.objects["workspaces/uk/wmtsstores/tiles/layers/basemap"]["wmtsLayer"]["title"] = "Edited"
    changes = {c.target: c.fields for c in plan(tmp_path, gs)}
    assert changes["uk:imagery"] == [("url", "s3://bucket/other.tif", "s3://bucket/imagery.tif")]
    assert changes["uk:basemap"] == [("title", "Edited", "basemap")]


def test_a_store_offering_several_needs_resource(tmp_path):
    gs = FakeGeoServer()
    gs.objects["workspaces/uk/wmtsstores/tiles"] = {"wmtsStore": {"name": "tiles", "capabilitiesURL": "https://tiles.example/wmts"}}
    extra = """\
apiVersion: gisdata/v1alpha1
kind: Store
metadata: { name: tiles }
spec: { type: wmts, url: "https://tiles.example/wmts" }
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: { name: basemap }
spec: { store: tiles }
"""
    [change] = plan(tmp_path, gs, extra)
    assert (change.action, change.target) == ("!", "uk:basemap")
    assert change.note == "store tiles offers osm, terrain: say which with spec.resource"


def test_validate_stores(tmp_path):
    (tmp_path / "project.yaml").write_text(PROJECT + """---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: { name: no-address }
spec: { type: wms }
---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: { name: no-host }
spec: { type: postgis, connection: { database: gis } }
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: { name: a }
spec: { store: nowhere }
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: { name: b }
spec: { store: no-address, table: { schema: s, name: t } }
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: { name: c }
spec: { table: { schema: s, name: t }, resource: x }
""")
    messages = [p.message for p in validate(load(tmp_path)) if p.severity == "error"]
    assert "spec.url: a wms store needs its address" in messages
    assert "spec.connection: a postgis store needs host" in messages
    assert any(m.startswith("spec.store: no Store named 'nowhere'") for m in messages)
    assert "spec.table: a layer on a store doesn't take a table" in messages
    assert "spec.resource: only for a layer on a store" in messages


FILES = """\
apiVersion: gisdata/v1alpha1
kind: Store
metadata: { name: sites }
spec: { type: geopackage, path: vector/sites.gpkg }
---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: { name: roads }
spec: { type: shapefile, path: vector/roads.shp }
---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: { name: tiles }
spec: { type: pmtiles, url: "https://example.org/roads.pmtiles" }
---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: { name: dem }
spec: { type: geotiff, path: rasters/dem.tif }
---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: { name: photos }
spec: { type: imagemosaic, path: rasters/photos }
"""


def test_file_stores_point_at_the_data_volume(tmp_path):
    gs = FakeGeoServer()
    gs.offers.update(sites=["sites"], roads=["roads"], dem=["dem"], photos=["photos"])
    for c in plan(tmp_path, gs, FILES):
        c.run()
    assert plan(tmp_path, gs, FILES) == []

    def params(name):
        return {e["@key"]: e["$"] for e in gs.objects[f"workspaces/uk/datastores/{name}"]["dataStore"]["connectionParameters"]["entry"]}
    assert params("sites") == {"database": "/data/vector/sites.gpkg", "dbtype": "geopkg"}
    assert params("roads") == {"url": "file:/data/vector/roads.shp"}
    assert params("tiles") == {"pmtiles": "https://example.org/roads.pmtiles"}
    dem = gs.objects["workspaces/uk/coveragestores/dem"]["coverageStore"]
    assert (dem["type"], dem["url"]) == ("GeoTIFF", "file:/data/rasters/dem.tif") and "metadata" not in dem  # not a COG
    photos = gs.objects["workspaces/uk/coveragestores/photos"]["coverageStore"]
    assert (photos["type"], photos["url"]) == ("ImageMosaic", "file:/data/rasters/photos")


def test_validate_file_stores(tmp_path):
    (tmp_path / "project.yaml").write_text(PROJECT + """---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: { name: a }
spec: { type: geopackage }
---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: { name: b }
spec: { type: geotiff, url: "https://x/y.tif" }
---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: { name: c }
spec: { type: pmtiles }
---
apiVersion: gisdata/v1alpha1
kind: Store
metadata: { name: d }
spec: { type: shapefile, path: ../etc/passwd }
""")
    messages = [p.message for p in validate(load(tmp_path)) if p.severity == "error"]
    assert "spec.path: a geopackage store needs its file's path on the data volume, e.g. rasters/surrey.tif" in messages
    assert "spec.url: a geotiff store reads a file on the data volume: use spec.path" in messages
    assert "a pmtiles store needs spec.path (on the data volume) or spec.url, not both" in messages
    assert any(m.startswith("spec.path: '../etc/passwd' isn't allowed") or "does not match" in m for m in messages)
