"""Pure-logic tests for the worktree, suite and land tools in scripts/dev."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Callable

import pytest

DEV = Path(__file__).parents[1] / "scripts/dev"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, DEV / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


worktree = _load("worktree")
suite = _load("suite")
land = _load("land")

MAIN = Path("/repo/main")
LANE = Path("/repo/wt-lane")
HEAD = "a" * 40
MERGED = "m" * 40
PORCELAIN = (
    f"worktree {MAIN}\nHEAD {'1' * 40}\nbranch refs/heads/main\n\n"
    f"worktree {LANE}\nHEAD {HEAD}\nbranch refs/heads/wt/lane\n\n"
    "worktree /repo/detached\nHEAD " + "2" * 40 + "\ndetached\n"
)

Reply = tuple[int, str]


class FakeRunner:
    """Answers commands by their leading words and records every call."""

    def __init__(self, replies: dict[tuple[str, ...], Reply | Callable[[], Reply]]):
        self.replies = replies
        self.calls: list[tuple[list[str], Path | None]] = []

    def __call__(self, args, *, cwd=None, check=True, env=None):
        args = list(args)
        self.calls.append((args, cwd))
        key = max(
            (key for key in self.replies if tuple(args[: len(key)]) == key),
            key=len,
            default=None,
        )
        if key is None:
            raise AssertionError(f"unexpected command: {args}")
        reply = self.replies[key]
        code, stdout = reply() if callable(reply) else reply
        if check and code != 0:
            raise subprocess.CalledProcessError(code, args, stdout, "refused")
        return subprocess.CompletedProcess(args, code, stdout, "")

    def ran(self, *prefix: str) -> list[int]:
        return [
            index
            for index, (args, _cwd) in enumerate(self.calls)
            if tuple(args[: len(prefix)]) == prefix
        ]


# ---------- worktree.py ----------


def test_porcelain_lists_main_checkout_first_with_branches():
    trees = worktree.parse_porcelain(PORCELAIN)
    assert [t.path for t in trees] == [MAIN, LANE, Path("/repo/detached")]
    assert [t.branch for t in trees] == ["main", "wt/lane", None]
    assert worktree.main_checkout(FakeRunner({("git",): (0, PORCELAIN)})) == MAIN


def _lane(status: str = "", unpushed: str = "0") -> FakeRunner:
    return FakeRunner(
        {
            ("git", "status"): (0, status),
            ("git", "rev-list"): (0, unpushed),
            ("git", "worktree", "remove"): (0, ""),
            ("git", "branch"): (0, ""),
        }
    )


def test_remove_refuses_uncommitted_or_unpushed_work_unless_forced():
    runner = _lane(status=" M src/x.py\n?? notes.txt", unpushed="2")
    tree = worktree.Worktree(LANE, HEAD, "wt/lane")
    with pytest.raises(worktree.WorktreeError) as refused:
        worktree.remove_worktree(runner, MAIN, tree, force=False, delete_branch=True)
    assert "2 uncommitted or untracked" in str(refused.value)
    assert "2 commit(s) on wt/lane are not on any remote" in str(refused.value)
    assert not runner.ran("git", "worktree", "remove")

    worktree.remove_worktree(runner, MAIN, tree, force=True, delete_branch=True)
    assert runner.calls[-2][0] == ["git", "worktree", "remove", "--force", str(LANE)]
    assert runner.calls[-1][0] == ["git", "branch", "-D", "wt/lane"]
    assert all(cwd == MAIN for args, cwd in runner.calls if "remove" in args)


def test_remove_never_touches_the_main_checkout():
    runner = _lane()
    with pytest.raises(worktree.WorktreeError, match="main checkout"):
        worktree.remove_worktree(
            runner,
            MAIN,
            worktree.Worktree(MAIN, HEAD, "main"),
            force=True,
            delete_branch=True,
        )
    assert runner.calls == []


def test_remove_of_a_clean_pushed_lane_uses_safe_branch_delete():
    runner = _lane()
    tree = worktree.Worktree(LANE, HEAD, "wt/lane")
    worktree.remove_worktree(runner, MAIN, tree, force=False, delete_branch=True)
    assert runner.calls[-2][0] == ["git", "worktree", "remove", str(LANE)]
    assert runner.calls[-1][0] == ["git", "branch", "-d", "wt/lane"]


def test_probe_refuses_broken_venv_with_the_additive_fix(tmp_path):
    report = {"frisket": None, "broken": ["pydantic: not installed"], "drift": []}
    with pytest.raises(worktree.WorktreeError) as refused:
        worktree.check_probe(report, MAIN, tmp_path)
    assert "pydantic: not installed" in str(refused.value)
    assert f"cd {MAIN} && uv sync --inexact --frozen" in str(refused.value)


def test_probe_refuses_frisket_imported_from_another_checkout(tmp_path):
    report = {
        "frisket": str(MAIN / "src/frisket/__init__.py"),
        "broken": [],
        "drift": [],
    }
    with pytest.raises(worktree.WorktreeError, match="resolved to"):
        worktree.check_probe(report, MAIN, tmp_path)


def test_probe_accepts_worktree_import_and_reports_drift(tmp_path):
    package = tmp_path / "src/frisket"
    report = {
        "frisket": str(package / "__init__.py"),
        "broken": [],
        "drift": ["fastapi: venv 1.0, uv.lock 1.1"],
    }
    assert worktree.check_probe(report, MAIN, tmp_path) == report["drift"]


# ---------- suite.py ----------


def test_lock_holder_is_live_only_while_its_pid_is_alive():
    record = json.dumps({"pid": 4242, "worktree": "/repo/wt-a"})
    assert (
        suite.lock_holder(record, lambda pid: pid == 4242)["worktree"] == "/repo/wt-a"
    )
    assert suite.lock_holder(record, lambda pid: False) is None
    assert suite.lock_holder("", lambda pid: True) is None
    assert suite.lock_holder('{"pid": 0}', lambda pid: True) is None
    assert suite.lock_holder('{"worktree": "/x"}', lambda pid: True) is None


def test_lock_refuses_live_holder_and_takes_over_a_stale_one(tmp_path):
    lock = tmp_path / "cache/suite.lock"
    assert suite.try_lock(lock, Path("/repo/wt-a"), lambda pid: True) is None
    mine = json.loads(lock.read_text())
    assert mine["worktree"] == "/repo/wt-a"

    held = suite.try_lock(lock, Path("/repo/wt-b"), lambda pid: True)
    assert held["pid"] == mine["pid"]
    assert json.loads(lock.read_text())["worktree"] == "/repo/wt-a"

    lock.write_text(json.dumps({"pid": 999_999, "worktree": "/repo/crashed"}))
    assert suite.try_lock(lock, Path("/repo/wt-b"), lambda pid: False) is None
    assert json.loads(lock.read_text())["worktree"] == "/repo/wt-b"


def test_release_removes_only_this_process_lock(tmp_path):
    lock = tmp_path / "suite.lock"
    lock.write_text(json.dumps({"pid": 999_999, "worktree": "/other"}))
    suite.release_lock(lock)
    assert lock.exists()
    assert suite.try_lock(lock, Path("/repo"), lambda pid: False) is None
    suite.release_lock(lock)
    assert not lock.exists()


@pytest.mark.parametrize(
    ("cpus", "load", "cap", "expected"),
    [
        (16, 3.0, 8, 8),
        (8, 6.5, 8, 1),
        (12, 7.9, 8, 4),
        (8, 0.0, 4, 4),
        (4, 150.0, 8, 1),
        (None, 0.0, 8, 1),
    ],
)
def test_worker_count_leaves_room_for_current_load(cpus, load, cap, expected):
    assert suite.worker_count(cpus, load, cap) == expected


def test_known_failures_file_maps_node_ids_to_diagnoses(tmp_path):
    known = tmp_path / "known.txt"
    known.write_text(
        "# heading\n\n"
        "tests/a.py::test_x  # needs GNU find\n"
        "tests/b.py::TestC::test_y[param one]\n"
    )
    assert suite.read_known(known) == {
        "tests/a.py::test_x": "needs GNU find",
        "tests/b.py::TestC::test_y[param one]": "",
    }


JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.engine.test_a" name="test_ok" file="tests/engine/test_a.py" line="1"/>
  <testcase classname="tests.engine.test_a" name="test_new" file="tests/engine/test_a.py" line="2">
    <failure message="boom">boom</failure></testcase>
  <testcase classname="tests.engine.test_a.TestCls" name="test_known[p-1]" file="tests/engine/test_a.py" line="3">
    <failure message="known">known</failure></testcase>
  <testcase classname="tests.test_b" name="test_setup" file="tests/test_b.py" line="4">
    <error message="fixture">fixture</error></testcase>
  <testcase classname="tests.test_b" name="test_fixed" file="tests/test_b.py" line="5"/>
  <testcase classname="tests.test_b" name="test_skipped_known" file="tests/test_b.py" line="6">
    <skipped message="no tool">no tool</skipped></testcase>
</testsuite></testsuites>
"""


