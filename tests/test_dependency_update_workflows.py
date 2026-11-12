"""Exercise merge policy and failure propagation without GitHub writes."""

from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    # rule19: repository workflow files are the contract under test.
    return yaml.load(
        (ROOT / ".github" / "workflows" / name).read_text(), Loader=yaml.BaseLoader
    )


def run_step(tmp_path, script, **values):
    for command in ("gh", "git"):
        stub = tmp_path / command
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$FILES"\n')
        stub.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "GITHUB_OUTPUT": str(output),
            **values,
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.returncode, output.read_text() if output.exists() else ""


@pytest.mark.parametrize(
    "name,gate",
    [
        ("ci.yml", "ci-pass"),
        ("desktop-macos.yml", "desktop-macos-ci"),
        ("desktop-windows.yml", "desktop-windows-ci"),
        ("docker.yml", "docker-ci"),
    ],
)
def test_merge_summaries_refuse_failure_and_cancellation(tmp_path, name, gate):
    job = workflow(name)["jobs"][gate]
    assert "always()" in job["if"]
    script = job["steps"][0]["run"]
    for status in ("failure", "cancelled", "pending", ""):
        code, _ = run_step(
            tmp_path,
            script,
            RESULTS='{"changes":{"result":"success"},"test":{"result":"'
            + status
            + '"}}',
        )
        assert code != 0
    code, _ = run_step(
        tmp_path,
        script,
        RESULTS='{"changes":{"result":"failure"},"test":{"result":"skipped"}}',
    )
    assert code != 0
    code, _ = run_step(
        tmp_path,
        script,
        RESULTS='{"changes":{"result":"success"},"test":{"result":"skipped"}}',
    )
    assert code == 0


def test_ci_summary_covers_every_job():
    jobs = workflow("ci.yml")["jobs"]
    assert set(jobs["ci-pass"]["needs"]) == set(jobs) - {"ci-pass"}


@pytest.mark.parametrize(
    "name", ["desktop-macos.yml", "desktop-windows.yml", "docker.yml"]
)
@pytest.mark.parametrize(
    "path,expected", [("uv.lock", "true"), ("docs/example.md", "false")]
)
def test_always_report_but_only_build_relevant_changes(tmp_path, name, path, expected):
    config = workflow(name)
    assert "paths" not in (config["on"]["pull_request"] or {})
    script = config["jobs"]["changes"]["steps"][-1]["run"]
    code, output = run_step(
        tmp_path, script, FILES=path, EVENT_NAME="pull_request", BASE_SHA="base"
    )
    assert code == 0
    assert output.strip() == f"relevant={expected}"


@pytest.mark.parametrize(
    "update,dependencies,files,eligible",
    [
        ("patch", "pypdf", "uv.lock", True),
        ("minor", "dompurify, undici", "web/package.json\nweb/package-lock.json", True),
        ("major", "pypdf", "uv.lock", False),
        ("patch", "", "uv.lock", False),
        ("patch", "undici, electron", "desktop/package-lock.json", False),
        ("minor", "torch", "uv.lock", False),
        ("patch", "pypdf", "sidecar/uv.lock", False),
        ("patch", "pypdf", "uv.lock\nsrc/frisket/cli.py", False),
        ("unknown", "pypdf", "uv.lock", False),
    ],
)
def test_automerge_policy(tmp_path, update, dependencies, files, eligible):
    steps = workflow("dependabot-automerge.yml")["jobs"]["enable"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "policy")
    code, output = run_step(
        tmp_path,
        script,
        FILES=files,
        GH_REPO="example/frisket",
        PR_NUMBER="1",
        UPDATE_TYPE=f"version-update:semver-{update}",
        DEPENDENCIES=dependencies,
    )
    assert code == 0
    assert ("eligible=true" in output) is eligible


def test_automerge_does_not_execute_candidate_code_or_bypass_protection():
    config = workflow("dependabot-automerge.yml")
    job = config["jobs"]["enable"]
    assert "DEPENDABOT_AUTOMERGE_ENABLED == 'true'" in job["if"]
    assert "dependabot[bot]" in job["if"]
    assert "head.repo.full_name == github.repository" in job["if"]
    assert not any("actions/checkout@" in step.get("uses", "") for step in job["steps"])
    merge = job["steps"][-1]["run"]
    assert "--match-head-commit" in merge and "--auto" in merge
    assert "--admin" not in merge
    assert config["permissions"] == {"contents": "read", "pull-requests": "read"}
