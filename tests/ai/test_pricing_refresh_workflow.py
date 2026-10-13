"""Behavioral contract for the scheduled pricing-refresh workflow."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "refresh-pricing.yml"
PRICING_PATH = Path("src/frisket/ai/llm/pricing_data.json")
MODEL_PATH = Path("src/frisket/ai/llm/model_catalog.json")
GENERATED_PATH = Path("web/src/actions/modelCatalog.generated.ts")
OCR_GENERATED_PATH = Path("web/src/actions/engineCatalog.generated.ts")
BEFORE = {
    "source": "https://example.invalid/prices.json",
    "updated": "2026-08-02",
    "text": {"provider/model": [1.0, 2.0]},
    "audio": {},
}
MODEL_BEFORE = {
    "schema_version": "frisket.model_catalog.v1",
    "updated": "2026-08-02",
    "providers": {
        "openai": [{"id": "gpt-5", "label": "GPT 5", "vision": True}],
        "ollama": [],
    },
}
REFRESH_DAY = "2026-08-09"
REFRESH_BRANCH = f"pricing-refresh-{REFRESH_DAY.replace('-', '')}"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _workflow_steps() -> tuple[dict, dict]:
    # rule19: the test executes these exact workflow shell artifacts below.
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = {
        step["name"]: step
        for step in workflow["jobs"]["refresh"]["steps"]
        if "name" in step
    }
    refresh = steps["Refresh price table"]
    open_pr = steps["Open PR"]
    assert open_pr["if"] == "steps.refresh.outputs.changed == 'true'"
    return refresh, open_pr


def _install_fake_commands(fake_bin: Path) -> None:
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        """#!/usr/bin/env python3
import os
import shutil
import sys

if "scripts/dev/update_pricing.py" in sys.argv:
    shutil.copyfile(
        os.environ["PRICING_AFTER"],
        "src/frisket/ai/llm/pricing_data.json",
    )
    shutil.copyfile(
        os.environ["MODEL_AFTER"],
        "src/frisket/ai/llm/model_catalog.json",
    )
elif "scripts/dev/sync_model_catalog.py" in sys.argv:
    shutil.copyfile(
        os.environ["GENERATED_AFTER"],
        "web/src/actions/modelCatalog.generated.ts",
    )
elif "scripts/dev/sync_ocr_engine_catalog.py" in sys.argv:
    expected = [
        "run",
        "--frozen",
        "--no-dev",
        "python",
        "scripts/dev/sync_ocr_engine_catalog.py",
    ]
    if sys.argv[1:] != expected:
        raise SystemExit(f"unexpected OCR sync invocation: {sys.argv[1:]}")
    shutil.copyfile(
        os.environ["OCR_GENERATED_AFTER"],
        "web/src/actions/engineCatalog.generated.ts",
    )
elif "scripts/dev/pricing_delta_report.py" in sys.argv:
    print("synthetic pricing delta")
else:
    raise SystemExit(f"unexpected uv invocation: {sys.argv[1:]}")
""",
        encoding="utf-8",
    )
    uv.chmod(0o755)

    gh = fake_bin / "gh"
    gh.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
attempts = Path(os.environ["GH_ATTEMPTS"])
state_path = Path(os.environ["GH_STATE"])
with attempts.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")

state = json.loads(state_path.read_text()) if state_path.exists() else {"numbers": []}
if args[:2] == ["pr", "list"]:
    list_exit = int(os.environ.get("GH_PR_LIST_EXIT", "0"))
    if list_exit:
        raise SystemExit(list_exit)
    for number in state["numbers"]:
        print(number)
elif args[:2] == ["pr", "create"]:
    if state["numbers"]:
        raise SystemExit("duplicate PR creation")
    state_path.write_text(json.dumps({"numbers": [1]}) + "\\n")
else:
    raise SystemExit(f"unexpected gh invocation: {args}")
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)

    git = fake_bin / "git"
    git.write_text(
        """#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
