"""Runtime contract for the reusable pricing/model refresh workflow."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "refresh-catalog.yml"
PRICING_PATH = Path("src/frisket/ai/llm/pricing_data.json")
MODEL_PATH = Path("src/frisket/ai/llm/model_catalog.json")
GENERATED_PATH = Path("web/src/actions/modelCatalog.generated.ts")
OCR_GENERATED_PATH = Path("web/src/actions/engineCatalog.generated.ts")
REFRESH_DAY = "2026-08-09"

PRICING_BEFORE = {
    "sources": {"litellm": "https://example.invalid/prices.json"},
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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _workflow_steps() -> dict[str, dict]:
    # rule19: these shell artifacts are executed by the harness below.
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return {
        step["name"]: step
        for step in workflow["jobs"]["refresh"]["steps"]
        if "name" in step
    }


def _install_fake_commands(fake_bin: Path) -> None:
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        """#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["UV_ATTEMPTS"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")

if "scripts/dev/update_pricing.py" in args:
    mode = args[args.index("--mode") + 1]
    if mode == "pricing":
        shutil.copyfile(
            os.environ["PRICING_AFTER"],
            "src/frisket/ai/llm/pricing_data.json",
        )
    elif mode == "models" and not os.environ.get("MODELS_UPDATER_NOOP"):
        shutil.copyfile(
            os.environ["PRICING_AFTER"],
            "src/frisket/ai/llm/pricing_data.json",
        )
        shutil.copyfile(
            os.environ["MODEL_AFTER"],
            "src/frisket/ai/llm/model_catalog.json",
        )
    elif mode != "models":
        raise SystemExit(f"unsupported fake mode: {mode}")
elif "scripts/dev/sync_model_catalog.py" in args:
    shutil.copyfile(
        os.environ["GENERATED_AFTER"],
        "web/src/actions/modelCatalog.generated.ts",
    )
elif "scripts/dev/sync_ocr_engine_catalog.py" in args:
    expected = [
        "run",
        "--frozen",
        "--no-dev",
        "python",
        "scripts/dev/sync_ocr_engine_catalog.py",
    ]
    if args != expected:
        raise SystemExit(f"unexpected OCR sync invocation: {args}")
    shutil.copyfile(
        os.environ["OCR_GENERATED_AFTER"],
        "web/src/actions/engineCatalog.generated.ts",
    )
elif "scripts/dev/pricing_delta_report.py" in args:
    print("synthetic catalog delta")
else:
    raise SystemExit(f"unexpected uv invocation: {args}")
""",
        encoding="utf-8",
    )
    uv.chmod(0o755)

    gh = fake_bin / "gh"
    gh.write_text(
        """#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

args = sys.argv[1:]
attempts = Path(os.environ["GH_ATTEMPTS"])
state_path = Path(os.environ["GH_STATE"])
with attempts.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")

state = json.loads(state_path.read_text()) if state_path.exists() else {"numbers": []}
if args[:2] == ["pr", "list"]:
    exit_code = int(os.environ.get("GH_PR_LIST_EXIT", "0"))
    if exit_code:
        raise SystemExit(exit_code)
    if args[args.index("--state") + 1] == "merged":
        if state.get("merged_head"):
            print(state["merged_head"])
    else:
        for number in state["numbers"]:
            print(number)
elif args[:2] == ["pr", "create"]:
    if state["numbers"]:
        raise SystemExit("duplicate PR creation")
    body_path = Path(args[args.index("--body-file") + 1])
    shutil.copyfile(body_path, os.environ["GH_BODY_CAPTURE"])
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
if (
    args[:2] == ["ls-remote", "--heads"]
    and args[-1] == f"refs/heads/{os.environ['REFRESH_BRANCH']}"
    and os.environ.get("INJECT_RACE_BRANCH")
):
    marker = Path(os.environ["RACE_MARKER"])
    if not marker.exists():
        subprocess.run(
            [
                os.environ["REAL_GIT"],
                f"--git-dir={os.environ['RACE_REMOTE']}",
                "update-ref",
                f"refs/heads/{os.environ['REFRESH_BRANCH']}",
                "refs/heads/main",
            ],
            check=True,
        )
        marker.touch()
