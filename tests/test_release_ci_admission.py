"""Execute the release admission step without GitHub or credentials."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml


@pytest.mark.parametrize(
    ("bypass", "event", "status", "conclusion", "accepted"),
    [
        (True, None, None, None, True),
        (False, None, None, None, False),
        (False, "push", "completed", "success", True),
        (False, "workflow_dispatch", "completed", "success", True),
        (False, "pull_request", "completed", "success", False),
        (False, "push", "completed", "failure", False),
        (False, "push", "in_progress", None, False),
    ],
)
def test_release_ci_admission(tmp_path, bypass, event, status, conclusion, accepted):
    # rule19: execute the workflow's shell step with a fake GitHub transport.
    workflow = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/release-public-artifacts.yml"
        ).read_text()
    )
    script = next(
        step["run"]
        for step in workflow["jobs"]["build"]["steps"]
        if step.get("id") == "full_ci"
    )
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s" "$RELEASE_TEST_RUNS"\n')
    gh.chmod(0o755)
    runs = (
        []
        if status is None
        else [
            {
                "event": event,
                "status": status,
                "conclusion": conclusion,
                "run_number": 1,
                "run_attempt": 1,
                "html_url": "https://example.invalid/ci/1",
            }
        ]
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            "GITHUB_REPOSITORY": "example/test",
            "TARGET": "a" * 40,
            "EMERGENCY_CI_BYPASS": str(bypass).lower(),
            "RELEASE_TEST_RUNS": json.dumps({"workflow_runs": runs}),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
