"""`configops validate`: each kind of mistake is found, on the right line, in plain words."""

import json
import textwrap
from pathlib import Path

import pytest

from configops.cli import main
from configops.loader import load
from configops.validate import validate

PROJECT = """\
apiVersion: configops/v1alpha1
kind: Project
metadata: { name: test }
spec:
  geoserver: { url: http://gs:8080/geoserver-cloud, credentials: { secret: gs-admin } }
  database: { jndi: java:comp/env/jdbc/mypostgis, connection: { secret: pg } }
  workspace: uk
"""

DATA = """\
apiVersion: configops/v1alpha1
kind: Source
metadata: { name: os }
spec: { type: os-downloads, credentials: { secret: os-api } }
---
apiVersion: configops/v1alpha1
kind: Dataset
metadata: { name: roads }
spec:
  source: os
  product: OpenRoads
  tables: { road_link: {} }
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: road-links }
spec:
  dataset: roads
  table: road_link
  style: line
"""


def project(tmp_path: Path, extra: dict[str, str] | None = None, data: str = DATA) -> Path:
    files = {"project.yaml": PROJECT, "data.yaml": data, **(extra or {})}
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    return tmp_path


def problems(root: Path, severity: str = "error"):
    return [(p.file.name, p.line, p.message) for p in validate(load(root)) if p.severity == severity]


def one(root: Path, contains: str):
    found = [p for p in problems(root) if contains in p[2]]
    assert len(found) == 1, f"expected one problem with {contains!r}, got: {problems(root)}"
    return found[0]


def test_a_minimal_project_is_valid(tmp_path):
    assert problems(project(tmp_path)) == []
    assert problems(tmp_path, "warning") == []


def test_unknown_field_is_named_with_a_suggestion_and_its_line(tmp_path):
    root = project(tmp_path, data=DATA.replace("  style: line", "  style: line\n  titel: Roads"))
    assert one(root, "unknown field 'titel'") == ("data.yaml", 21, "spec.titel: unknown field 'titel' (did you mean 'title'?)")


def test_bad_names_say_what_is_allowed(tmp_path):
    root = project(tmp_path, data=DATA.replace("name: road-links", "name: Road_Links"))
    assert "use lowercase letters, digits and dashes" in one(root, "Road_Links")[2]


def test_schedule_lists_the_choices(tmp_path):
    root = project(tmp_path, data=DATA.replace("  product: OpenRoads", "  product: OpenRoads\n  update: fortnightly"))
    assert "five-field cron expression" in one(root, "fortnightly")[2]


@pytest.mark.parametrize("change, message, line", [
    (("source: os", "source: osm"), "no Source named 'osm' (known: os)", 10),
    (("dataset: roads", "dataset: rail"), "no Dataset named 'rail' (known: roads)", 18),
    (("table: road_link", "table: road_node"), "dataset roads has no table 'road_node' (its tables: road_link)", 19),
    (("style: line", "style: roads-thick"), "no style named 'roads-thick'", 20),
    (("  product: OpenRoads\n", ""), "spec.product: required for a os-downloads source", 10),
])
def test_broken_references(tmp_path, change, message, line):
    root = project(tmp_path, data=DATA.replace(*change))
    assert one(root, message)[1] == line


def test_a_layer_on_a_dataset_needs_a_table_name_not_a_schema_table(tmp_path):
    root = project(tmp_path, data=DATA.replace("table: road_link", "table: { schema: public, name: roads }"))
    assert problems(root), "a dataset layer's table must be one of the dataset's tables"


def test_style_files_and_style_resources_count(tmp_path):
    root = project(tmp_path, extra={
        "styles/roads-thick.sld": "<sld/>",
        "styles.yaml": "apiVersion: configops/v1alpha1\nkind: Style\nmetadata: { name: roads-thin }\nspec: { file: styles/thin.sld }\n",
        "styles/thin.sld": "<sld/>",
    }, data=DATA.replace("style: line", "style: roads-thick\n  styles: [roads-thin, thin]"))
    assert problems(root) == []


def test_a_missing_style_file_and_ambiguous_style_files(tmp_path):
    root = project(tmp_path, extra={
        "styles.yaml": "apiVersion: configops/v1alpha1\nkind: Style\nmetadata: { name: gone }\nspec: { file: styles/gone.sld }\n",
        "styles/twin.sld": "<sld/>",
        "styles/twin.mbstyle": "{}",
    })
    one(root, "styles/gone.sld not found")
    one(root, "two style files named twin")


def test_duplicate_names_point_at_the_first(tmp_path):
    second = DATA.split("---\n")[2]
    root = project(tmp_path, extra={"more.yaml": second})
    assert "name already used in data.yaml:14" in one(root, "already used")[2]


def test_layers_and_groups_cannot_share_a_geoserver_name(tmp_path):
    group = "apiVersion: configops/v1alpha1\nkind: LayerGroup\nmetadata: { name: roads-group }\nspec: { name: road_links, layers: [road-links] }\n"
    root = project(tmp_path, extra={"group.yaml": group})
    one(root, "GeoServer name uk:road_links is also used by Layer road-links")


