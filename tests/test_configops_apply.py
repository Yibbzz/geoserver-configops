"""`configops apply` against a GeoServer that remembers what it's told: what apply leaves behind,
in what order, and that a layer is never served more openly than the files say on the way there."""

import copy
import json
import textwrap

import pytest

from configops import plan as plan_module
from configops.database import Database
from configops.geoserver import GeoServer, GeoServerError
from configops.loader import load
from configops.plan import Planner, ordered

PROJECT = """\
apiVersion: configops/v1alpha1
kind: Project
metadata: { name: t }
spec:
  geoserver: { url: http://gs/geoserver, credentials: { secret: gs } }
  database: { jndi: java:comp/env/jdbc/x, connection: { secret: pg } }
  workspace: uk
"""
FT = "workspaces/uk/datastores/public/featuretypes"
COLUMNS = [{"name": "id", "binding": "java.lang.Integer"}, {"name": "owner", "binding": "java.lang.String"},
           {"name": "geom", "binding": "org.locationtech.jts.geom.Point"}]


COLLECTIONS = {"layers": "layer", "layergroups": "layerGroup", "styles": "style", "datastores": "dataStore",
               "coveragestores": "coverageStore", "wmsstores": "wmsStore", "wmtsstores": "wmtsStore"}


class Response:
    def __init__(self, status=200, data=None, text=None):
        self.status_code, self._data = status, data
        self.text = text if text is not None else json.dumps(data if data is not None else {})
        self.content = self.text.encode()

    def json(self):
        return self._data


class MemoryGeoServer(GeoServer):
    """The REST API over a dict: a POST adds to its collection, a PUT merges, a GET reads. `fail` holds
    (method, path) pairs to refuse; `calls` is every request that changes something."""

    def __init__(self):
        self.url, self.rest, self.gwc = "http://gs", "http://gs/rest", "http://gs/gwc/rest"
        self.calls, self.fail, self.tiles, self.served = [], set(), {}, []
        self.objects = {
            "workspaces/uk": {"workspace": {"name": "uk"}},
            "workspaces/uk/datastores/public": {"dataStore": {"connectionParameters": {"entry": [
                {"@key": "jndiReferenceName", "$": "java:comp/env/jdbc/x"}, {"@key": "schema", "$": "public"}]}}},
            "security/roles": {"roles": ["ADMIN", "GROUP_ADMIN"]}, "security/usergroup/groups": {"groups": []},
            "security/usergroup/users": {"users": []}, "security/acl/layers": {"*.*.r": "*", "*.*.w": "ADMIN"},
            "security/acl/services": {},
        }

    version = "3.0.2"

    def request(self, method, path, body=None, content_type="application/json", params=None, ok=(200, 201)):
        key = path.removesuffix(".json")
        if method == "GET":
            if key == "resource/security/rest.properties":
                return Response(200, text="/**;GET,HEAD=ADMIN\n/**;POST,DELETE,PUT=ADMIN\n")
            collection = key.rsplit("/", 1)[-1]
            if collection in COLLECTIONS and key.startswith("workspaces/"):
                names = [k[len(key) + 1:] for k in self.objects if k.startswith(key + "/") and "/" not in k[len(key) + 1:]]
                return Response(200, {collection: {COLLECTIONS[collection]: [{"name": n} for n in names]}})
            if key.startswith("resource/") or key not in self.objects:
                return Response(404)
            found = copy.deepcopy(self.objects[key])
            ft = found.get("featureType")
            if ft is not None and not (ft.get("attributes") or {}).get("attribute"):
                ft["attributes"] = {"attribute": copy.deepcopy(COLUMNS)}  # an empty list: GeoServer lists every column
            return Response(200, found)
        self.calls.append((method, path, body, params))
        if (method, path) in self.fail:
            raise GeoServerError(method, path, 500, "refused")
        if method == "DELETE":
            if "/featuretypes/" in key:
                name = key.rsplit("/", 1)[1]
                for group in (v["layerGroup"] for k, v in self.objects.items() if "/layergroups/" in k):
                    if any(p["name"] == f"uk:{name}" for p in group["publishables"]["published"]):  # as GeoServer answers
                        raise GeoServerError(method, path, 500, "Unable to delete layer referenced by layer group")
                self.objects.pop(f"workspaces/uk/layers/{name}", None)
            self.objects.pop(key, None)
            return Response(200)
        if not (isinstance(body, dict) and len(body) == 1):
            return Response(200)
        kind, value = next(iter(body.items()))
        if method == "POST" and isinstance(value, dict) and "name" in value:
            self.objects[f"{key}/{value['name']}"] = {kind: copy.deepcopy(value)}
            if kind == "featureType":
                self.objects[f"workspaces/uk/layers/{value['name']}"] = {"layer": {
                    "name": value["name"], "resource": {"href": f"http://gs/rest/{key}/{value['name']}.json"}}}
                self.tiles[f"uk:{value['name']}"] = tile()  # GeoServer caches every new layer
        elif method == "POST" and key.startswith("security/acl/"):
            self.objects[key].update(value if isinstance(value, dict) else body)
        elif method == "PUT" and key in self.objects:
            self.objects[key].setdefault(kind, {}).update(copy.deepcopy(value))
        self.snapshot()
        return Response(201 if method == "POST" else 200)

    def snapshot(self):
        """What a request would be served now, per feature type: (name, enabled, the columns published)."""
        for key, value in self.objects.items():
            ft = value.get("featureType")
            if ft is not None and "/featuretypes/" in key:
                listed = (ft.get("attributes") or {}).get("attribute") or COLUMNS
                self.served.append((ft["name"], ft.get("enabled", True), [a["name"] for a in listed],
                                    self.objects["security/acl/layers"].get(f"uk.{ft['name']}.r", "*")))

    def gwc_request(self, method, path, body=None, accept="application/json"):
        name = path.split("/", 1)[1] if "/" in path else path
        if method == "GET":
            return Response(200, {"GeoServerLayer": self.tiles[name]}) if name in self.tiles else Response(404, text="Unknown layer")
        self.calls.append((method, f"gwc/{path}", body, None))
        if method == "PUT" and path.startswith("layers/"):
            self.tiles[name] = tile(["EPSG:4326"])
        elif method == "DELETE":
            self.tiles.pop(name, None)
        return Response(200)


