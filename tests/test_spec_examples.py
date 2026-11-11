"""Every example under examples/ is valid against the spec, with no warnings."""

from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from conftest import ROOT
from configops.loader import load
from configops.validate import schema, validate

PROJECTS = sorted(p.parent for p in (ROOT / "examples").glob("*/project.yaml"))


def test_there_are_examples():
    assert PROJECTS, "no examples/*/project.yaml found"


def test_the_schema_is_a_valid_json_schema():
    Draft202012Validator.check_schema(schema())


@pytest.mark.parametrize("project", PROJECTS, ids=lambda p: p.name)
def test_example_is_valid(project: Path):
    problems = validate(load(project))
    assert not problems, "\n".join(f"{p.file.name}:{p.line} {p.severity}: {p.resource} {p.message}" for p in problems)
