"""`gisdata plan` against a fake GeoServer: what it compares, and in what order apply works."""

import textwrap

import pytest

from gisdata import ingest
from gisdata.loader import load
from gisdata.plan import Planner, ordered, print_plan

PROJECT = """\
apiVersion: gisdata/v1alpha1
kind: Project
metadata: { name: t }
spec:
  geoserver: { url: http://gs:8080/geoserver-cloud, credentials: { secret: gs } }
  database: { jndi: java:comp/env/jdbc/x, connection: { secret: pg } }
  workspace: uk
---
apiVersion: gisdata/v1alpha1
kind: Source
metadata: { name: web }
spec: { type: url }
---
apiVersion: gisdata/v1alpha1
kind: Dataset
metadata: { name: roads }
spec: { source: web, url: "https://x/roads.zip", tables: { road_link: {} } }
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: { name: road-links }
spec: { dataset: roads, table: road_link, title: Roads, style: roads, styles: [line], queryable: false }
---
apiVersion: gisdata/v1alpha1
kind: Layer
metadata: { name: sites }
spec: { table: { schema: field, name: sites } }
---
apiVersion: gisdata/v1alpha1
kind: LayerGroup
metadata: { name: base }
spec: { layers: [inner, { layer: road-links, style: line }] }
---
apiVersion: gisdata/v1alpha1
kind: LayerGroup
metadata: { name: inner }
spec: { mode: container, layers: [sites] }
"""


class FakeGeoServer:
    """Holds a catalog as GeoServer's JSON; only reads, as plan does."""

    def __init__(self):
        self.workspaces, self.stores, self.styles, self.layers, self.fts, self.groups = {}, {}, {}, {}, {}, {}
        self.tiles = {}  # ws:name -> GeoWebCache settings

    def tile_layer(self, name):
        return self.tiles.get(name)

    def workspace(self, ws):
        return {"workspace": {"name": ws}} if ws in self.workspaces else None

    def datastore(self, ws, name):
        return self.stores.get((ws, name))

    def style_body(self, name, ws=None):
        return self.styles.get((ws, name))

    def layer(self, ws, name):
        return self.layers.get((ws, name))

    def featuretype(self, ws, store, name):
        return self.fts.get((ws, store, name))

    def layergroup(self, name, ws=None):
        return self.groups.get((ws, name))

    def names(self, path, key):
        ws = path.split("/")[1]
        source = {"layer": self.layers, "layerGroup": self.groups, "dataStore": self.stores}.get(key)
        if key == "layerGroup":
            return [n for (w, n) in self.groups if w == ws]
        if key == "style":
            return [n for (w, n) in self.styles if w == ws]
        return [k[1] for k in source if k[0] == ws]

    def publish(self, ws, store, name, table, title, queryable=True, style="polygon", styles=()):
        self.fts[(ws, store, name)] = {"featureType": {"name": name, "nativeName": table, "title": title,
                                                       "enabled": True, "advertised": True}}
        self.layers[(ws, name)] = {"layer": {
            "name": name, "queryable": queryable, "defaultStyle": {"name": style},
            "styles": {"style": [{"name": s} for s in styles]} if styles else None,
            "resource": {"href": f"http://gs/rest/workspaces/{ws}/datastores/{store}/featuretypes/{name}.json"}}}


def store(jndi="java:comp/env/jdbc/x", schema="data_layers"):
    return {"dataStore": {"connectionParameters": {"entry": [{"@key": "jndiReferenceName", "$": jndi}, {"@key": "schema", "$": schema}]}}}


@pytest.fixture
def project(tmp_path):
    (tmp_path / "project.yaml").write_text(PROJECT)
    (tmp_path / "styles").mkdir()
    (tmp_path / "styles" / "roads.sld").write_text("<sld>roads</sld>\n")
    return tmp_path


@pytest.fixture
def tables(monkeypatch):
    existing = {("field", "sites"), ("data_layers", "roads_road_link")}
    monkeypatch.setattr(ingest, "tables_exist", lambda conn, schema, names: all((schema, n) in existing for n in names))
    return existing


def plan(root, gs, prune=False):
    db = ingest.Database("h", 5432, "d", "u", "p")
    return ordered(Planner(load(root), root, gs, None, db, prune).plan())


def lines(changes):
    return [(c.action, c.kind, c.target) for c in changes]


