"""The examples under examples/ follow docs/spec/datasets.md.

A preview of what `gisdata validate` will check: resource shape, unique names,
and that every reference between resources (or to a style file) resolves.
"""

import re
from pathlib import Path

import pytest
import yaml

from conftest import ROOT

API_VERSION = "gisdata/v1alpha1"
KINDS = {"Project", "Source", "Dataset", "Layer", "LayerGroup", "Style"}
GROUP_MODES = {"single", "named", "container", "opaque"}
NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
SOURCE_TYPES = {"os-downloads", "url", "ckan", "ogc-api-features", "wfs"}
BUILT_IN_STYLES = {"generic", "line", "point", "polygon", "raster"}
PROJECTS = sorted(p.parent for p in (ROOT / "examples").glob("*/project.yaml"))


def load(project: Path) -> list[dict]:
    resources = []
    for path in sorted(project.rglob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc:
                doc["_file"] = path.relative_to(project)
                resources.append(doc)
    return resources


def by_kind(resources, kind):
    return {r["metadata"]["name"]: r for r in resources if r["kind"] == kind}


def test_there_are_examples():
    assert PROJECTS, "no examples/*/project.yaml found"


@pytest.mark.parametrize("project", PROJECTS, ids=lambda p: p.name)
def test_resources_have_the_right_shape(project):
    resources = load(project)
    for r in resources:
        where = f'{r["_file"]}: {r.get("kind")} {r.get("metadata", {}).get("name")}'
        assert r.get("apiVersion") == API_VERSION, where
        assert r.get("kind") in KINDS, where
        assert NAME.match(r.get("metadata", {}).get("name", "")), f"{where}: bad name"
        assert isinstance(r.get("spec"), dict), f"{where}: spec missing"
        assert "status" not in r, f"{where}: status is never written to files"
    assert len(by_kind(resources, "Project")) == 1, "exactly one Project per directory"
    for kind in KINDS:
        names = [r["metadata"]["name"] for r in resources if r["kind"] == kind]
        assert len(names) == len(set(names)), f"duplicate {kind} names"


@pytest.mark.parametrize("project", PROJECTS, ids=lambda p: p.name)
def test_references_resolve(project):
    resources = load(project)
    sources, datasets = by_kind(resources, "Source"), by_kind(resources, "Dataset")
    styles = set(by_kind(resources, "Style")) | {p.stem for p in (project / "styles").glob("*") if p.is_file()}

    for name, source in sources.items():
        assert source["spec"].get("type") in SOURCE_TYPES, f"Source {name}: unknown type"

    for name, dataset in datasets.items():
        assert dataset["spec"].get("source") in sources, f"Dataset {name}: unknown source"

    for name, layer in by_kind(resources, "Layer").items():
        spec = layer["spec"]
        if "dataset" in spec:
            dataset = datasets.get(spec["dataset"])
            assert dataset, f"Layer {name}: unknown dataset {spec['dataset']}"
            tables = dataset["spec"].get("tables")
            assert isinstance(spec.get("table"), str), f"Layer {name}: table must name one of the dataset's tables"
            if tables:  # no `tables` means every table in the product, not known until discover
                assert spec["table"] in tables, f"Layer {name}: dataset has no table {spec['table']}"
        else:
            table = spec.get("table")
            assert isinstance(table, dict) and table.get("schema") and table.get("name"), (
                f"Layer {name}: needs `dataset` + `table`, or `table: {{schema, name}}`"
            )
        for style in [spec.get("style"), *spec.get("styles", [])]:
            if style:
                assert style in styles | BUILT_IN_STYLES, f"Layer {name}: unknown style {style}"


@pytest.mark.parametrize("project", PROJECTS, ids=lambda p: p.name)
def test_layer_groups_resolve_and_have_no_cycles(project):
    resources = load(project)
    layers, groups = by_kind(resources, "Layer"), by_kind(resources, "LayerGroup")
    styles = set(by_kind(resources, "Style")) | {p.stem for p in (project / "styles").glob("*") if p.is_file()}

    def members(group):
        for entry in group["spec"].get("layers", []):
            yield entry if isinstance(entry, str) else entry.get("layer"), entry

    for name, group in groups.items():
        spec = group["spec"]
        assert spec.get("mode", "single") in GROUP_MODES, f"LayerGroup {name}: unknown mode"
        assert spec.get("layers"), f"LayerGroup {name}: needs at least one layer"
        for member, entry in members(group):
            assert member in layers or member in groups, f"LayerGroup {name}: unknown member {member}"
            if isinstance(entry, dict) and entry.get("style"):
                assert entry["style"] in styles | BUILT_IN_STYLES, f"LayerGroup {name}: unknown style {entry['style']}"

    def reaches(start, target, seen=()):
        for member, _ in members(groups[start]):
            if member == target or (member in groups and member not in seen and reaches(member, target, seen + (member,))):
                return True
        return False

    for name in groups:
        assert not reaches(name, name), f"LayerGroup {name} contains itself"