if args[-1:] == ["refs/heads/main"] and os.environ.get("INJECT_MAIN_MOVE"):
    marker = Path(os.environ["MAIN_RACE_MARKER"])
    count = int(marker.read_text()) + 1 if marker.exists() else 1
    marker.write_text(str(count))
    if count == 2:
        remote = os.environ["RACE_REMOTE"]
        old = subprocess.check_output(
            [os.environ["REAL_GIT"], f"--git-dir={remote}", "rev-parse", "refs/heads/main"],
            text=True,
        ).strip()
        tree = subprocess.check_output(
            [os.environ["REAL_GIT"], f"--git-dir={remote}", "rev-parse", f"{old}^{{tree}}"],
            text=True,
        ).strip()
        identity = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Main race",
            "GIT_AUTHOR_EMAIL": "race@example.invalid",
            "GIT_COMMITTER_NAME": "Main race",
            "GIT_COMMITTER_EMAIL": "race@example.invalid",
        }
        moved = subprocess.run(
            [
                os.environ["REAL_GIT"],
                f"--git-dir={remote}",
                "commit-tree",
                tree,
                "-p",
                old,
            ],
            input="advance main during refresh\\n",
            text=True,
            check=True,
            capture_output=True,
            env=identity,
        ).stdout.strip()
        subprocess.run(
            [
                os.environ["REAL_GIT"],
                f"--git-dir={remote}",
                "update-ref",
                "refs/heads/main",
                moved,
                old,
            ],
            check=True,
        )
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
    print(os.environ["REFRESH_DAY"])
else:
    raise SystemExit(f"unexpected date invocation: {sys.argv[1:]}")