def converged():
    gs = FakeGeoServer()
    gs.workspaces["uk"] = True
    gs.stores[("uk", "data_layers")] = store()
    gs.stores[("uk", "field")] = store(schema="field")
    gs.styles[("uk", "roads")] = "<sld>roads</sld>"
    gs.publish("uk", "data_layers", "road_links", "roads_road_link", "Roads", queryable=False, style="uk:roads", styles=["line"])
    gs.publish("uk", "field", "sites", "sites", "sites", style="point")
    gs.groups[("uk", "inner")] = {"layerGroup": {"title": "inner", "mode": "CONTAINER",
                                                 "publishables": {"published": {"@type": "layer", "name": "uk:sites"}},
                                                 "styles": {"style": ""}}}
    gs.groups[("uk", "base")] = {"layerGroup": {"title": "base", "mode": "SINGLE", "publishables": {"published": [
        {"@type": "layerGroup", "name": "uk:inner"}, {"@type": "layer", "name": "uk:road_links"}]},
        "styles": {"style": ["", {"name": "line"}]}}}
    return gs


def test_an_empty_geoserver_gets_everything_in_order(project, tables):
    assert lines(plan(project, FakeGeoServer())) == [
        ("+", "workspace", "uk"),
        ("+", "store", "uk:data_layers"),
        ("+", "store", "uk:field"),
        ("+", "style", "uk:roads"),
        ("+", "layer", "uk:road_links"),
        ("+", "layer", "uk:sites"),
        ("+", "group", "uk:inner"),  # before the group that holds it
        ("+", "group", "uk:base"),
    ]


def test_a_missing_dataset_table_is_a_first_sync_and_a_missing_own_table_cant_be_done(project, tables):
    tables.clear()
    changes = {(c.action, c.kind, c.target): c for c in plan(project, FakeGeoServer())}
    assert changes[("+", "table", "data_layers.roads_road_link")].note == "first sync of dataset roads"
    blocked = changes[("!", "layer", "uk:sites")]
    assert blocked.run is None and "field.sites doesn't exist" in blocked.note


def test_groups_leave_out_layers_that_cant_be_published(project, tables):
    tables.discard(("field", "sites"))
    gs = converged()
    del gs.layers[("uk", "sites")], gs.groups[("uk", "inner")]
    base = gs.groups[("uk", "base")]["layerGroup"]  # GeoServer drops the members that don't exist
    base["publishables"]["published"], base["styles"]["style"] = [{"@type": "layer", "name": "uk:road_links"}], [{"name": "line"}]
    changes = plan(project, gs)
    assert lines(changes) == [("!", "layer", "uk:sites"), ("!", "group", "uk:inner")]  # base is unchanged
    assert changes[1].note == "none of its layers can be published yet"


def test_converged_means_no_changes(project, tables, capsys):
    assert plan(project, converged()) == []
    print_plan([])
    assert "No changes: GeoServer matches the files." in capsys.readouterr().out


def test_drift_shows_each_field(project, tables, capsys):
    gs = converged()
    gs.publish("uk", "data_layers", "road_links", "roads_road_link", "Edited", queryable=True, style="polygon", styles=["line"])
    gs.styles[("uk", "roads")] = "<sld>someone else's</sld>"
    gs.stores[("uk", "field")] = store(jndi="java:comp/env/jdbc/other", schema="field")
    gs.groups[("uk", "inner")]["layerGroup"]["mode"] = "SINGLE"
    changes = plan(project, gs)
    print_plan(changes)
    out = capsys.readouterr().out
    assert lines(changes) == [("~", "store", "uk:field"), ("~", "style", "uk:roads"), ("~", "layer", "uk:road_links"),
                              ("~", "group", "uk:inner")]
    assert 'title: "Edited" -> "Roads"' in out
    assert "queryable: True -> False" in out
    assert 'style: "polygon" -> "uk:roads"' in out
    assert 'jndiReferenceName: "java:comp/env/jdbc/other" -> "java:comp/env/jdbc/x"' in out
    assert 'mode: "SINGLE" -> "CONTAINER"' in out
    assert "Plan: 0 to add, 4 to change, 0 to delete, 0 unmanaged." in out


def test_fields_the_files_dont_set_are_not_compared(project, tables):
    gs = converged()
    gs.publish("uk", "field", "sites", "sites", "sites", style="anything")  # sites sets no style
    assert plan(project, gs) == []


def test_a_layer_moved_to_another_table(project, tables):
    gs = converged()
    gs.publish("uk", "data_layers", "road_links", "old_roads", "Roads", queryable=False, style="uk:roads", styles=["line"])
    [change] = plan(project, gs)
    assert change.fields == [("nativeName", "old_roads", "roads_road_link")]


def test_unmanaged_shown_then_pruned_outermost_first(project, tables):
    gs = converged()
    gs.publish("uk", "field", "old", "sites", "Old")
    gs.groups[("uk", "hand")] = {"layerGroup": {}}
    gs.styles[("uk", "leftover")] = "<sld/>"
    gs.stores[("uk", "spare")] = store(schema="spare")
    assert {(c.action, c.target) for c in plan(project, gs)} == {
        ("?", "uk:old"), ("?", "uk:hand"), ("?", "uk:leftover"), ("?", "uk:spare")}
    pruned = plan(project, gs, prune=True)
    assert lines(pruned) == [("-", "group", "uk:hand"), ("-", "layer", "uk:old"), ("-", "style", "uk:leftover"),
                             ("-", "store", "uk:spare")]
    assert all(c.run for c in pruned)