def tile(gridsets=("EPSG:4326", "EPSG:900913")):
    return {"enabled": True, "gridSubsets": [{"gridSetName": g} for g in gridsets], "mimeFormats": ["image/png"]}


@pytest.fixture
def tables(monkeypatch):
    existing = {("public", "roads"), ("public", "sites"), ("other", "roads")}
    monkeypatch.setattr(plan_module, "tables_exist", lambda conn, schema, names: all((schema, n) in existing for n in names))
    return existing


def project(tmp_path, resources: str):
    (tmp_path / "project.yaml").write_text(PROJECT + textwrap.dedent(resources))
    return tmp_path


def plan(root, gs, prune=False):
    return ordered(Planner(load(root), root, gs, None, Database("h", 5432, "d", "u", "p"), prune).plan())


def apply(root, gs):
    changes = plan(root, gs)
    for c in changes:
        if c.run:
            c.run()
    return changes


def layer(spec: str, name: str = "roads") -> str:
    return f"---\napiVersion: configops/v1alpha1\nkind: Layer\nmetadata: {{ name: {name} }}\nspec: {spec}\n"


RULE = """\
---
apiVersion: configops/v1alpha1
kind: AccessRule
metadata: { name: roads-private }
spec: { layer: { workspace: uk, layer: roads, access: read }, roles: [ADMIN] }
"""


def test_a_layer_the_files_turn_off_stays_off(tmp_path, tables):
    root, gs = project(tmp_path, layer("{ table: roads, enabled: false }")), MemoryGeoServer()
    apply(root, gs)
    assert gs.objects[f"{FT}/roads"]["featureType"]["enabled"] is False
    assert all(enabled is False for _, enabled, _, _ in gs.served)  # at no point on the way
    assert plan(root, gs) == []
    gs.recalculate("uk", "public", "roads")  # as `configops refresh` does after new data
    assert gs.objects[f"{FT}/roads"]["featureType"]["enabled"] is False