def test_failures_classify_three_ways_against_the_known_list():
    outcomes = suite.junit_outcomes(JUNIT)
    assert outcomes.failed == {
        "tests/engine/test_a.py::test_new",
        "tests/engine/test_a.py::TestCls::test_known[p-1]",
        "tests/test_b.py::test_setup",
    }
    known = {
        "tests/engine/test_a.py::TestCls::test_known[p-1]",
        "tests/test_b.py::test_fixed",
        "tests/test_b.py::test_skipped_known",
        "tests/not_selected.py::test_elsewhere",
    }
    result = suite.classify(outcomes, known)
    assert result.new == [
        "tests/engine/test_a.py::test_new",
        "tests/test_b.py::test_setup",
    ]
    assert result.known == ["tests/engine/test_a.py::TestCls::test_known[p-1]"]
    # A skipped or unselected known failure is not evidence it was fixed.
    assert result.fixed == ["tests/test_b.py::test_fixed"]


# ---------- land.py ----------


def _check(
    name: str,
    status: str = "COMPLETED",
    conclusion: str = "SUCCESS",
    required: bool = True,
):
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "isRequired": required,
    }


def _pr(
    *,
    state: str = "OPEN",
    checks: list[dict] | None = None,
    checked_oid: str = HEAD,
    merge_state: str = "CLEAN",
    mergeable: str = "MERGEABLE",
) -> dict:
    contexts = [_check("ci-pass")] if checks is None else checks
    return {
        "number": 7,
        "state": state,
        "isDraft": False,
        "isCrossRepository": False,
        "headRefName": "wt/lane",
        "headRefOid": HEAD,
        "baseRefName": "main",
        "mergeable": mergeable,
        "mergeStateStatus": merge_state,
        "mergeCommit": {"oid": MERGED} if state == "MERGED" else None,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": checked_oid,
                        "statusCheckRollup": {"contexts": {"nodes": contexts}},
                    }
                }
            ]
        },
    }


