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
