def test_the_images_lock_file_has_every_dependency():
    """requirements.lock is what the image installs, with hashes: a dependency added to pyproject.toml must be added there."""
    import re
    import tomllib
    from pathlib import Path

    root = Path(__file__).parent.parent
    wanted = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    locked = set(re.findall(r"^([a-z0-9-]+)==", (root / "requirements.lock").read_text(), re.M))
    names = {re.split(r"[<>=!~\[ ;]", d, maxsplit=1)[0].lower().replace("_", "-") for d in wanted}
    assert names <= locked, names - locked


def test_every_example_project_is_valid():
    from pathlib import Path

    from configops.loader import load
    from configops.validate import validate

    examples = [p for p in sorted((Path(__file__).parent.parent / "examples").iterdir()) if (p / "project.yaml").is_file()]
    assert len(examples) >= 4
    for project in examples:
        assert [p.message for p in validate(load(project)) if p.severity == "error"] == [], project.name


def test_the_readmes_example_is_the_one_in_the_repository():
    """The README shows examples/city-maps/project.yaml whole: what a reader copies is what the tests validate."""
    from pathlib import Path

    root = Path(__file__).parent.parent
    assert (root / "examples" / "city-maps" / "project.yaml").read_text().rstrip() in (root / "README.md").read_text()
