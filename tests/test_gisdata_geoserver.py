"""gisdata's GeoServer client without a GeoServer: the requests it makes and how it reads answers."""

import json

import pytest

from gisdata.geoserver import GeoServer, GeoServerError


class Response:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data = status, data
        self.text = text or (json.dumps(data) if data is not None else "")

    def json(self):
        return self._data


class FakeGeoServer:
    """Answers from a dict of path -> JSON; records every request."""

    def __init__(self, objects=None, fail=None):
        self.objects, self.fail, self.calls, self.auth = objects or {}, fail or {}, [], None

    def request(self, method, url, params=None, timeout=None, headers=None, json=None, data=None):
        path = url.split("/rest/", 1)[1]
        self.calls.append((method, path, params, json if json is not None else data, (headers or {}).get("Content-Type")))
        if (method, path) in self.fail:
            return Response(*self.fail[(method, path)])
        if method == "GET":
            key = path.removesuffix(".json")
            return Response(200, self.objects[key]) if key in self.objects else Response(404, text="not found")
        return Response(201 if method == "POST" else 200)


def client(**kw):
    fake = FakeGeoServer(**kw)
    return GeoServer("http://gs:8080/geoserver-cloud/", "admin", "pw", session=fake), fake


def test_upsert_creates_then_updates():
    gs, fake = client()
    assert gs.upsert_workspace("uk") == "created"
    assert fake.calls[-1][:2] == ("POST", "workspaces")
    fake.objects["workspaces/uk"] = {"workspace": {"name": "uk"}}
    assert gs.upsert_workspace("uk") == "updated"
    assert fake.calls[-1][:2] == ("PUT", "workspaces/uk")
    assert fake.auth == ("admin", "pw")


def test_names_are_one_path_segment():
    gs, fake = client()
    gs.layer("my ws", "a/b")
    assert fake.calls[-1][1] == "workspaces/my%20ws/layers/a%2Fb.json"


def test_missing_objects_are_none_and_deletes_report_it():
    gs, fake = client(fail={("DELETE", "styles/gone"): (404, None, "no such style")})
    assert gs.style("gone") is None
    assert gs.delete_style("gone") is False
    assert fake.calls[-1][2] == {"purge": "true", "recurse": "true"}


def test_errors_name_the_request_and_geoservers_reason():
    gs, _ = client(fail={("POST", "workspaces"): (500, None, "Workspace   named\n uk already exists")})
    with pytest.raises(GeoServerError, match=r"POST workspaces with HTTP 500: Workspace named uk already exists") as e:
        gs.upsert_workspace("uk")
    assert e.value.status == 500


def test_jndi_store_has_no_password():
    gs, fake = client()
    gs.upsert_jndi_datastore("uk", "postgis", "java:comp/env/jdbc/mypostgis", "data_layers")
    body = fake.calls[-1][3]["dataStore"]
    entries = {e["@key"]: e["$"] for e in body["connectionParameters"]["entry"]}
    assert entries == {"dbtype": "postgis", "jndiReferenceName": "java:comp/env/jdbc/mypostgis",
                       "schema": "data_layers", "Expose primary keys": "true"}


def test_feature_types_recalculate_their_bounding_boxes():
    gs, fake = client()
    fake.objects["workspaces/uk/datastores/postgis/featuretypes/roads"] = {"featureType": {"name": "roads"}}
    gs.upsert_featuretype("uk", "postgis", "roads", "os_open_roads_road_link", srs="EPSG:27700")
    put = [c for c in fake.calls if c[0] == "PUT"]
    assert put[0][3]["featureType"]["nativeName"] == "os_open_roads_road_link"
    assert put[0][3]["featureType"]["projectionPolicy"] == "FORCE_DECLARED"
    assert put[-1][2] == {"recalculate": "nativebbox,latlonbbox"}


def test_new_styles_are_posted_as_their_file():
    gs, fake = client()
    assert gs.upsert_style("roads", "<sld/>", ".sld", ws="uk") == "created"
    method, path, params, body, ctype = fake.calls[-1]
    assert (method, path, params, body, ctype) == ("POST", "workspaces/uk/styles", {"name": "roads", "raw": "true"},
                                                   b"<sld/>", "application/vnd.ogc.sld+xml")


def test_layer_updates_send_only_what_is_set():
    gs, fake = client()
    gs.update_layer("uk", "roads", queryable=False)
    assert fake.calls[-1][3] == {"layer": {"queryable": False}}
    calls = len(fake.calls)
    gs.update_layer("uk", "roads")
    assert len(fake.calls) == calls  # nothing to change, no request


def test_layer_group_modes_and_order():
    gs, fake = client()
    gs.upsert_layergroup("base", [("layer", "uk:counties", None), ("layer", "uk:roads", "os-road-link")], ws="uk", mode="opaque")
    group = fake.calls[-1][3]["layerGroup"]
    assert group["mode"] == "OPAQUE_CONTAINER"
    assert [p["name"] for p in group["publishables"]["published"]] == ["uk:counties", "uk:roads"]
    assert group["styles"]["style"] == ["", {"name": "os-road-link"}]


def test_collection_names_one_or_many():
    gs, fake = client()
    fake.objects["workspaces"] = {"workspaces": {"workspace": [{"name": "a"}, {"name": "b"}]}}
    assert gs.names("workspaces", "workspace") == ["a", "b"]
    fake.objects["workspaces"] = {"workspaces": {"workspace": {"name": "a"}}}
    assert gs.names("workspaces", "workspace") == ["a"]
    fake.objects["workspaces"] = {"workspaces": ""}  # what GeoServer sends for none
    assert gs.names("workspaces", "workspace") == []


def test_wait_retries_until_geoserver_answers(monkeypatch):
    import gisdata.geoserver as module
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    gs, fake = client(fail={("GET", "about/version.json"): (503, None, "starting")})
    answers = iter([503, 503, 200])

    def request(method, url, **kw):
        fake.calls.append((method, url))
        status = next(answers)
        return Response(status, {"about": {}} if status == 200 else None, "starting" if status != 200 else "")

    fake.request = request
    gs.wait(60, every=1)
    assert len(fake.calls) == 3


def test_wait_gives_up_and_never_retries_a_wrong_password(monkeypatch):
    import gisdata.geoserver as module
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    gs, _ = client(fail={("GET", "about/version.json"): (401, None, "bad login")})
    with pytest.raises(GeoServerError) as e:
        gs.wait(600)
    assert e.value.status == 401
    gs, _ = client(fail={("GET", "about/version.json"): (503, None, "starting")})
    with pytest.raises(GeoServerError):
        gs.wait(0)
