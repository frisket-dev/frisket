from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

from scripts.release.release_preflight import package_metadata_findings

ROOT = Path(__file__).resolve().parents[1]


def run_preflight(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    return subprocess.run(
        ["scripts/release/release_preflight.py", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


def test_release_preflight_ignores_stale_pypi_artifacts(tmp_path):
    (tmp_path / "frisket_data-0.0.1.tar.gz").write_text("stale")

    proc = run_preflight(
        "--publish-pypi",
        "--confirm-owner-account",
        "owner-account-frisket-data",
        "--artifact-dir",
        str(tmp_path),
    )

    assert proc.returncode == 2
    with (ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    assert f"no frisket-data {version} artifacts" in proc.stderr


def test_release_preflight_static_checks_pass_without_network_or_upload():
    proc = run_preflight()

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "package metadata: OK" in proc.stdout
    assert "no explicit @frisket/frontend-host package found" not in proc.stdout
    assert "uv publish" not in proc.stdout


def test_release_preflight_recognizes_only_the_explicit_frontend_host_package(
    monkeypatch,
):
    errors, warnings = package_metadata_findings()

    assert errors == []
    assert warnings == []

    monkeypatch.setattr(
        "scripts.release.release_preflight.load_json",
        lambda _path: {"name": "frisket-web"},
    )
    errors, _warnings = package_metadata_findings()
    assert errors == [
        "web/package.json must stay private unless it is the explicit "
        "@frisket/frontend-host package"
    ]


def test_release_preflight_rejects_direct_url_dependencies(tmp_path):
    (tmp_path / "README.md").write_text("Frisket")
    (tmp_path / "LICENSE").write_text("Apache-2.0")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text(
        json.dumps({"name": "web", "private": True})
    )
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "frisket-data"
description = "test package"
license = "Apache-2.0"
readme = "README.md"
classifiers = ["Programming Language :: Python :: 3.12"]
dependencies = []

[project.optional-dependencies]
ner-spacy = [
  "spacy>=3.8,<3.9",
  "en_core_web_sm @ https://example.test/en_core_web_sm.whl",
]

[project.scripts]
frisket = "frisket.cli:main"

[project.urls]
Homepage = "https://example.test"
Repository = "https://example.test/repo"
Issues = "https://example.test/issues"
""".strip()
    )

    errors, _warnings = package_metadata_findings(tmp_path)
    assert errors == [
        "PyPI rejects direct-URL requirements; replace metadata URLs for: "
        "en_core_web_sm"
    ]


def test_release_preflight_refuses_pypi_publish_without_owner_confirmation():
    proc = run_preflight("--publish-pypi")

    assert proc.returncode == 2
    assert "refusing PyPI publish" in proc.stderr
    assert "uv publish" not in proc.stdout


def test_release_preflight_refuses_npm_publish_without_a_registry_workflow():
    proc = run_preflight(
        "--publish-npm",
        "--confirm-owner-account",
        "owner-account-frisket-data",
        "--execute-upload",
    )

    assert proc.returncode == 2
    assert "has no registry release workflow" in proc.stderr