result = subprocess.run([os.environ["REAL_GIT"], *args], check=False)
if args[:2] == ["ls-remote", "--heads"] and os.environ.get("INJECT_RACE_BRANCH"):
    marker = Path(os.environ["RACE_MARKER"])
    if not marker.exists():
        subprocess.run(
            [
                os.environ["REAL_GIT"],
                f"--git-dir={os.environ['RACE_REMOTE']}",
                "update-ref",
                f"refs/heads/{os.environ['RACE_BRANCH']}",
                "refs/heads/main",
            ],
            check=True,
        )
        marker.touch()
raise SystemExit(result.returncode)
""",
        encoding="utf-8",
    )
    git.chmod(0o755)

    date = fake_bin / "date"
    date.write_text(
        """#!/usr/bin/env python3
import os
import sys

if sys.argv[1:] == ["-u", "+%Y-%m-%d"]:
    print(os.environ["PRICING_REFRESH_DAY"])
else:
    raise SystemExit(f"unexpected date invocation: {sys.argv[1:]}")
""",
        encoding="utf-8",
    )
    date.chmod(0o755)


def _run_workflow(
    tmp_path: Path,
    after: dict,
    *,
    model_after: dict | None = None,
    ocr_generated_after: str | None = None,
    reruns: int = 1,
    existing_branch: bool = False,
    initial_pr_numbers: tuple[int, ...] = (),
    gh_pr_list_exit: int = 0,
    inject_race_branch: bool = False,
    expect_open_pr_success: bool = True,
) -> dict:
    refresh_step, open_pr_step = _workflow_steps()
    repo = tmp_path / "checkout"
    remote = tmp_path / "origin.git"
    fake_bin = tmp_path / "bin"
    workflow_tmp = repo / "workflow-tmp"
    pricing = repo / PRICING_PATH
    model_catalog = repo / MODEL_PATH
    generated = repo / GENERATED_PATH
    ocr_generated = repo / OCR_GENERATED_PATH
    output = workflow_tmp / "github-output"
    gh_attempts = workflow_tmp / "gh-attempts.jsonl"
    gh_state = workflow_tmp / "gh-state.json"
    after_path = tmp_path / "after.json"
    model_after_path = tmp_path / "model-after.json"
    generated_after_path = tmp_path / "generated-after.ts"
    ocr_generated_after_path = tmp_path / "ocr-generated-after.ts"

    pricing.parent.mkdir(parents=True)
    generated.parent.mkdir(parents=True)
    workflow_tmp.mkdir()
    pricing.write_text(json.dumps(BEFORE, indent=2) + "\n", encoding="utf-8")
    model_catalog.write_text(
        json.dumps(MODEL_BEFORE, indent=2) + "\n", encoding="utf-8"
    )
    generated.write_text("generated before\n", encoding="utf-8")
    ocr_generated.write_text("ocr generated before\n", encoding="utf-8")
    after_path.write_text(json.dumps(after, indent=2) + "\n", encoding="utf-8")
    effective_model_after = model_after or {
        **MODEL_BEFORE,
        "updated": after.get("updated", MODEL_BEFORE["updated"]),
    }
    model_after_path.write_text(
        json.dumps(effective_model_after, indent=2) + "\n", encoding="utf-8"
    )
    generated_after_path.write_text(
        "generated after\n" if model_after is not None else "generated before\n",
        encoding="utf-8",
    )
    ocr_generated_after_path.write_text(
        ocr_generated_after
        if ocr_generated_after is not None
        else (
            "ocr generated after\n"
            if model_after is not None
            else "ocr generated before\n"
        ),
        encoding="utf-8",
    )
    _install_fake_commands(fake_bin)

    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Pricing workflow test")
    _git(repo, "config", "user.email", "pricing-test@example.invalid")
    _git(
        repo,
        "add",
        str(PRICING_PATH),
        str(MODEL_PATH),
        str(GENERATED_PATH),
        str(OCR_GENERATED_PATH),
    )
    _git(repo, "commit", "-m", "fixture baseline")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")

    existing_tip = None
    if existing_branch:
        branch = REFRESH_BRANCH
        _git(repo, "switch", "-c", branch)
        pricing.write_text(
            json.dumps(
                {
                    **BEFORE,
                    "updated": REFRESH_DAY,
                    "text": {"provider/model": [1.0, 2.5]},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        model_catalog.write_text(
            json.dumps({**MODEL_BEFORE, "updated": REFRESH_DAY}, indent=2) + "\n",
            encoding="utf-8",
        )
        _git(repo, "add", str(PRICING_PATH), str(MODEL_PATH))
        _git(repo, "commit", "-m", "existing dated pricing branch")
        _git(repo, "push", "-u", "origin", branch)
        existing_tip = _git(repo, "rev-parse", "HEAD")
        _git(repo, "switch", "main")

    if initial_pr_numbers:
        gh_state.write_text(
            json.dumps({"numbers": list(initial_pr_numbers)}) + "\n",
            encoding="utf-8",
        )

    real_git = shutil.which("git")
    assert real_git is not None
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output),
        "GH_ATTEMPTS": str(gh_attempts),
        "GH_STATE": str(gh_state),
        "GH_TOKEN": "test-only-token",
        "GH_PR_LIST_EXIT": str(gh_pr_list_exit),
        "REAL_GIT": real_git,
        "INJECT_RACE_BRANCH": "1" if inject_race_branch else "",
        "RACE_REMOTE": str(remote),
        "RACE_BRANCH": REFRESH_BRANCH,
        "RACE_MARKER": str(workflow_tmp / "race-injected"),
        "PRICING_AFTER": str(after_path),
        "MODEL_AFTER": str(model_after_path),
        "GENERATED_AFTER": str(generated_after_path),
        "OCR_GENERATED_AFTER": str(ocr_generated_after_path),
        "PRICING_REFRESH_DAY": REFRESH_DAY,
    }
    refresh_script = (
        refresh_step["run"]
        .replace("/tmp/pricing_before.json", "workflow-tmp/pricing-before.json")
        .replace(
            "/tmp/model_catalog_before.json",
            "workflow-tmp/model-catalog-before.json",
        )
        .replace(
            "/tmp/model_catalog_generated_before.ts",
            "workflow-tmp/model-catalog-generated-before.ts",
        )
        .replace(
            "/tmp/ocr_engine_catalog_generated_before.ts",
            "workflow-tmp/ocr-engine-catalog-generated-before.ts",
        )
        .replace("/tmp/pricing_delta.md", "workflow-tmp/pricing-delta.md")
    )
    outputs: dict[str, str] = {}
    open_pr_returncodes: list[int] = []
    for _ in range(reruns):
        _git(repo, "switch", "main")
        _git(repo, "reset", "--hard", "origin/main")
        output.unlink(missing_ok=True)
        refresh_run = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", refresh_script],
            cwd=repo,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert refresh_run.returncode == 0, refresh_run.stderr
        # rule19: test-owned runner output, not repository source.
        output_lines = output.read_text(encoding="utf-8").splitlines()
        outputs = dict(line.split("=", 1) for line in output_lines)

        if outputs["changed"] == "true":
            open_pr_script = open_pr_step["run"].replace(
                "/tmp/pricing_delta.md", "workflow-tmp/pricing-delta.md"
            )
            open_pr_script = (
                open_pr_script.replace(
                    "/tmp/pricing_after.json", "workflow-tmp/pricing-after.json"
                )
                .replace(
                    "/tmp/model_catalog_after.json",
                    "workflow-tmp/model-catalog-after.json",
                )
                .replace(
                    "/tmp/model_catalog_generated_after.ts",
                    "workflow-tmp/model-catalog-generated-after.ts",
                )
                .replace(
                    "/tmp/ocr_engine_catalog_generated_after.ts",
                    "workflow-tmp/ocr-engine-catalog-generated-after.ts",
                )
            )
            open_pr_run = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", open_pr_script],
                cwd=repo,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            open_pr_returncodes.append(open_pr_run.returncode)
            if expect_open_pr_success:
                assert open_pr_run.returncode == 0, open_pr_run.stderr

    remote_branches = subprocess.run(
        [
            "git",
            "--git-dir",
            str(remote),
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    remote_refresh_branches = [
        branch for branch in remote_branches if branch.startswith("pricing-refresh-")
    ]
    pr_calls = (
        # rule19: test-owned fake-gh journal, not repository source.
        [json.loads(line) for line in gh_attempts.read_text().splitlines()]
        if gh_attempts.exists()
        else []
    )
    remote_main = _git(remote, "rev-parse", "refs/heads/main")
    remote_tip = (
        _git(remote, "rev-parse", f"refs/heads/{remote_refresh_branches[0]}")
        if remote_refresh_branches
        else None
    )
    # rule19: test-owned fake-gh state, not repository source.
    state = json.loads(gh_state.read_text()) if gh_state.exists() else {"numbers": []}
    return {
        "changed": outputs["changed"],
        "branch": _git(repo, "branch", "--show-current"),
        "existing_tip": existing_tip,
        "remote_tip": remote_tip,
        "remote_branch_parent": (
            _git(remote, "rev-parse", f"{remote_tip}^")
            if remote_tip and remote_tip != remote_main
            else None
        ),
        "remote_main": remote_main,
        "remote_main_committed": json.loads(
            _git(repo, "show", f"origin/main:{PRICING_PATH}")
        ),
        "remote_branch_committed": (
            json.loads(_git(repo, "show", f"{remote_tip}:{PRICING_PATH}"))
            if remote_tip
            else None
        ),
        "remote_refresh_branches": remote_refresh_branches,
        "pr_calls": pr_calls,
        "pr_numbers": state["numbers"],
        "open_pr_returncodes": open_pr_returncodes,
    }


def test_timestamp_only_regeneration_is_a_complete_noop(tmp_path: Path) -> None:
    after = {**BEFORE, "updated": "2026-08-09"}

    assert _run_workflow(
        tmp_path,
        after,
        ocr_generated_after="transient OCR regeneration\n",
    ) == {
        "changed": "false",
        "branch": "main",
        "existing_tip": None,
        "remote_tip": None,
        "remote_branch_parent": None,
        "remote_main": _git(tmp_path / "origin.git", "rev-parse", "refs/heads/main"),
        "remote_main_committed": BEFORE,
        "remote_branch_committed": None,
        "remote_refresh_branches": [],
        "pr_calls": [],
        "pr_numbers": [],
        "open_pr_returncodes": [],
    }
    # rule19: inspect the artifact restored by executing the workflow shell.
    assert (tmp_path / "checkout" / OCR_GENERATED_PATH).read_text() == (
        "ocr generated before\n"
    )


def test_rate_change_keeps_generated_update_and_pr_path(tmp_path: Path) -> None:
    after = {
        **BEFORE,
        "updated": "2026-08-09",
        "text": {"provider/model": [1.0, 3.0]},
    }

    result = _run_workflow(tmp_path, after)

    assert result["changed"] == "true"
    assert result["branch"].startswith("pricing-refresh-")
    assert result["existing_tip"] is None
    assert result["remote_tip"] is not None
    assert result["remote_branch_parent"] == _git(
        tmp_path / "checkout", "rev-parse", "origin/main"
    )
    assert result["remote_main_committed"] == BEFORE
    assert result["remote_branch_committed"] == after
    assert result["remote_refresh_branches"] == [result["branch"]]
    assert [call[:2] for call in result["pr_calls"]] == [
        ["pr", "list"],
        ["pr", "create"],
    ]
    assert result["pr_numbers"] == [1]
    assert result["open_pr_returncodes"] == [0]


def test_model_only_change_uses_the_same_reviewed_pr_path(tmp_path: Path) -> None:
    after = {**BEFORE, "updated": "2026-08-09"}
    model_after = {
        **MODEL_BEFORE,
        "updated": "2026-08-09",
        "providers": {
            **MODEL_BEFORE["providers"],
            "openai": [
                *MODEL_BEFORE["providers"]["openai"],
                {"id": "gpt-6.1-sol", "label": "GPT 6.1 Sol", "vision": True},
            ],
        },
    }

    result = _run_workflow(tmp_path, after, model_after=model_after)

    assert result["changed"] == "true"
    assert result["remote_refresh_branches"] == [REFRESH_BRANCH]
    assert [call[:2] for call in result["pr_calls"]] == [
        ["pr", "list"],
        ["pr", "create"],
    ]
    assert (
        json.loads(
            _git(tmp_path / "checkout", "show", f"{result['remote_tip']}:{MODEL_PATH}")
        )
        == model_after
    )
    assert (
        _git(
            tmp_path / "checkout",
            "show",
            f"{result['remote_tip']}:{GENERATED_PATH}",
        )
        == "generated after"
    )
    assert (
        _git(
            tmp_path / "checkout",
            "show",
            f"{result['remote_tip']}:{OCR_GENERATED_PATH}",
        )
        == "ocr generated after"
    )


def test_stale_orphan_branch_refuses_a_mismatched_ocr_fallback(
    tmp_path: Path,
) -> None:
    after = {
        **BEFORE,
        "updated": "2026-08-09",
        "text": {"provider/model": [1.0, 2.5]},
    }

    result = _run_workflow(
        tmp_path,
        after,
        ocr_generated_after="ocr generated after\n",
        existing_branch=True,
        expect_open_pr_success=False,
    )

    assert result["remote_tip"] == result["existing_tip"]
    assert [call[:2] for call in result["pr_calls"]] == [["pr", "list"]]
    assert result["pr_numbers"] == []
    assert result["open_pr_returncodes"] == [1]


def test_same_day_rerun_leaves_one_branch_and_one_pr_unchanged(tmp_path: Path) -> None:
    after = {
        **BEFORE,
        "updated": "2026-08-09",
        "text": {"provider/model": [1.0, 3.0]},
    }

    result = _run_workflow(tmp_path, after, reruns=2)

    assert result["branch"] == "main"
    assert result["remote_main_committed"] == BEFORE
    assert result["remote_branch_committed"] == after
    assert result["remote_refresh_branches"] == [REFRESH_BRANCH]
    assert [call[:2] for call in result["pr_calls"]] == [
        ["pr", "list"],
        ["pr", "create"],
        ["pr", "list"],
    ]
    assert result["pr_numbers"] == [1]
    assert result["open_pr_returncodes"] == [0, 0]


def test_stale_orphan_branch_refuses_a_mismatched_pr_body(tmp_path: Path) -> None:
    after = {
        **BEFORE,
        "updated": "2026-08-09",
        "text": {"provider/model": [1.0, 3.0]},
    }

    result = _run_workflow(
        tmp_path,
        after,
        existing_branch=True,
        expect_open_pr_success=False,
    )

    assert result["branch"] == "main"
    assert result["remote_tip"] == result["existing_tip"]
    assert result["remote_main_committed"] == BEFORE
    assert result["remote_branch_committed"]["text"] == {"provider/model": [1.0, 2.5]}
    assert [call[:2] for call in result["pr_calls"]] == [["pr", "list"]]
    assert result["pr_numbers"] == []
    assert result["open_pr_returncodes"] == [1]


def test_matching_orphan_branch_gets_pr_without_rewrite(tmp_path: Path) -> None:
    after = {
        **BEFORE,
        "updated": "2026-08-09",
        "text": {"provider/model": [1.0, 2.5]},
    }

    result = _run_workflow(tmp_path, after, existing_branch=True)

    assert result["remote_tip"] == result["existing_tip"]
    assert [call[:2] for call in result["pr_calls"]] == [
        ["pr", "list"],
        ["pr", "create"],
    ]
    assert result["pr_numbers"] == [1]


def test_existing_branch_and_pr_are_an_intentional_noop(tmp_path: Path) -> None:
    after = {
        **BEFORE,
        "updated": "2026-08-09",
        "text": {"provider/model": [1.0, 3.0]},
    }

    result = _run_workflow(
        tmp_path,
        after,
        existing_branch=True,
        initial_pr_numbers=(17,),
    )

    assert result["branch"] == "main"
    assert result["remote_tip"] == result["existing_tip"]
    assert result["remote_main_committed"] == BEFORE
    assert [call[:2] for call in result["pr_calls"]] == [["pr", "list"]]
    assert result["pr_numbers"] == [17]


def test_failed_pr_enumeration_refuses_before_branch_mutation(tmp_path: Path) -> None:
    after = {
        **BEFORE,
        "updated": "2026-08-09",
        "text": {"provider/model": [1.0, 3.0]},
    }

    result = _run_workflow(
        tmp_path,
        after,
        gh_pr_list_exit=7,
        expect_open_pr_success=False,
    )

    assert result["branch"] == "main"
    assert result["remote_main_committed"] == BEFORE
    assert result["remote_refresh_branches"] == []
    assert [call[:2] for call in result["pr_calls"]] == [["pr", "list"]]
    assert result["pr_numbers"] == []
    assert result["open_pr_returncodes"] == [1]


def test_multiple_prs_refuse_without_rewriting_existing_branch(tmp_path: Path) -> None:
    after = {
        **BEFORE,
        "updated": "2026-08-09",
        "text": {"provider/model": [1.0, 3.0]},
    }

    result = _run_workflow(
        tmp_path,
        after,
        existing_branch=True,
        initial_pr_numbers=(17, 18),
        expect_open_pr_success=False,
    )

    assert result["branch"] == "main"
    assert result["remote_tip"] == result["existing_tip"]
    assert result["remote_main_committed"] == BEFORE
    assert [call[:2] for call in result["pr_calls"]] == [["pr", "list"]]
    assert result["pr_numbers"] == [17, 18]
    assert result["open_pr_returncodes"] == [1]


def test_open_pr_without_remote_branch_refuses_before_mutation(tmp_path: Path) -> None:
    after = {
        **BEFORE,
        "updated": "2026-08-09",
        "text": {"provider/model": [1.0, 3.0]},
    }

    result = _run_workflow(
        tmp_path,
        after,
        initial_pr_numbers=(17,),
        expect_open_pr_success=False,
    )

    assert result["branch"] == "main"
    assert result["remote_main_committed"] == BEFORE
    assert result["remote_refresh_branches"] == []
    assert [call[:2] for call in result["pr_calls"]] == [["pr", "list"]]
    assert result["pr_numbers"] == [17]
    assert result["open_pr_returncodes"] == [1]


def test_branch_created_after_discovery_is_never_advanced(tmp_path: Path) -> None:
    after = {
        **BEFORE,
        "updated": "2026-08-09",
        "text": {"provider/model": [1.0, 3.0]},
    }

    result = _run_workflow(
        tmp_path,
        after,
        inject_race_branch=True,
        expect_open_pr_success=False,
    )

    assert result["remote_refresh_branches"] == [REFRESH_BRANCH]
    assert result["remote_tip"] == result["remote_main"]
    assert result["remote_branch_parent"] is None
    assert result["remote_main_committed"] == BEFORE
    assert result["remote_branch_committed"] == BEFORE
    assert [call[:2] for call in result["pr_calls"]] == [["pr", "list"]]
    assert result["pr_numbers"] == []
    assert result["open_pr_returncodes"] == [1]