def test_a_rule_is_there_before_the_layer_it_protects(tmp_path, tables):
    root, gs = project(tmp_path, layer("{ table: roads, cache: { seed: { zoom: [0, 3] } } }") + RULE), MemoryGeoServer()
    changes = apply(root, gs)
    kinds = [c.kind for c in changes]
    assert kinds.index("rule") < kinds.index("layer")
    assert gs.served and all(roles == "ADMIN" for _, _, _, roles in gs.served)  # never readable by everyone


def test_a_rule_is_removed_after_the_layer_it_protected(tmp_path, tables):
    root, gs = project(tmp_path, layer("{ table: roads }") + RULE), MemoryGeoServer()
    apply(root, gs)
    project(tmp_path, layer("{ table: sites }", "sites") + RULE.replace("roads", "sites"))
    deletions = [(c.kind, c.target) for c in plan(root, gs, prune=True) if c.action == "-"]
    assert deletions == [("layer", "uk:roads"), ("rule", "layers uk.roads.r")]


def test_only_the_listed_attributes_are_ever_served(tmp_path, tables):
    root, gs = project(tmp_path, layer("{ table: roads, attributes: [id] }")), MemoryGeoServer()
    apply(root, gs)
    assert [a["name"] for a in gs.objects[f"{FT}/roads"]["featureType"]["attributes"]["attribute"]] == ["geom", "id"]
    assert gs.objects[f"{FT}/roads"]["featureType"]["enabled"] is True
    assert all(columns == ["geom", "id"] for _, enabled, columns, _ in gs.served if enabled)  # `owner` never went out
    assert plan(root, gs) == []


def test_a_layer_whose_attributes_cant_be_set_is_left_off(tmp_path, tables):
    root, gs = project(tmp_path, layer("{ table: roads, attributes: [id] }")), MemoryGeoServer()
    changes = plan(root, gs)

    def refuse_the_list(method, path, body=None, *args, **kwargs):
        if method == "PUT" and path == f"{FT}/roads" and (body["featureType"].get("attributes") or {}).get("attribute"):
            raise GeoServerError(method, path, 500, "refused")
        return MemoryGeoServer.request(gs, method, path, body, *args, **kwargs)

    gs.request = refuse_the_list
    with pytest.raises(GeoServerError):
        next(c for c in changes if c.kind == "layer").run()
    assert gs.objects[f"{FT}/roads"]["featureType"]["enabled"] is False
    assert all(enabled is False for _, enabled, _, _ in gs.served)


def test_a_change_elsewhere_leaves_attributes_and_the_cache_alone(tmp_path, tables):
    spec = "{ table: roads, title: %s, attributes: [id], cache: { gridsets: ['EPSG:4326'], seed: { zoom: [0, 12] } } }"
    root, gs = project(tmp_path, layer(spec % "Roads")), MemoryGeoServer()
    apply(root, gs)
    assert any(path.startswith("gwc/seed/") for _, path, _, _ in gs.calls)  # a new layer is seeded
    gs.calls.clear(), gs.served.clear()
    project(tmp_path, layer(spec % "Highways"))
    changes = apply(root, gs)
    assert [(c.action, c.kind, [f for f, _, _ in c.fields]) for c in changes] == [("~", "layer", ["title"])]
    assert not [path for _, path, _, _ in gs.calls if path.startswith("gwc/")]  # not cached again, not seeded again
    assert all(enabled for _, enabled, _, _ in gs.served)  # nor turned off for a moment
    assert plan(root, gs) == []


def test_a_cache_change_sets_the_cache(tmp_path, tables):
    root, gs = project(tmp_path, layer("{ table: roads, cache: { gridsets: ['EPSG:4326'] } }")), MemoryGeoServer()
    apply(root, gs)
    gs.tiles["uk:roads"] = tile()
    gs.calls.clear()
    assert [[f for f, _, _ in c.fields] for c in apply(root, gs)] == [["cache.gridsets"]]
    assert [path for _, path, _, _ in gs.calls if path.startswith("gwc/")] == ["gwc/layers/uk:roads"]


