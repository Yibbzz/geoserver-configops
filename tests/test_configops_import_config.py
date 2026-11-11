"""`configops import --settings` and `--security` (configops/import_config.py) against a fake GeoServer."""

import types

from configops import import_config


class FakeGeoServer:
    def __init__(self, objects, rest_file="/**;GET,HEAD=ADMIN\n/**;POST,DELETE,PUT=ADMIN\n"):
        self.objects, self.rest_file = objects, rest_file

    def get(self, path):
        return self.objects.get(path)

    def names(self, path, key):
        return self.objects.get(f"names:{path}", [])

    def request(self, method, path, *args, **kwargs):
        assert method == "GET" and path in ("resource/security/rest.properties", "resource/gwc-gs.xml")
        if path == "resource/gwc-gs.xml":
            gwc = self.objects.get("gwc-gs.xml")
            return types.SimpleNamespace(status_code=200 if gwc else 404, text=gwc or "")
        return types.SimpleNamespace(status_code=200, text=self.rest_file)


def fresh():
    """A GeoServer as a fresh install has it."""
    d = import_config.DEFAULTS
    objects = {
        "settings": {"global": {"settings": dict(d["settings"]), **d["global"]}},
        "settings/contact": {"contact": {"id": "contact"}},
        "security/acl/catalog": {"mode": "HIDE"},
        "security/roles": {"roles": ["ADMIN", "GROUP_ADMIN"]},
        "security/usergroup/groups": {"groups": []},
        "security/usergroup/users": {"users": [{"userName": "admin", "enabled": True}]},
        "security/acl/layers": {"*.*.r": "*", "*.*.w": "GROUP_ADMIN,ADMIN"},
        "security/acl/services": {"wfs.CreateStoredQuery": "GROUP_ADMIN,ADMIN", "wfs.DropStoredQuery": "GROUP_ADMIN,ADMIN"},
        "names:workspaces": ["uk"],
        "workspaces/uk/settings": {"settings": {"charset": "UTF-8"}},  # defaults: no settings of its own
    }
    for name in ("wms", "wfs", "wcs", "wmts"):
        objects[f"services/{name}/settings"] = {name: {"name": name.upper(), **d["service"], **d[name]}}
    return objects


def test_a_fresh_geoserver_imports_as_nothing():
    gs = FakeGeoServer(fresh())
    assert import_config.settings_resources(gs) == ([], [])
    assert import_config.security_resources(gs) == ([], [], [])


def test_settings_only_what_differs():
    objects = fresh()
    objects["settings"]["global"]["settings"]["numDecimals"] = 6
    objects["settings"]["global"]["webUIMode"] = "REDIRECT"
    objects["settings/contact"]["contact"]["contactPerson"] = "Map Team"
    objects["security/acl/catalog"] = {"mode": "MIXED"}
    objects["workspaces/uk/settings"] = {"settings": {"workspace": {"name": "uk"}, "charset": "UTF-8", "numDecimals": 3}}
    wms = objects["services/wms/settings"]["wms"]
    wms.update(abstrct="Maps", keywords={"string": "only"}, watermark={"enabled": False, "position": "TOP_LEFT", "transparency": 100})
    found, notes = import_config.settings_resources(FakeGeoServer(objects))
    by_name = {r["metadata"]["name"]: r["spec"] for r in found}
    assert by_name["global"] == {"contact": {"contactPerson": "Map Team"}, "numDecimals": 6,
                                 "global": {"webUIMode": "REDIRECT", "catalogMode": "mixed"}}
    assert by_name["uk-settings"] == {"workspace": "uk", "numDecimals": 3}
    assert by_name["wms"] == {"abstract": "Maps", "keywords": ["only"], "watermark": {"position": "TOP_LEFT"}}
    assert set(by_name) == {"global", "uk-settings", "wms"}


def test_security_names_that_need_spelling_out_and_rules():
    objects = fresh()
    objects["security/roles"]["roles"] += ["ROLE_EDITOR", "data_admin"]
    objects["security/usergroup/groups"] = {"groups": ["Field Staff"]}
    objects["security/roles/group/Field Staff"] = {"roles": ["ROLE_EDITOR"]}
    objects["security/usergroup/users"]["users"].append({"userName": "alice@example.org", "enabled": False})
    objects["security/usergroup/user/alice@example.org/groups"] = {"groups": "Field Staff"}
    objects["security/roles/user/alice@example.org"] = {"roles": []}
    objects["security/acl/layers"]["*.*.r"] = "ROLE_EDITOR"  # a default rule changed
    objects["security/acl/layers"]["uk.roads.w"] = "GONE"
    gs = FakeGeoServer(objects, "/rest/uk/**;GET,HEAD=ROLE_EDITOR\n/**;GET,HEAD=ADMIN\n/**;POST,DELETE,PUT=ADMIN\n")
    found, notes, keys = import_config.security_resources(gs)
    specs = {(r["kind"], r["metadata"]["name"]): r["spec"] for r in found}
    assert specs[("Role", "role-editor")] == {}
    assert specs[("Role", "data-admin")] == {"name": "data_admin"}  # upper-casing wouldn't give it back
    assert specs[("Group", "field-staff")] == {"name": "Field Staff", "roles": ["ROLE_EDITOR"]}
    assert specs[("User", "alice-example-org")] == {
        "name": "alice@example.org", "password": {"secret": "geoserver-users", "key": "alice-example.org"},
        "groups": ["Field Staff"], "enabled": False}
    assert keys == ["alice-example.org"]
    rules = [s for (kind, _), s in specs.items() if kind == "AccessRule"]
    assert {"layer": {"workspace": "*", "layer": "*", "access": "read"}, "roles": ["ROLE_EDITOR"]} in rules
    assert {"layer": {"workspace": "uk", "layer": "roads", "access": "write"}, "roles": ["GONE"]} in rules
    assert {"rest": {"path": "/rest/uk/**", "methods": ["GET", "HEAD"]}, "roles": ["ROLE_EDITOR"]} in rules
    assert len(rules) == 3  # the unchanged defaults stay out
    assert notes == ["rule layers uk.roads.w names role(s) GeoServer doesn't have: GONE; add Roles for them"]


def test_tile_caching_defaults_and_a_workspaces_own_service():
    objects = fresh()
    objects["gwc-gs.xml"] = ("<GeoServerGWCConfig><version>1.1.0</version><cacheLayersByDefault>false</cacheLayersByDefault>"
                             "<metaTilingX>4</metaTilingX><gutter>8</gutter><defaultCachingGridSetIds><string>EPSG:900913</string>"
                             "</defaultCachingGridSetIds><defaultOtherCacheFormats><string>image/png</string><string>image/jpeg</string>"
                             "</defaultOtherCacheFormats></GeoServerGWCConfig>")
    global_wms = objects["services/wms/settings"]["wms"]
    objects["services/wms/workspaces/uk/settings"] = {"wms": {**global_wms, "workspace": {"name": "uk"}, "title": "UK maps"}}
    found, _ = import_config.settings_resources(FakeGeoServer(objects))
    by_name = {r["metadata"]["name"]: r["spec"] for r in found}
    assert by_name["global"] == {"global": {"tileCaching": {"cacheLayersByDefault": False, "gutter": 8,
                                                            "defaultCachingGridSetIds": ["EPSG:900913"]}}}
    assert by_name["uk-wms"] == {"service": "wms", "workspace": "uk", "title": "UK maps"}