def _github(*states: dict) -> FakeRunner:
    pending = list(states)

    def graphql() -> Reply:
        pr = pending.pop(0) if len(pending) > 1 else pending[0]
        return 0, json.dumps({"data": {"repository": {"pullRequest": pr}}})

    return FakeRunner(
        {
            ("gh", "api", "graphql"): graphql,
            ("gh", "pr", "merge"): (0, ""),
            ("gh", "pr", "update-branch"): (0, ""),
            ("git", "worktree", "list"): (0, PORCELAIN),
            ("git", "worktree", "remove"): (0, ""),
            ("git", "symbolic-ref"): (0, "main"),
            ("git", "status"): (0, ""),
            ("git", "fetch"): (0, ""),
            ("git", "merge"): (0, ""),
            ("git", "merge-base"): (0, ""),
            ("git", "rev-parse"): (0, HEAD),
            ("git", "branch"): (0, ""),
            ("git", "ls-remote"): (0, f"{HEAD}\trefs/heads/wt/lane"),
            ("git", "push"): (0, ""),
        }
    )


def _land(runner: FakeRunner, *, dry_run: bool = False) -> int:
    return land.Lander(
        runner, repo="owner/repo", remote="origin", dry_run=dry_run, number=7
    ).land()


MUTATIONS = (
    ("gh", "pr", "merge"),
    ("gh", "pr", "update-branch"),
    ("git", "worktree", "remove"),
    ("git", "branch"),
    ("git", "push"),
    ("git", "fetch"),
    ("git", "merge"),
)


def _mutations(runner: FakeRunner) -> list[list[str]]:
    return [
        args
        for args, _cwd in runner.calls
        if any(tuple(args[: len(prefix)]) == prefix for prefix in MUTATIONS)
        and args[:2] != ["git", "merge-base"]
    ]


