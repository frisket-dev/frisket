"""Packaging contract for the optional FollowTheMoney entity stack."""

from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _locked_package(name: str) -> dict[str, object]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    matches = [package for package in lock["package"] if package["name"] == name]
    assert len(matches) == 1, f"expected one locked {name!r} package, found {matches!r}"
    return matches[0]


def _dependency_names(package: dict[str, object]) -> set[str]:
    dependencies = package.get("dependencies", [])
    assert isinstance(dependencies, list)
    return {
        dependency["name"]
        for dependency in dependencies
        if isinstance(dependency, dict) and isinstance(dependency.get("name"), str)
    }


def test_locked_entities_chain_still_hard_requires_sdist_only_pyicu() -> None:
    """Force a packaging-policy revisit when any upstream premise changes.

    FollowTheMoney currently requires normality, normality hard-requires PyICU,
    and the locked PyICU release publishes only an sdist. If a future lock gains
    official wheels or drops that dependency, this test should fail so Frisket
    removes the source-build warning and can reconsider a standard extra.
    """
    followthemoney = _locked_package("followthemoney")
    normality = _locked_package("normality")
    pyicu = _locked_package("pyicu")

    assert "normality" in _dependency_names(followthemoney)
    assert "pyicu" in _dependency_names(normality)
    assert "sdist" in pyicu
    assert "wheels" not in pyicu


def test_entities_remains_an_opt_in_extra_while_pyicu_is_sdist_only() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entities = pyproject["project"]["optional-dependencies"]["entities"]

    assert any(requirement.startswith("followthemoney") for requirement in entities)
    assert not any(
        requirement.startswith("followthemoney")
        for requirement in pyproject["project"]["dependencies"]
    )
