"""Execute the nightly browser-suite workflow's steps offline."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github/workflows/nightly-e2e.yml"


def workflow():
    return yaml.load(
        # rule19: executes the workflow's shell steps and checks its job graph.
        WORKFLOW.read_text(),
        Loader=yaml.BaseLoader,
    )


def step(job: str, needle: str) -> dict:
    return next(
        s for s in workflow()["jobs"][job]["steps"] if needle in s.get("run", "")
    )


def test_keyless_sharded_and_bounded():
    config = workflow()
    assert set(config["on"]) == {"schedule", "workflow_dispatch"}
    assert config["on"]["schedule"][0]["cron"] != "23 7 * * *"
    assert config["permissions"] == {"contents": "read"}
    # rule19: a secret reference anywhere in the file would reach a job.
    assert "secrets." not in WORKFLOW.read_text()
    jobs = config["jobs"]
    assert all("timeout-minutes" in job for job in jobs.values())
    shards = jobs["shard"]["strategy"]["matrix"]["shard"]
    assert shards == [str(n) for n in range(1, int(config["env"]["SHARDS"]) + 1)]
    suite = step("shard", "npx playwright test")
    assert suite["env"]["FRISKET_CACHE_MODE"] == "replay_strict"
    assert suite["env"]["FRISKET_E2E_TRACE"] == "retain-on-failure"
    traces = next(
        s
        for s in jobs["shard"]["steps"]
        if s.get("with", {}).get("name", "").startswith("e2e-traces-")
    )
    assert traces["uses"].startswith("actions/upload-artifact@")
    assert "always()" in traces["if"]
    assert traces["with"]["path"] == "web/test-results"
    assert traces["with"]["retention-days"] == "7"
    writers = [k for k, j in jobs.items() if (j.get("permissions") or {}).get("issues")]
    assert writers == ["verdict"]


@pytest.mark.parametrize(
    ("event", "last_green", "run"),
    [
        ("schedule", "a" * 40, "false"),
        ("schedule", "c" * 40, "true"),
        ("workflow_dispatch", "a" * 40, "true"),
    ],
)
def test_a_scheduled_night_on_an_already_green_head_is_skipped(
    tmp_path, event, last_green, run
):
    runs = json.dumps(
        {"workflow_runs": [{"event": "schedule", "head_sha": last_green}]}
    )
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!/bin/sh\ncase \"$*\" in *nightly-e2e.yml*) printf '%s' '{runs}' ;; *) exit 1 ;; esac\n"
    )
    gh.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-c", workflow()["jobs"]["decide"]["steps"][0]["run"]],
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "GITHUB_REPOSITORY": "example/test",
            "GITHUB_SHA": "a" * 40,
            "EVENT_NAME": event,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    out = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert out == {"run": run, "last_green": last_green}


def _run_suite_step(tmp_path, *, writes_report: bool, exit_code: int):
    npx = tmp_path / "npx"
    write = 'printf "{}" > "$PLAYWRIGHT_JSON_OUTPUT_NAME"\n' if writes_report else ""
    npx.write_text(f"#!/bin/sh\n{write}exit {exit_code}\n")
    npx.chmod(0o755)
    return subprocess.run(
        ["bash", "-e", "-c", step("shard", "npx playwright test")["run"]],
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "PLAYWRIGHT_JSON_OUTPUT_NAME": str(tmp_path / "report.json"),
            "SHARD": "2",
            "SHARDS": "4",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_a_shard_with_test_failures_passes_but_one_without_a_report_fails(tmp_path):
    assert _run_suite_step(tmp_path, writes_report=True, exit_code=1).returncode == 0
    (tmp_path / "report.json").unlink()
    failed = _run_suite_step(tmp_path, writes_report=False, exit_code=1)
    assert failed.returncode == 1
    assert "shard 2 wrote no report" in failed.stdout


def _failing_report() -> dict:
    spec = {
        "title": "new failure",
        "file": "picker.spec.ts",
        "tests": [
            {
                "status": "unexpected",
                "results": [{"status": "failed", "error": {"message": "Error: x"}}],
            }
        ],
    }
    return {"config": {"shard": None}, "errors": [], "suites": [{"specs": [spec]}]}


@pytest.mark.parametrize(("enforce", "code"), [("false", 0), ("true", 1)])
def test_the_enforcement_switch_decides_whether_new_failures_fail(
    tmp_path, enforce, code
):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "e2e-report-1.json").write_text(json.dumps(_failing_report()))
    python3 = tmp_path / "python3"
    # subprocess-boundary: the workflow step runs the ratchet as `python3`.
    python3.symlink_to(sys.executable)
    summary = tmp_path / "summary"
    result = subprocess.run(
        ["bash", "-e", "-c", step("verdict", "e2e_known_failures.py")["run"]],
        cwd=REPO,
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "REPORTS": str(reports),
            "VERDICT": str(tmp_path / "verdict.md"),
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_STEP_SUMMARY": str(summary),
            "E2E_KNOWN_FAILURES_ENFORCE": enforce,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == code, result.stdout + result.stderr
    assert "`picker.spec.ts › new failure`: Error: x" in summary.read_text()
