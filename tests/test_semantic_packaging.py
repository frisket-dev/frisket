"""Packaging contract for the bundled local semantic runtime."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _requirement_names(requirements: list[str]) -> set[str]:
    return {
        re.split(r"[\s\[<>=!~;]", requirement.strip(), maxsplit=1)[0].casefold()
        for requirement in requirements
    }


def test_fastembed_is_owned_only_by_the_base_install() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = pyproject["project"]

    assert "fastembed" in _requirement_names(project["dependencies"])
    assert "semantic" not in project["optional-dependencies"]