def test_land_merges_then_confirms_then_fast_forwards_then_cleans_up():
    runner = _github(_pr(), _pr(state="MERGED"))
    assert _land(runner) == 0
    assert _mutations(runner) == [
        [
            "gh",
            "pr",
            "merge",
            "7",
            "--repo",
            "owner/repo",
            "--squash",
            "--match-head-commit",
            HEAD,
        ],
        ["git", "fetch", "origin", "main"],
        ["git", "merge", "--ff-only", "origin/main"],
        ["git", "worktree", "remove", str(LANE)],
        ["git", "branch", "-D", "wt/lane"],
        ["git", "push", "origin", "--delete", "wt/lane"],
    ]
    [merge] = runner.ran("gh", "pr", "merge")
    confirm = runner.ran("gh", "api", "graphql")[1]
    assert merge < confirm < runner.ran("git", "fetch")[0]
    assert (
        runner.ran("git", "merge-base")[0] < runner.ran("git", "worktree", "remove")[0]
    )
    assert not any("--admin" in args for args, _cwd in runner.calls)
    assert {cwd for args, cwd in runner.calls if args[:2] == ["git", "fetch"]} == {MAIN}


def test_land_ignores_checks_reported_for_an_older_head():
    runner = _github(_pr(checked_oid="b" * 40))
    with pytest.raises(land.LandStop, match="the head is"):
        _land(runner)
    assert _mutations(runner) == []


@pytest.mark.parametrize(
    ("checks", "reason"),
    [
        (
            [_check("ci-pass", status="IN_PROGRESS", conclusion=None)],
            "ci-pass: in_progress",
        ),
        ([_check("ci-pass", conclusion="FAILURE")], "ci-pass: failure"),
        ([_check("lint", required=False)], "no required checks"),
    ],
)
def test_land_requires_every_required_check_green(checks, reason):
    runner = _github(_pr(checks=checks))
    with pytest.raises(land.LandStop, match=reason):
        _land(runner)
    assert _mutations(runner) == []


def test_land_ignores_failing_checks_that_are_not_required():
    checks = [
        _check("ci-pass"),
        _check("optional", conclusion="FAILURE", required=False),
    ]
    runner = _github(_pr(checks=checks), _pr(state="MERGED"))
    assert _land(runner) == 0


def test_land_updates_a_behind_branch_and_exits_without_merging():
    runner = _github(_pr(merge_state="BEHIND"))
    with pytest.raises(land.LandStop) as stop:
        _land(runner)
    assert stop.value.code == land.UPDATED
    assert _mutations(runner) == [
        ["gh", "pr", "update-branch", "7", "--repo", "owner/repo"]
    ]


@pytest.mark.parametrize(
    ("mergeable", "merge_state", "reason"),
    [
        ("UNKNOWN", "UNKNOWN", "still computing"),
        ("CONFLICTING", "DIRTY", "not mergeable"),
        ("MERGEABLE", "BLOCKED", "branch protection"),
    ],
)
def test_land_stops_when_github_will_not_merge(mergeable, merge_state, reason):
    runner = _github(_pr(mergeable=mergeable, merge_state=merge_state))
    with pytest.raises(land.LandStop, match=reason):
        _land(runner)
    assert _mutations(runner) == []


def test_land_never_cleans_up_when_the_merge_did_not_complete():
    runner = _github(_pr(), _pr(state="OPEN"))
    with pytest.raises(land.LandStop, match="not MERGED"):
        _land(runner)
    assert _mutations(runner) == [
        [
            "gh",
            "pr",
            "merge",
            "7",
            "--repo",
            "owner/repo",
            "--squash",
            "--match-head-commit",
            HEAD,
        ]
    ]


def test_land_keeps_a_local_branch_with_work_beyond_the_merged_head():
    runner = _github(_pr(state="MERGED"))
    runner.replies[("git", "rev-parse")] = (0, "c" * 40)
    with pytest.raises(land.LandStop, match="kept"):
        _land(runner)
    assert ["git", "worktree", "remove", str(LANE)] not in _mutations(runner)
    assert not runner.ran("git", "branch")
    assert not runner.ran("git", "push")


def test_land_refuses_to_fast_forward_a_main_checkout_off_its_base():
    runner = _github(_pr(state="MERGED"))
    runner.replies[("git", "symbolic-ref")] = (0, "feature")
    with pytest.raises(land.LandStop, match="not main"):
        _land(runner)
    assert _mutations(runner) == []


def test_land_dry_run_runs_no_mutating_command(capsys):
    runner = _github(_pr())
    assert _land(runner, dry_run=True) == 0
    assert _mutations(runner) == []
    out = capsys.readouterr().out
    assert "would run: gh pr merge 7 --repo owner/repo --squash" in out
    assert "would run: git push origin --delete wt/lane" in out