def test_other_workspaces_are_never_touched(project, tables):
    gs = converged()
    gs.publish("someone_else", "x", "theirs", "t", "Theirs")
    assert plan(project, gs, prune=True) == []


def test_style_files_override_and_built_ins(project, tables):
    (project / "extra.yaml").write_text(textwrap.dedent("""\
        apiVersion: gisdata/v1alpha1
        kind: Style
        metadata: { name: line }
        spec: { file: styles/roads.sld, workspace: styling }
        """))
    changes = lines(plan(project, converged()))
    # a project style named like a built-in one is the project's, in its own workspace
    assert ("+", "workspace", "styling") in changes
    assert ("+", "style", "styling:line") in changes
    assert ("~", "layer", "uk:road_links") in changes  # its extra style is now styling:line


def tile(gridsets=("EPSG:4326", "EPSG:900913"), formats=("image/png",)):
    return {"enabled": True, "gridSubsets": [{"gridSetName": g} for g in gridsets], "mimeFormats": list(formats)}


def test_the_cache_geoserver_makes_for_new_layers_is_dropped(project, tables, capsys):
    gs = converged()
    gs.tiles["uk:sites"] = tile()  # GeoServer Cloud caches every new layer
    [change] = plan(project, gs)
    assert (change.action, change.target, change.fields) == ("~", "uk:sites", [
        ("cache", {"gridsets": ["EPSG:4326", "EPSG:900913"], "formats": ["image/png"]}, False)])
    print_plan([change])
    assert "cache: cached (EPSG:4326, EPSG:900913; image/png) -> False" in capsys.readouterr().out


def test_cache_true_and_cache_settings(project, tables):
    (project / "project.yaml").write_text(PROJECT.replace(
        "spec: { table: { schema: field, name: sites } }",
        "spec: { table: { schema: field, name: sites }, cache: { gridsets: [EPSG:27700], formats: [image/png, image/jpeg] } }"
    ).replace("spec: { mode: container, layers: [sites] }", "spec: { mode: container, layers: [sites], cache: true }"))
    gs = converged()
    changes = {c.target: c.fields for c in plan(project, gs)}
    assert changes["uk:sites"] == [("cache", False, {"gridsets": ["EPSG:27700"], "formats": ["image/jpeg", "image/png"]})]
    assert changes["uk:inner"] == [("cache", False, {"gridsets": ["EPSG:4326", "EPSG:900913"], "formats": ["image/png"]})]
    gs.tiles["uk:sites"] = tile(["EPSG:27700"], ["image/jpeg", "image/png"])
    gs.tiles["uk:inner"] = tile()
    assert plan(project, gs) == []


def test_attribution_from_the_file_or_the_sources_licence(project, tables, monkeypatch):
    (project / "project.yaml").write_text(PROJECT.replace("spec: { type: url }", "spec: { type: os-downloads }").replace(
        'spec: { source: web, url: "https://x/roads.zip", tables: { road_link: {} } }',
        "spec: { source: web, product: OpenRoads, tables: { road_link: {} } }",
    ).replace("spec: { table: { schema: field, name: sites } }", "spec: { table: { schema: field, name: sites }, attribution: Our survey }"))
    changes = {c.target: c.fields for c in plan(project, converged())}
    [(field, live, wanted)] = changes["uk:road_links"]
    assert field == "attribution" and live is None
    assert wanted.startswith("Contains OS data © Crown copyright and database right 20")
    assert changes["uk:sites"] == [("attribution", None, "Our survey")]


def test_wait_covers_a_database_still_starting(project, monkeypatch):
    """A Job started with a fresh install finds the database refusing connections at first."""
    import psycopg2

    from gisdata import plan as plan_module

    for key, value in {"GS_USERNAME": "a", "GS_PASSWORD": "b", "PG_HOST": "db", "PG_DATABASE": "d",
                       "PG_USERNAME": "u", "PG_PASSWORD": "p"}.items():
        monkeypatch.setenv(f"GISDATA_{key}", value)
    attempts = []

    def connect(self):
        attempts.append(1)
        if len(attempts) < 3:
            raise psycopg2.OperationalError('connection to server at "db" failed: Connection refused')
        return "conn"

    monkeypatch.setattr(ingest.Database, "connect", connect)
    monkeypatch.setattr(plan_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(plan_module.GeoServer, "wait", lambda self, seconds: None)
    files, gs, conn, db = plan_module.connect(project, None, wait=600)
    assert conn == "conn" and len(attempts) == 3

    attempts.clear()
    with pytest.raises(plan_module.PlanError, match="Connection refused"):
        plan_module.connect(project, None, wait=0)  # no wait: the first refusal is final