def test_two_datasets_cannot_load_the_same_table(tmp_path):
    other = DATA.split("---\n")[1].replace("name: roads", "name: roads2").replace("road_link: {}", "x: { name: roads_road_link }")
    root = project(tmp_path, extra={"other.yaml": other})
    one(root, "table roads_road_link is also loaded by Dataset roads")


def test_group_loops_are_found_through_other_groups(tmp_path):
    groups = """\
    apiVersion: configops/v1alpha1
    kind: LayerGroup
    metadata: { name: a }
    spec: { layers: [road-links, b] }
    ---
    apiVersion: configops/v1alpha1
    kind: LayerGroup
    metadata: { name: b }
    spec: { layers: [a, { layer: nope }] }
    """
    root = project(tmp_path, extra={"groups.yaml": groups})
    assert "(a -> b -> a)" in one(root, "the group contains itself (a ")[2]
    one(root, "no Layer or LayerGroup named 'nope'")


def test_project_count(tmp_path):
    root = project(tmp_path)
    (root / "project.yaml").unlink()
    one(root, "no Project resource")
    project(tmp_path, extra={"again.yaml": PROJECT.replace("name: test", "name: test2")})
    one(tmp_path, "a second Project")


def test_unreadable_yaml_is_reported_and_secrets_file_is_never_read(tmp_path):
    root = project(tmp_path, extra={"broken.yaml": "kind: [Layer\nmetadata: x\n", "secrets.yaml": "not: [valid"})
    assert one(root, "not valid YAML") [:2] == ("broken.yaml", 2)
    assert not [p for p in problems(root) if p[0] == "secrets.yaml"]


def test_unused_things_are_warnings_not_errors(tmp_path):
    unused = "apiVersion: configops/v1alpha1\nkind: Source\nmetadata: { name: spare }\nspec: { type: url }\n"
    root = project(tmp_path, extra={"spare.yaml": unused}, data=DATA.split("---\n")[0] + "---\n" + DATA.split("---\n")[1])
    assert problems(root) == []
    warnings = [m for _, _, m in problems(root, "warning")]
    assert "no Dataset uses this source" in warnings
    assert "loaded but no Layer publishes it" in warnings


def test_cli_exit_codes_and_json(tmp_path, capsys):
    root = project(tmp_path)
    assert main(["validate", str(root)]) == 0
    assert "Valid: 4 resources in 2 files" in capsys.readouterr().out

    (root / "data.yaml").write_text(DATA.replace("style: line", "style: nope"))
    assert main(["validate", str(root), "--format", "json"]) == 1
    [problem] = json.loads(capsys.readouterr().out)
    assert problem["line"] == 20 and problem["severity"] == "error" and problem["resource"] == "Layer road-links"

    assert main(["validate", str(root / "missing")]) == 2
    assert main(["plan"]) == 2


CKAN_DATA = DATA.replace(
    "spec: { type: os-downloads, credentials: { secret: os-api } }", "spec: { type: ckan, url: https://data.gov.uk }"
).replace("  product: OpenRoads", "  product: conservation-areas\n  resource: 9dd40b81")


def test_a_ckan_source_and_a_dataset_naming_a_resource(tmp_path):
    assert problems(project(tmp_path, data=CKAN_DATA)) == []


def test_a_ckan_source_needs_its_address(tmp_path):
    root = project(tmp_path, data=CKAN_DATA.replace(", url: https://data.gov.uk", ""))
    assert one(root, "needs its address")[1] == 4


def test_source_url_and_resource_only_where_they_mean_something(tmp_path):
    root = project(tmp_path, data=DATA.replace("credentials: { secret: os-api }", "url: https://x.example").replace(
        "  product: OpenRoads", "  product: OpenRoads\n  resource: x"))
    one(root, "spec.url: not used by a os-downloads source")
    one(root, "spec.resource: only used with a ckan source")


def test_a_project_mounted_from_a_configmap(tmp_path):
    """Kubernetes mounts a ConfigMap as symlinks into a hidden ..data folder; folders are links too."""
    data = tmp_path / "..2026_11_10_17_00_00.123"
    (data / "datasets").mkdir(parents=True)
    (data / "styles").mkdir()
    (data / "project.yaml").write_text(textwrap.dedent(PROJECT))
    (data / "datasets" / "roads.yaml").write_text(textwrap.dedent(DATA.replace("style: line", "style: roads")))
    (data / "styles" / "roads.sld").write_text("<sld/>")
    (tmp_path / "..data").symlink_to(data.name)
    for name in ("project.yaml", "datasets", "styles"):
        (tmp_path / name).symlink_to(f"..data/{name}")
    project = load(tmp_path)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in project.files) == ["datasets/roads.yaml", "project.yaml"]
    assert problems(tmp_path) == []
