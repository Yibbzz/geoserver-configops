"""Read every resource in a project directory, keeping where each one came from."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Holds secret values (spec: "Secrets"); never read as configuration
SECRET_FILES = {"secrets.yaml", "secrets.yml"}


def default_table(dataset: str, layer: str) -> str:
    """A Dataset layer's table when `tables` doesn't name it: <dataset>_<layer>, lowercase, a-z 0-9 _ only."""
    name = re.sub(r"[^a-z0-9]+", "_", f"{dataset}_{layer}".lower()).strip("_")
    if not name or name[0].isdigit():
        name = f"t_{name}"
    return name[:63]


@dataclass
class Problem:
    """Something wrong with the files. `line` is 1-based; 0 means the whole file."""

    file: Path
    line: int
    message: str
    resource: str = ""  # e.g. "Layer road-links"
    severity: str = "error"  # error | warning


@dataclass
class Resource:
    data: Any
    file: Path
    node: yaml.Node = field(repr=False)

    @property
    def kind(self) -> str | None:
        return self.data.get("kind") if isinstance(self.data, dict) else None

    @property
    def name(self) -> str | None:
        meta = self.data.get("metadata") if isinstance(self.data, dict) else None
        return meta.get("name") if isinstance(meta, dict) else None

    @property
    def spec(self) -> dict:
        spec = self.data.get("spec") if isinstance(self.data, dict) else None
        return spec if isinstance(spec, dict) else {}

    @property
    def label(self) -> str:
        return f"{self.kind or '?'} {self.name or '?'}"

    def line(self, path: tuple = ()) -> int:
        """1-based line of the value at `path` (keys and list indexes), or of its nearest parent."""
        node = self.node
        for step in path:
            child = None
            if isinstance(node, yaml.MappingNode):
                child = next((v for k, v in node.value if k.value == step), None)
            elif isinstance(node, yaml.SequenceNode) and isinstance(step, int) and step < len(node.value):
                child = node.value[step]
            if child is None:
                break
            node = child
        return node.start_mark.line + 1


@dataclass
class Project:
    root: Path
    resources: list[Resource]
    files: list[Path]
    problems: list[Problem]  # files that couldn't be read

    def of_kind(self, kind: str) -> list[Resource]:
        return [r for r in self.resources if r.kind == kind]

    def named(self, kind: str) -> dict[str, Resource]:
        """First resource of each name (duplicates are reported by validate)."""
        out: dict[str, Resource] = {}
        for r in self.of_kind(kind):
            if r.name is not None:
                out.setdefault(r.name, r)
        return out


def find_files(root: Path) -> list[Path]:
    """
    Every YAML file under `root`, hidden ones left out. Symlinked folders are
    followed: Kubernetes mounts a ConfigMap as symlinks into a hidden ..data
    folder, so a project's datasets/ is a link to a directory there.
    """
    files = []
    for folder, dirs, names in os.walk(root, followlinks=True):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            path = Path(folder) / name
            if (not name.startswith(".") and path.suffix in (".yaml", ".yml") and name not in SECRET_FILES
                    and path.is_file()):
                files.append(path)
    return sorted(files)


def load(root: Path) -> Project:
    root = Path(root)
    resources: list[Resource] = []
    problems: list[Problem] = []
    files = find_files(root)
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
            for node in yaml.compose_all(text, Loader=yaml.SafeLoader):
                if node is None:
                    continue
                data = yaml.SafeLoader(text).construct_document(node)
                if data is None:
                    continue
                resources.append(Resource(data=data, file=path, node=node))
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            reason = getattr(e, "problem", None) or str(e)
            problems.append(Problem(path, mark.line + 1 if mark else 0, f"not valid YAML: {reason}"))
        except UnicodeDecodeError:
            problems.append(Problem(path, 0, "not UTF-8 text"))
    return Project(root=root, resources=resources, files=files, problems=problems)
