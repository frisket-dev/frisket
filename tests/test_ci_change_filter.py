"""Execute CI path selection with small and pipe-buffer-sized Git diffs."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
import yaml


@pytest.mark.parametrize(
    ("files", "enabled", "target"),
    [
        (
            [
                "src/frisket/engine/example.py",
                "sidecar/example.py",
                "docker-compose.models-gpu.yml",
                "web/src/example.ts",
                "scripts/example.py",
                *[f"docs/large-diff-{index:06d}.md" for index in range(40_000)],
            ],
            {"backend", "sidecar", "models_gpu", "frontend", "infra"},
            "tests",
        ),
        (["src/frisket/server/example.py"], {"backend"}, "tests/server tests/*.py"),
        (["web/src/example.ts"], {"frontend"}, "tests"),
        (["docs/example.md"], set(), "tests"),
        ([], set(), "tests"),
    ],
    ids=("large-early-matches", "backend-leaf", "frontend-only", "docs-only", "empty"),
)
def test_ci_change_filter_executes_real_workflow(tmp_path, files, enabled, target):
    # Execute the workflow artifact; Git is the only replaced transport.
    workflow = yaml.safe_load(
        # rule19: executes this workflow shell with real pipeline/SIGPIPE behavior.
        (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    )
    script = next(
        step["run"]
        for step in workflow["jobs"]["changes"]["steps"]
        if step.get("id") == "filter"
    )
    diff = tmp_path / "diff"
    diff.write_text("\n".join(files))
    git = tmp_path / "git"
    git.write_text(
        '#!/bin/sh\ncase "$1" in\n'
        "  cat-file) exit 0;;\n"
        '  diff) cat "$CI_TEST_DIFF";;\n'
        "  *) exit 2;;\nesac\n"
    )
    git.chmod(0o755)
    output = tmp_path / "output"
    # subprocess-boundary: execute the actual Bash pipeline and its SIGPIPE behavior.
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
            "EVENT_NAME": "pull_request",
            "PR_BASE_SHA": "a" * 40,
            "PUSH_BEFORE": "",
            "GITHUB_REF": "refs/pull/1/merge",
            "GITHUB_OUTPUT": str(output),
            "CI_TEST_DIFF": str(diff),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    actual = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert actual == {
        **{
            name: str(name in enabled).lower()
            for name in ("backend", "sidecar", "models_gpu", "frontend", "infra")
        },
        "pytest_target": target,
    }
