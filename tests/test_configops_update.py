"""`configops import` into an existing project: only what GeoServer changed is written, in place."""

from configops.loader import load
from configops.update import merge

PROJECT = """\
apiVersion: configops/v1alpha1
kind: Project
metadata: { name: test }
spec:
  geoserver: { url: http://geoserver, credentials: { secret: gs } }
  database: { jndi: java:comp/env/jdbc/x, connection: { secret: db } }
  workspace: uk
"""

LAYERS = """\
# Roads, from OS Open Roads
apiVersion: configops/v1alpha1
kind: Layer
metadata:
  name: roads   # our name
spec:
  table: { schema: data_layers, name: roads }
  title: Roads
  style: line   # thin lines
  cache:
    gridsets: [EPSG:900913]   # web maps only
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: old }
spec: { table: { schema: data_layers, name: old }, enabled: true }
"""


def layer(name, **spec):
    return {"apiVersion": "configops/v1alpha1", "kind": "Layer", "metadata": {"name": name},
            "spec": {"table": {"schema": "data_layers", "name": name}, **spec}}


def project(tmp_path):
    (tmp_path / "project.yaml").write_text(PROJECT)
    (tmp_path / "layers.yaml").write_text(LAYERS)
    return load(tmp_path)


def test_the_same_however_written_leaves_the_files_alone(tmp_path):
    existing = project(tmp_path)
    imported = {tmp_path / "uk.yaml": [
        layer("roads", title="Roads", style="line", cache={"gridsets": ["EPSG:900913"], "formats": ["image/png"]}),
        layer("old")]}  # enabled: true is the default, so it means the same
    result = merge(tmp_path, existing, imported, "uk", False, False, prune=False)
    assert (result.changed, result.added, result.missing) == ([], [], [])
    assert (tmp_path / "layers.yaml").read_text() == LAYERS
    assert not (tmp_path / "uk.yaml").exists()


def test_only_the_changed_keys_are_rewritten_and_comments_stay(tmp_path):
    existing = project(tmp_path)
    imported = {tmp_path / "uk.yaml": [
        layer("roads", title="Roads (edited)", style="line", cache={"gridsets": ["EPSG:900913"]}, advertised=False),
        layer("old")]}
    result = merge(tmp_path, existing, imported, "uk", False, False, prune=False)
    assert result.changed == ["Layer roads (title, advertised)"]
    text = (tmp_path / "layers.yaml").read_text()
    assert text == LAYERS.replace("  title: Roads\n", "  title: Roads (edited)\n").replace(
        "  cache:\n    gridsets: [EPSG:900913]   # web maps only\n",
        "  cache:\n    gridsets: [EPSG:900913]   # web maps only\n  advertised: false\n")


def test_a_key_geoserver_no_longer_has_is_removed(tmp_path):
    existing = project(tmp_path)
    imported = {tmp_path / "uk.yaml": [layer("roads", title="Roads", style="line"), layer("old")]}
    merge(tmp_path, existing, imported, "uk", False, False, prune=False)
    text = (tmp_path / "layers.yaml").read_text()
    assert "cache" not in text and "web maps only" not in text and "# thin lines" in text


def test_a_spec_on_one_line_is_written_whole(tmp_path):
    existing = project(tmp_path)
    imported = {tmp_path / "uk.yaml": [layer("roads", title="Roads", style="line", cache={"gridsets": ["EPSG:900913"]}),
                                       layer("old", enabled=False)]}
    merge(tmp_path, existing, imported, "uk", False, False, prune=False)
    reloaded = load(tmp_path).named("Layer")
    assert reloaded["old"].spec == {"table": {"schema": "data_layers", "name": "old"}, "enabled": False}
    assert reloaded["roads"].spec["title"] == "Roads"


def test_new_resources_are_added_and_missing_ones_reported_or_pruned(tmp_path):
    existing = project(tmp_path)
    imported = {tmp_path / "uk.yaml": [layer("roads", title="Roads", style="line", cache={"gridsets": ["EPSG:900913"]}),
                                       layer("parishes", title="Parishes")]}
    result = merge(tmp_path, existing, imported, "uk", False, False, prune=False)
    assert result.added == ["Layer parishes"] and result.missing == ["Layer old"]
    assert "name: parishes" in (tmp_path / "uk.yaml").read_text()

    existing = load(tmp_path)
    result = merge(tmp_path, existing, imported, "uk", False, False, prune=True)
    assert result.removed == ["Layer old"] and result.added == []
    text = (tmp_path / "layers.yaml").read_text()
    assert "name: old" not in text and text.startswith("# Roads, from OS Open Roads") and not text.rstrip().endswith("---")


def test_names_in_the_files_are_kept_and_references_follow_them(tmp_path):
    (tmp_path / "project.yaml").write_text(PROJECT)
    (tmp_path / "layers.yaml").write_text("""\
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: road-links }
spec: { name: roads, table: { schema: data_layers, name: roads } }
---
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata: { name: base }
spec:
  layers: [road-links]
""")
    existing = load(tmp_path)
    group = {"apiVersion": "configops/v1alpha1", "kind": "LayerGroup", "metadata": {"name": "base"},
             "spec": {"layers": ["roads"], "title": "Base"}}
    result = merge(tmp_path, existing, {tmp_path / "uk.yaml": [layer("roads"), group]}, "uk", False, False, prune=False)
    assert result.changed == ["LayerGroup base (title)"] and result.added == []
    assert load(tmp_path).named("LayerGroup")["base"].spec == {"layers": ["road-links"], "title": "Base"}


def test_what_import_cant_read_is_kept_from_the_files(tmp_path):
    (tmp_path / "project.yaml").write_text(PROJECT + """\
---
apiVersion: configops/v1alpha1
kind: User
metadata: { name: editor }
spec: { password: { secret: editor } }
""")
    existing = load(tmp_path)
    user = {"apiVersion": "configops/v1alpha1", "kind": "User", "metadata": {"name": "editor"}, "spec": {"roles": ["EDITOR"]}}
    result = merge(tmp_path, existing, {tmp_path / "security.yaml": [user]}, None, False, True, prune=False)
    assert result.changed == ["User editor (roles)"]
    assert load(tmp_path).named("User")["editor"].spec == {"password": {"secret": "editor"}, "roles": ["EDITOR"]}


def test_a_default_that_depends_on_the_layer_counts_as_the_same(tmp_path):
    """Import leaves out a style that's the default for the geometry: a file naming it means the same."""
    existing = project(tmp_path)
    roads = {**layer("roads", title="Roads", cache={"gridsets": ["EPSG:900913"]}), "x-defaults": {"style": "line"}}
    result = merge(tmp_path, existing, {tmp_path / "uk.yaml": [roads, layer("old")]}, "uk", False, False, prune=False)
    assert result.changed == [] and (tmp_path / "layers.yaml").read_text() == LAYERS

    roads["x-defaults"] = {"style": "point"}  # GeoServer's style changed to its default, so import left it out
    result = merge(tmp_path, existing, {tmp_path / "uk.yaml": [roads, layer("old")]}, "uk", False, False, prune=False)
    assert result.changed == ["Layer roads (style)"]
    assert "  style: point\n" in (tmp_path / "layers.yaml").read_text()
