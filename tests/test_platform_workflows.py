"""Execute the platform workflows' change filters and nightly wiring offline."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
DESKTOP = ("desktop-macos.yml", "desktop-windows.yml")
PORTABILITY = ("macos-portability.yml", "windows-portability.yml")
PLATFORM = DESKTOP + PORTABILITY


def workflow(name):
    return yaml.load(
        # rule19: executes workflow shell steps and checks the nightly job graph.
        (WORKFLOWS / name).read_text(),
        Loader=yaml.BaseLoader,
    )


def change_filter(name):
    return workflow(name)["jobs"]["changes"]["steps"][-1]["run"]


def run_filter(tmp_path, name, *, files=(), patch="", added=(), **env):
    """Run a workflow's change filter with Git replaced by a fixed diff."""
    (tmp_path / "files").write_text("\n".join(files))
    (tmp_path / "patch").write_text(patch)
    (tmp_path / "added").write_text("\n".join(added))
    git = tmp_path / "git"
    git.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  cat-file*) exit 0 ;;\n"
        '  *-U0*) cat "$STUB/patch" ;;\n'
        '  *--diff-filter=ADR*) cat "$STUB/added" ;;\n'
        '  diff*) cat "$STUB/files" ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    git.chmod(0o755)
    output = tmp_path / "output"
    output.write_text("")
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", change_filter(name)],
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "GITHUB_OUTPUT": str(output),
            "STUB": str(tmp_path),
            "BASE_SHA": "b" * 40,
            "QUEUE_BASE_SHA": "q" * 40,
            **env,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


@pytest.mark.parametrize("name", PLATFORM)
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize(
    ("files", "patch", "relevant", "reason"),
    [
        (["uv.lock"], "", "true", "changes an input"),
        (["src/frisket/runtime/supervisor.py"], "", "true", "changes an input"),
        (["src/frisket/engine/sandbox/fence.py"], "", "true", "changes an input"),
        (
            ["src/frisket/ai/llm/cache.py"],
            '+    if sys.platform == "win32":\n',
            "true",
            "platform-specific Python",
        ),
        (
            ["src/frisket/plugins/runner.py"],
            "-    return subprocess.Popen(argv)\n",
            "true",
            "platform-specific Python",
        ),
        (
            ["src/frisket/server/routes/rows.py"],
            "+    rows = query(rows)\n",
            "false",
            "deferred to the nightly platform run",
        ),
        (["docs/example.md", "CONTRIBUTING.md"], "", "false", "not affected"),
    ],
    ids=["lock", "runtime", "sandbox", "os-branch", "subprocess", "app-code", "docs"],
)
def test_pr_and_queue_prove_only_own_inputs(
    tmp_path, name, event, files, patch, relevant, reason
):
    out = run_filter(tmp_path, name, files=files, patch=patch, EVENT_NAME=event)
    assert out["relevant"] == relevant
    assert reason in out["reason"]
    assert ("ci:full" in out["reason"]) is (
        relevant == "false" and "deferred" in reason and event == "pull_request"
    )


@pytest.mark.parametrize("name", PLATFORM)
def test_full_label_proves_every_platform_regardless_of_the_diff(tmp_path, name):
    out = run_filter(
        tmp_path,
        name,
        files=["docs/example.md"],
        EVENT_NAME="pull_request",
        FULL_LABEL="true",
    )
    assert out["relevant"] == "true"


@pytest.mark.parametrize("name", DESKTOP)
def test_nightly_call_proves_the_installed_app(tmp_path, name):
    out = run_filter(
        tmp_path, name, files=["docs/example.md"], EVENT_NAME="schedule", NIGHTLY="true"
    )
    assert out["relevant"] == "true"
    assert "nightly" in out["reason"]


@pytest.mark.parametrize("name", DESKTOP)
def test_a_removed_bundled_resource_proves_the_installed_app(tmp_path, name):
    removed = "src/frisket/authoring/bundled_plugins/frisket.ftm/plugin.json"
    out = run_filter(
        tmp_path, name, files=[removed], added=[removed], EVENT_NAME="merge_group"
    )
    assert out["relevant"] == "true"
    assert removed in out["reason"]


@pytest.mark.parametrize("name", PORTABILITY)
@pytest.mark.parametrize(
    ("event", "tested", "relevant"),
    [
        ("push", "true", "false"),
        ("push", "false", "true"),
        ("push", "", "true"),
        ("workflow_dispatch", "", "true"),
        ("schedule", "", "true"),
    ],
)
def test_portability_runs_outside_the_queue_unless_it_tested_the_commit(
    tmp_path, name, event, tested, relevant
):
    out = run_filter(
        tmp_path, name, files=["docs/example.md"], EVENT_NAME=event, QUEUE_TESTED=tested
    )
    assert out["relevant"] == relevant
    assert out.get("managed_python", relevant) == relevant


