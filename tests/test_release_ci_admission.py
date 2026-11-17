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


GATES = (
    ("release-public-artifacts.yml", "build", "platform_proof"),
    ("desktop-release.yml", "admission", "platform-proof"),
)


def platform_proof(tmp_path, name, job, step_id, *, runs, jobs, bypass=False):
    workflow = yaml.safe_load(
        # rule19: execute the workflow's shell step with a fake GitHub transport.
        (Path(__file__).resolve().parents[1] / ".github/workflows" / name).read_text()
    )
    script = next(
        step["run"]
        for step in workflow["jobs"][job]["steps"]
        if step.get("id") == step_id
    )
    (tmp_path / "runs.json").write_text(json.dumps({"workflow_runs": runs}))
    for run_id, run_jobs in jobs.items():
        (tmp_path / f"jobs-{run_id}.json").write_text(json.dumps({"jobs": run_jobs}))
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do case "$arg" in\n'
        '  */workflows/nightly-platform.yml/runs) cat "$STUB/runs.json"; exit 0 ;;\n'
        '  */actions/runs/*/jobs) id="${arg#*/actions/runs/}"; cat "$STUB/jobs-${id%/jobs}.json"; exit 0 ;;\n'
        "esac; done\n"
        "exit 1\n"
    )
    gh.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
            "STUB": str(tmp_path),
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            "GITHUB_REPOSITORY": "example/test",
            "TARGET": "a" * 40,
            "EMERGENCY_CI_BYPASS": str(bypass).lower(),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0, result.stdout + result.stderr


def proof_job(conclusion):
    return {
        "name": "platform-proofs",
        "conclusion": conclusion,
        "html_url": f"https://example.invalid/proof/{conclusion}",
    }


def night(run_id, run_number, event="schedule"):
    return {"id": run_id, "run_number": run_number, "event": event}


@pytest.mark.parametrize(("name", "job", "step_id"), GATES)
@pytest.mark.parametrize(
    ("runs", "jobs", "accepted"),
    [
        ([], {}, False),
        ([night(1, 1)], {1: [proof_job("success")]}, True),
        ([night(1, 1, "workflow_dispatch")], {1: [proof_job("success")]}, True),
        ([night(1, 1)], {1: [proof_job("failure")]}, False),
        # A newer failed proof of the same commit outranks an older green one.
        (
            [night(1, 1), night(2, 2)],
            {1: [proof_job("success")], 2: [proof_job("failure")]},
            False,
        ),
        # A skipped night and an unfinished proof are not verdicts.
        (
            [night(1, 1), night(2, 2), night(3, 3)],
            {
                1: [proof_job("success")],
                2: [proof_job("skipped")],
                3: [proof_job(None)],
            },
            True,
        ),
        ([night(1, 1)], {1: [{**proof_job("success"), "name": "decide"}]}, False),
        ([night(1, 1, "pull_request")], {1: [proof_job("success")]}, False),
    ],
    ids=[
        "none",
        "green-night",
        "green-dispatch",
        "red-night",
        "newer-red",
        "skipped-and-unfinished",
        "no-proof-job",
        "wrong-event",
    ],
)
def test_release_requires_a_platform_proof_of_the_exact_commit(
    tmp_path, name, job, step_id, runs, jobs, accepted
):
    ok, log = platform_proof(tmp_path, name, job, step_id, runs=runs, jobs=jobs)
    assert ok is accepted, log
    if accepted:
        assert (tmp_path / "output").read_text() == (
            "url=https://example.invalid/proof/success\n"
        )


def test_public_release_emergency_bypass_covers_the_platform_proof(tmp_path):
    ok, log = platform_proof(tmp_path, *GATES[0], runs=[], jobs={}, bypass=True)
    assert ok, log