""",
        encoding="utf-8",
    )
    date.chmod(0o755)


def _run_shell(
    repo: Path, env: dict[str, str], script: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _step_outputs(path: Path) -> dict[str, str]:
    # rule19: test-owned output from an executed workflow step.
    return dict(
        line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
    )


def _run_workflow(
    tmp_path: Path,
    *,
    mode: str,
    pricing_after: dict,
    model_after: dict | None = None,
    models_updater_noop: bool = False,
    generated_change: bool = True,
    reruns: int = 1,
    initial_pr_numbers: tuple[int, ...] = (),
    existing_branch: bool = False,
    existing_branch_pricing: dict | None = None,
    squash_merged_branch: bool = False,
    gh_pr_list_exit: int = 0,
    inject_race_branch: bool = False,
    inject_main_move: bool = False,
    expect_success: bool = True,
) -> dict:
    steps = _workflow_steps()
    repo = tmp_path / "checkout"
    remote = tmp_path / "origin.git"
    fake_bin = tmp_path / "bin"
    runner_tmp = tmp_path / "runner"
    output = runner_tmp / "github-output"
    gh_attempts = runner_tmp / "gh-attempts.jsonl"
    uv_attempts = runner_tmp / "uv-attempts.jsonl"
    gh_state = runner_tmp / "gh-state.json"
    body_capture = runner_tmp / "pr-body.md"
    branch = "pricing-refresh" if mode == "pricing" else "model-catalog-refresh"

    pricing = repo / PRICING_PATH
    model = repo / MODEL_PATH
    generated = repo / GENERATED_PATH
    ocr_generated = repo / OCR_GENERATED_PATH
    pricing.parent.mkdir(parents=True)
    generated.parent.mkdir(parents=True)
    runner_tmp.mkdir()
    pricing.write_text(json.dumps(PRICING_BEFORE, indent=2) + "\n")
    model.write_text(json.dumps(MODEL_BEFORE, indent=2) + "\n")
    generated.write_text("generated before\n")
    ocr_generated.write_text("ocr generated before\n")

    pricing_after_path = tmp_path / "pricing-after.json"
    model_after_path = tmp_path / "model-after.json"
    generated_after_path = tmp_path / "generated-after.ts"
    ocr_after_path = tmp_path / "ocr-after.ts"
    pricing_after_path.write_text(json.dumps(pricing_after, indent=2) + "\n")
    model_after_path.write_text(
        json.dumps(model_after or MODEL_BEFORE, indent=2) + "\n"
    )
    generated_after_path.write_text(
        "generated after\n" if generated_change else "generated before\n"
    )
    ocr_after_path.write_text(
        "ocr generated after\n" if generated_change else "ocr generated before\n"
    )
    _install_fake_commands(fake_bin)

    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Catalog workflow test")
    _git(repo, "config", "user.email", "catalog-test@example.invalid")
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
        _git(repo, "switch", "-c", branch)
        prior_refresh = existing_branch_pricing or pricing_after
        pricing.write_text(json.dumps(prior_refresh, indent=2) + "\n")
        _git(repo, "add", str(PRICING_PATH))
        _git(repo, "commit", "-m", "existing refresh under review")
        _git(repo, "push", "-u", "origin", branch)
        existing_tip = _git(repo, "rev-parse", "HEAD")
        _git(repo, "switch", "main")
        if squash_merged_branch:
            pricing.write_text(json.dumps(prior_refresh, indent=2) + "\n")
            _git(repo, "add", str(PRICING_PATH))
            _git(repo, "commit", "-m", "squash merged prior refresh")
            _git(repo, "push", "origin", "main")

    if initial_pr_numbers or squash_merged_branch:
        gh_state.write_text(
            json.dumps(
                {
                    "numbers": list(initial_pr_numbers),
                    "merged_head": existing_tip if squash_merged_branch else None,
                }
            )
            + "\n"
        )

    real_git = shutil.which("git")
    assert real_git is not None
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output),
        "RUNNER_TEMP": str(runner_tmp),
        "GH_ATTEMPTS": str(gh_attempts),
        "GH_STATE": str(gh_state),
        "GH_BODY_CAPTURE": str(body_capture),
        "GH_TOKEN": "test-only-token",
        "GH_PR_LIST_EXIT": str(gh_pr_list_exit),
        "UV_ATTEMPTS": str(uv_attempts),
        "REAL_GIT": real_git,
        "INJECT_RACE_BRANCH": "1" if inject_race_branch else "",
        "RACE_REMOTE": str(remote),
        "RACE_MARKER": str(runner_tmp / "race-injected"),
        "INJECT_MAIN_MOVE": "1" if inject_main_move else "",
        "MAIN_RACE_MARKER": str(runner_tmp / "main-race-count"),
        "REFRESH_MODE": mode,
        "REFRESH_BRANCH": branch,
        "REFRESH_DAY": REFRESH_DAY,
        "PRICING_AFTER": str(pricing_after_path),
        "MODEL_AFTER": str(model_after_path),
        "GENERATED_AFTER": str(generated_after_path),
        "OCR_GENERATED_AFTER": str(ocr_after_path),
        "MODELS_UPDATER_NOOP": "1" if models_updater_noop else "",
        "GITHUB_REPOSITORY": "",
    }

    returncodes: list[int] = []
    last_pending = None
    last_changed = None
    for _ in range(reruns):
        _git(repo, "switch", "main")
        _git(repo, "reset", "--hard", "origin/main")
        output.unlink(missing_ok=True)
        pending_run = _run_shell(
            repo, env, steps["Check for pending refresh PR"]["run"]
        )
        returncodes.append(pending_run.returncode)
        if pending_run.returncode != 0:
            if expect_success:
                raise AssertionError(pending_run.stderr)
            break
        last_pending = _step_outputs(output)["pending"]
        if last_pending == "true":
            continue

        output.unlink()
        base_run = _run_shell(repo, env, steps["Prepare exact main revision"]["run"])
        returncodes.append(base_run.returncode)
        if base_run.returncode != 0:
            if expect_success:
                raise AssertionError(base_run.stderr)
            break
        env["GENERATION_BASE"] = _step_outputs(output)["sha"]

        output.unlink()
        refresh_run = _run_shell(repo, env, steps["Refresh catalog"]["run"])
        returncodes.append(refresh_run.returncode)
        if refresh_run.returncode != 0:
            if expect_success:
                raise AssertionError(refresh_run.stderr)
            break
        last_changed = _step_outputs(output)["changed"]
        if last_changed == "false":
            continue

        open_run = _run_shell(repo, env, steps["Open review PR"]["run"])
        returncodes.append(open_run.returncode)
        if open_run.returncode != 0 and expect_success:
            raise AssertionError(open_run.stderr)

    remote_refs = subprocess.run(
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
    remote_tip = (
        _git(remote, "rev-parse", f"refs/heads/{branch}")
        if branch in remote_refs
        else None
    )
    gh_calls = (
        # rule19: fake transport journal produced by the workflow shell.
        [json.loads(line) for line in gh_attempts.read_text().splitlines()]
        if gh_attempts.exists()
        else []
    )
    uv_calls = (
        # rule19: fake transport journal produced by the workflow shell.
        [json.loads(line) for line in uv_attempts.read_text().splitlines()]
        if uv_attempts.exists()
        else []
    )
    state = json.loads(gh_state.read_text()) if gh_state.exists() else {"numbers": []}
    return {
        "repo": repo,
        "remote": remote,
        "branch": branch,
        "existing_tip": existing_tip,
        "remote_tip": remote_tip,
        "pending": last_pending,
        "changed": last_changed,
        "gh_calls": gh_calls,
        "uv_calls": uv_calls,
        "pr_numbers": state["numbers"],
        "body": body_capture.read_text() if body_capture.exists() else None,
        "returncodes": returncodes,
    }


def test_pricing_refresh_commits_only_the_price_book(tmp_path: Path) -> None:
    pricing_after = {
        **PRICING_BEFORE,
        "updated": REFRESH_DAY,
        "text": {"provider/model": [1.0, 3.0]},
    }

    result = _run_workflow(
        tmp_path,
        mode="pricing",
        pricing_after=pricing_after,
    )

    assert result["changed"] == "true"
    assert result["pr_numbers"] == [1]
    assert [call[:2] for call in result["gh_calls"]] == [
        ["pr", "list"],
        ["pr", "list"],
        ["pr", "create"],
    ]
    assert _git(
        result["repo"],
        "diff",
        "--name-only",
        "origin/main",
        result["remote_tip"],
    ).splitlines() == [str(PRICING_PATH)]
    assert (
        json.loads(
            _git(result["repo"], "show", f"{result['remote_tip']}:{PRICING_PATH}")
        )
        == pricing_after
    )
    assert "approved catalog models" in result["body"]
    assert "regenerated from current `main`" in result["body"]


def test_pricing_timestamp_only_refresh_is_a_noop(tmp_path: Path) -> None:
    result = _run_workflow(
        tmp_path,
        mode="pricing",
        pricing_after={**PRICING_BEFORE, "updated": REFRESH_DAY},
    )

    assert result["changed"] == "false"
    assert result["remote_tip"] is None
    assert result["pr_numbers"] == []
    assert [call[:2] for call in result["gh_calls"]] == [["pr", "list"]]


def test_pending_pr_is_checked_before_updater_and_preserved(tmp_path: Path) -> None:
    pricing_after = {
        **PRICING_BEFORE,
        "updated": REFRESH_DAY,
        "text": {"provider/model": [1.0, 3.0]},
    }
    result = _run_workflow(
        tmp_path,
        mode="pricing",
        pricing_after=pricing_after,
        existing_branch=True,
        initial_pr_numbers=(17,),
    )

    assert result["pending"] == "true"
    assert result["remote_tip"] == result["existing_tip"]
    assert result["uv_calls"] == []
    assert [call[:2] for call in result["gh_calls"]] == [["pr", "list"]]


def test_rerun_sees_stable_pending_pr_before_second_fetch(tmp_path: Path) -> None:
    pricing_after = {
        **PRICING_BEFORE,
        "updated": REFRESH_DAY,
        "text": {"provider/model": [1.0, 3.0]},
    }
    result = _run_workflow(
        tmp_path,
        mode="pricing",
        pricing_after=pricing_after,
        reruns=2,
    )

    updater_calls = [
        call for call in result["uv_calls"] if "scripts/dev/update_pricing.py" in call
    ]
    assert updater_calls == [
        [
            "run",
            "--no-project",
            "python",
            "scripts/dev/update_pricing.py",
            "--mode",
            "pricing",
        ]
    ]
    assert [call[:2] for call in result["gh_calls"]] == [
        ["pr", "list"],
        ["pr", "list"],
        ["pr", "create"],
        ["pr", "list"],
    ]


def test_exact_squash_merged_head_is_recycled_with_a_new_review_pr(
    tmp_path: Path,
) -> None:
    prior_refresh = {
        **PRICING_BEFORE,
        "updated": "2026-08-08",
        "text": {"provider/model": [1.0, 2.5]},
    }
    next_refresh = {
        **PRICING_BEFORE,
        "updated": REFRESH_DAY,
        "text": {"provider/model": [1.0, 3.0]},
    }

    result = _run_workflow(
        tmp_path,
        mode="pricing",
        pricing_after=next_refresh,
        existing_branch=True,
        existing_branch_pricing=prior_refresh,
        squash_merged_branch=True,
    )

    assert result["remote_tip"] != result["existing_tip"]
    assert (
        json.loads(
            _git(result["repo"], "show", f"{result['remote_tip']}:{PRICING_PATH}")
        )
        == next_refresh
    )
    assert [call[:2] for call in result["gh_calls"]] == [
        ["pr", "list"],
        ["pr", "list"],
        ["pr", "list"],
        ["pr", "create"],
    ]
    assert result["pr_numbers"] == [1]


def test_failed_preflight_never_runs_updater_or_mutates_branch(tmp_path: Path) -> None:
    result = _run_workflow(
        tmp_path,
        mode="pricing",
        pricing_after={**PRICING_BEFORE, "updated": REFRESH_DAY},
        gh_pr_list_exit=7,
        expect_success=False,
    )

    assert result["returncodes"] == [1]
    assert result["uv_calls"] == []
    assert result["remote_tip"] is None


def test_branch_race_never_overwrites_the_competing_branch(tmp_path: Path) -> None:
    pricing_after = {
        **PRICING_BEFORE,
        "updated": REFRESH_DAY,
        "text": {"provider/model": [1.0, 3.0]},
    }
    result = _run_workflow(
        tmp_path,
        mode="pricing",
        pricing_after=pricing_after,
        inject_race_branch=True,
        expect_success=False,
    )

    assert result["returncodes"][-1] != 0
    assert result["remote_tip"] == _git(
        result["remote"], "rev-parse", "refs/heads/main"
    )
    assert result["pr_numbers"] == []


def test_main_move_after_generation_refuses_before_refresh_push(tmp_path: Path) -> None:
    pricing_after = {
        **PRICING_BEFORE,
        "updated": REFRESH_DAY,
        "text": {"provider/model": [1.0, 3.0]},
    }

    result = _run_workflow(
        tmp_path,
        mode="pricing",
        pricing_after=pricing_after,
        inject_main_move=True,
        expect_success=False,
    )

    assert result["returncodes"][-1] != 0
    assert result["remote_tip"] is None
    assert result["pr_numbers"] == []


def test_model_discovery_commits_models_matching_prices_and_generated_catalogs(
    tmp_path: Path,
) -> None:
    model_after = {
        **MODEL_BEFORE,
        "updated": REFRESH_DAY,
        "providers": {
            **MODEL_BEFORE["providers"],
            "openai": [
                *MODEL_BEFORE["providers"]["openai"],
                {"id": "gpt-6.1-sol", "label": "GPT 6.1 Sol", "vision": True},
            ],
        },
    }
    pricing_after = {
        **PRICING_BEFORE,
        "updated": REFRESH_DAY,
        "text": {
            **PRICING_BEFORE["text"],
            "gpt-6.1-sol": [1.5, 6.0],
        },
    }

    result = _run_workflow(
        tmp_path,
        mode="models",
        pricing_after=pricing_after,
        model_after=model_after,
    )

    assert result["changed"] == "true"
    assert result["pr_numbers"] == [1]
    assert set(
        _git(
            result["repo"],
            "diff",
            "--name-only",
            "origin/main",
            result["remote_tip"],
        ).splitlines()
    ) == {
        str(PRICING_PATH),
        str(MODEL_PATH),
        str(GENERATED_PATH),
        str(OCR_GENERATED_PATH),
    }
    updater_call = next(
        call for call in result["uv_calls"] if "scripts/dev/update_pricing.py" in call
    )
    assert updater_call[-2:] == ["--mode", "models"]
    assert "official names" in result["body"]
    assert "Current frontier models" in result["body"]
    assert "no inferred power claims" in result["body"]
    assert "do not trigger them" in result["body"]


def test_opendocrouter_metadata_change_opens_model_review_without_frozen_changes(
    tmp_path: Path,
) -> None:
    pricing_after = {
        **PRICING_BEFORE,
        "updated": REFRESH_DAY,
        "opendocrouter": {
            "price_version": "2026-08",
            "data": [
                {
                    "id": "fast-ocr",
                    "name": "Fast OCR",
                    "version": "2026-08",
                    "max_sync_pages": 50,
                }
            ],
        },
    }

    result = _run_workflow(
        tmp_path,
        mode="models",
        pricing_after=pricing_after,
        model_after={**MODEL_BEFORE, "updated": REFRESH_DAY},
        generated_change=False,
    )

    assert result["changed"] == "true"
    assert set(
        _git(
            result["repo"],
            "diff",
            "--name-only",
            "origin/main",
            result["remote_tip"],
        ).splitlines()
    ) == {str(PRICING_PATH), str(MODEL_PATH)}
    assert result["pr_numbers"] == [1]


def test_model_discovery_price_only_updater_noop_opens_no_pr(tmp_path: Path) -> None:
    price_only_candidate = {
        **PRICING_BEFORE,
        "updated": REFRESH_DAY,
        "text": {"provider/model": [1.0, 3.0]},
    }

    result = _run_workflow(
        tmp_path,
        mode="models",
        pricing_after=price_only_candidate,
        models_updater_noop=True,
    )

    assert result["changed"] == "false"
    assert result["remote_tip"] is None
    assert result["pr_numbers"] == []
    # rule19: artifacts restored by the executed workflow shell.
    assert (result["repo"] / GENERATED_PATH).read_text() == "generated before\n"
    # rule19: artifacts restored by the executed workflow shell.
    assert (result["repo"] / OCR_GENERATED_PATH).read_text() == (
        "ocr generated before\n"
    )