@pytest.mark.parametrize(
    ("files", "managed"),
    [
        (["src/frisket/server/routes/rows.py"], "true"),
        (["tests/server/test_rows.py"], "true"),
        (["uv.lock"], "true"),
        (["web/src/app.tsx"], "false"),
        (["docs/example.md"], "false"),
    ],
)
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_managed_python_follows_the_package_tests_and_lock(
    tmp_path, files, managed, event
):
    out = run_filter(tmp_path, "macos-portability.yml", files=files, EVENT_NAME=event)
    assert out["managed_python"] == managed


def test_the_four_filters_share_one_platform_definition():
    for variable in ("platform_code", "platform_lines"):
        values = {
            re.search(rf"^\s*{variable}='([^']*)'$", change_filter(name), re.M).group(1)
            for name in PLATFORM
        }
        assert len(values) == 1, variable


@pytest.mark.parametrize(
    ("name", "gate"),
    [
        ("desktop-macos.yml", "desktop-macos-ci"),
        ("desktop-windows.yml", "desktop-windows-ci"),
        ("macos-portability.yml", "macos-portability"),
        ("windows-portability.yml", "windows-portability"),
    ],
)
def test_required_checks_report_why_their_jobs_skipped(name, gate):
    job = workflow(name)["jobs"][gate]
    assert "always()" in job["if"]
    assert job["steps"][0]["env"]["REASON"] == "${{ needs.changes.outputs.reason }}"


def test_nightly_proves_all_four_platforms_and_only_its_report_writes_issues():
    config = workflow("nightly-platform.yml")
    assert set(config["on"]) == {"schedule", "workflow_dispatch"}
    assert config["permissions"] == {"contents": "read"}
    jobs = config["jobs"]
    calls = {
        job["uses"].rsplit("/", 1)[-1]: job for job in jobs.values() if "uses" in job
    }
    assert set(calls) == set(PLATFORM)
    for name in DESKTOP:
        assert calls[name]["with"] == {"nightly": "true"}
        assert "workflow_call" in workflow(name)["on"]
    for name in PORTABILITY:
        assert "workflow_call" in workflow(name)["on"]
    gate = jobs["platform-proofs"]
    assert set(gate["needs"]) == {"decide", *(Path(n).stem for n in PLATFORM)}
    writers = [
        key
        for key, job in jobs.items()
        if (job.get("permissions") or {}).get("issues") == "write"
    ]
    assert writers == ["report"]


@pytest.mark.parametrize(
    ("event", "last_green", "run"),
    [
        ("schedule", "a" * 40, "false"),
        ("schedule", "c" * 40, "true"),
        ("schedule", None, "true"),
        ("workflow_dispatch", "a" * 40, "true"),
    ],
)
def test_nightly_skips_only_a_scheduled_night_on_an_already_green_head(
    tmp_path, event, last_green, run
):
    script = workflow("nightly-platform.yml")["jobs"]["decide"]["steps"][0]["run"]
    runs = (
        "{}"
        if last_green is None
        else '{"workflow_runs":[{"event":"schedule","head_sha":"%s"}]}' % last_green
    )
    gh = tmp_path / "gh"
    gh.write_text(f"#!/bin/sh\nprintf '%s' '{runs}'\n")
    gh.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-c", script],
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
    assert out["run"] == run
    assert out["last_green"] == (last_green or "")


PATH_GATED = {
    "local-model-runtime.yml": (
        "local-model-runtime",
        {"changes", "retrieval", "classifiers", "docling"},
        "sidecar/retrieval-runtime/pyproject.toml",
    ),
    "retrieval-native.yml": (
        "retrieval-native",
        {"changes", "wheel"},
        "vendor/sqlite-vec1/vec1.c",
    ),
}


@pytest.mark.parametrize("name", PATH_GATED)
def test_path_gated_proofs_always_report_one_check(name):
    gate, proofs, _ = PATH_GATED[name]
    config = workflow(name)
    assert config["on"]["pull_request"] == ""
    assert config["on"]["merge_group"] == {"types": ["checks_requested"]}
    jobs = config["jobs"]
    assert set(jobs) == proofs | {gate}
    for job in proofs - {"changes"}:
        assert jobs[job]["needs"] == "changes"
    assert jobs[gate]["if"] == "${{ always() }}"
    assert set(jobs[gate]["needs"]) == proofs
    assert jobs[gate]["steps"][0]["env"]["REASON"] == (
        "${{ needs.changes.outputs.reason }}"
    )


@pytest.mark.parametrize("name", PATH_GATED)
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_path_gated_proofs_run_only_when_their_inputs_change(tmp_path, name, event):
    _, _, input_path = PATH_GATED[name]
    touched = run_filter(
        tmp_path, name, files=["docs/a.md", input_path], EVENT_NAME=event
    )
    assert touched["relevant"] == "true"
    assert input_path in touched["reason"]
    untouched = run_filter(tmp_path, name, files=["docs/a.md"], EVENT_NAME=event)
    assert untouched["relevant"] == "false"
    assert "not affected" in untouched["reason"]
    assert run_filter(tmp_path, name, EVENT_NAME="workflow_dispatch")["relevant"] == (
        "true"
    )