GROUP = """\
---
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata: { name: base }
spec: { layers: [roads, sites] }
"""


def test_a_published_layer_whose_table_is_away_stays_in_its_groups(tmp_path, tables):
    root, gs = project(tmp_path, layer("{ table: roads }") + layer("{ table: sites }", "sites") + GROUP), MemoryGeoServer()
    apply(root, gs)
    assert plan(root, gs) == []
    tables.discard(("public", "sites"))  # dropped while it is loaded again
    changes = plan(root, gs)
    assert [(c.action, c.kind, c.target) for c in changes] == [("!", "layer", "uk:sites")]
    assert "left as GeoServer has it" in changes[0].note


def test_a_layer_moved_to_another_schema_is_put_back_in_its_groups(tmp_path, tables):
    root, gs = project(tmp_path, layer("{ table: roads }") + layer("{ table: sites }", "sites") + GROUP), MemoryGeoServer()
    apply(root, gs)
    project(tmp_path, layer("{ table: { schema: other, name: roads } }") + layer("{ table: sites }", "sites") + GROUP)
    gs.objects["workspaces/uk/datastores/other"] = copy.deepcopy(gs.objects["workspaces/uk/datastores/public"])
    gs.objects["workspaces/uk/datastores/other"]["dataStore"]["connectionParameters"]["entry"][1]["$"] = "other"
    changes = apply(root, gs)
    assert [(c.action, c.kind, c.target) for c in changes] == [("~", "layer", "uk:roads")]
    members = gs.objects["workspaces/uk/layergroups/base"]["layerGroup"]["publishables"]["published"]
    assert [m["name"] for m in members] == ["uk:roads", "uk:sites"]  # GeoServer deletes no layer a group holds: out, then back
    assert f"{FT}/roads" not in gs.objects and "workspaces/uk/datastores/other/featuretypes/roads" in gs.objects
    assert plan(root, gs) == []


def test_a_layer_that_cant_be_made_in_its_new_store_is_put_back(tmp_path, tables, capsys):
    root, gs = project(tmp_path, layer("{ table: roads }") + layer("{ table: sites }", "sites") + GROUP), MemoryGeoServer()
    apply(root, gs)
    project(tmp_path, layer("{ table: { schema: other, name: roads } }") + layer("{ table: sites }", "sites") + GROUP)
    gs.objects["workspaces/uk/datastores/other"] = copy.deepcopy(gs.objects["workspaces/uk/datastores/public"])
    gs.objects["workspaces/uk/datastores/other"]["dataStore"]["connectionParameters"]["entry"][1]["$"] = "other"
    gs.fail.add(("POST", "workspaces/uk/datastores/other/featuretypes"))
    with pytest.raises(GeoServerError):
        apply(root, gs)
    assert f"{FT}/roads" in gs.objects
    assert "was put back in public" in capsys.readouterr().err
    members = gs.objects["workspaces/uk/layergroups/base"]["layerGroup"]["publishables"]["published"]
    assert [m["name"] for m in members] == ["uk:roads", "uk:sites"]  # and back in its group


def test_a_groups_only_layer_isnt_moved(tmp_path, tables):
    only = GROUP.replace("[roads, sites]", "[roads]")
    root, gs = project(tmp_path, layer("{ table: roads }") + only), MemoryGeoServer()
    apply(root, gs)
    project(tmp_path, layer("{ table: { schema: other, name: roads } }") + only)
    gs.objects["workspaces/uk/datastores/other"] = copy.deepcopy(gs.objects["workspaces/uk/datastores/public"])
    gs.objects["workspaces/uk/datastores/other"]["dataStore"]["connectionParameters"]["entry"][1]["$"] = "other"
    gs.calls.clear()
    with pytest.raises(GeoServerError, match="is this group's only layer"):
        apply(root, gs)
    assert gs.calls == [] and f"{FT}/roads" in gs.objects  # nothing was touched
