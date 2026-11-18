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


@pytest.mark.parametrize(
    ("cpus", "load", "expected"),
    [
        (10, 21.0, 4),
        (10, 0.0, 8),
        (16, 3.0, 8),
        (4, 150.0, 2),
        (2, 9.0, 2),
        (None, 0.0, 2),
    ],
)
def test_full_run_never_starves_below_the_floor(cpus, load, expected):
    assert suite.worker_count(cpus, load, 8, full=True) == expected


def test_worker_reason_names_count_and_cause():
    text = suite.worker_reason(4, 10, 21.0, override=False, full=True)
    assert text.startswith("4 workers") and "load 21.0" in text and "floor 4" in text
    assert "explicit -n" in suite.worker_reason(3, 10, 0.0, override=True, full=True)


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
    queue: bool = False,
    entry: dict | None = None,
    auto: bool = False,
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
        "mergeQueueEntry": entry,
        "autoMergeRequest": {"enabledAt": "2026-01-01T00:00:00Z"} if auto else None,
        "_queue": queue,
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
        repository = {
            "pullRequest": {k: v for k, v in pr.items() if k != "_queue"},
            "mergeQueue": {"id": "MQ"} if pr["_queue"] else None,
        }
        return 0, json.dumps({"data": {"repository": repository}})

    return FakeRunner(
        {
            ("gh", "api", "graphql"): graphql,
            ("gh", "pr", "merge"): (0, ""),
            ("gh", "pr", "update-branch"): (0, ""),
            ("gh", "run", "list"): (0, "[]"),
            ("gh", "run", "view"): (0, ""),
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


def _land(runner: FakeRunner, *, dry_run: bool = False, wait: bool = False) -> int:
    return land.Lander(
        runner,
        repo="owner/repo",
        remote="origin",
        dry_run=dry_run,
        number=7,
        wait=wait,
        sleep=lambda seconds: None,
        poll_seconds=1,
        timeout_seconds=5,
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


def _entry(position: int, state: str = "QUEUED") -> dict:
    return {"state": state, "position": position}


QUEUE_MERGE = [
    "gh",
    "pr",
    "merge",
    "7",
    "--repo",
    "owner/repo",
    "--squash",
    "--auto",
    "--match-head-commit",
    HEAD,
]


def test_land_in_a_queue_repository_enqueues_and_exits_queued_without_cleanup():
    runner = _github(
        _pr(queue=True, merge_state="BEHIND"),
        _pr(queue=True, merge_state="BEHIND", entry=_entry(3)),
    )
    with pytest.raises(land.LandStop, match="position 3") as stop:
        _land(runner)
    assert stop.value.code == land.QUEUED == 4
    assert _mutations(runner) == [QUEUE_MERGE]
    assert not runner.ran("gh", "pr", "update-branch")


def test_land_waits_in_the_queue_then_fast_forwards_and_cleans_up():
    runner = _github(
        _pr(queue=True),
        _pr(queue=True, entry=_entry(2)),
        _pr(queue=True, entry=_entry(1, "AWAITING_CHECKS")),
        _pr(queue=True, state="MERGED"),
    )
    assert _land(runner, wait=True) == 0
    assert _mutations(runner) == [
        QUEUE_MERGE,
        ["git", "fetch", "origin", "main"],
        ["git", "merge", "--ff-only", "origin/main"],
        ["git", "worktree", "remove", str(LANE)],
        ["git", "branch", "-D", "wt/lane"],
        ["git", "push", "origin", "--delete", "wt/lane"],
    ]


def test_land_wait_reports_an_ejection_with_the_failing_check():
    runner = _github(
        _pr(queue=True),
        _pr(queue=True, entry=_entry(1)),
        _pr(queue=True, entry=_entry(1)),
        _pr(queue=True),
    )
    runs = [
        {
            "name": "deterministic-source",
            "conclusion": "failure",
            "headBranch": f"gh-readonly-queue/main/pr-7-{HEAD}",
            "url": "https://example/run/1",
        },
        {
            "name": "ci-pass",
            "conclusion": "failure",
            "headBranch": f"gh-readonly-queue/main/pr-8-{HEAD}",
            "url": "https://example/run/2",
        },
    ]
    runner.replies[("gh", "run", "list")] = (0, json.dumps(runs))
    with pytest.raises(land.LandStop, match="left the merge queue") as stop:
        _land(runner, wait=True)
    assert "deterministic-source" in str(stop.value)
    assert "ci-pass" not in str(stop.value)
    assert stop.value.code == 1
    assert ["git", "worktree", "remove", str(LANE)] not in _mutations(runner)
    assert not runner.ran("git", "fetch")


def test_land_resumes_a_pr_that_is_already_queued_without_merging_again():
    runner = _github(_pr(queue=True, entry=_entry(1)), _pr(queue=True, state="MERGED"))
    assert _land(runner, wait=True) == 0
    assert not runner.ran("gh", "pr", "merge")
    assert runner.ran("git", "fetch")


def test_land_without_a_queue_still_updates_a_behind_branch_and_merges_directly():
    behind = _github(_pr(merge_state="BEHIND"))
    with pytest.raises(land.LandStop) as stop:
        _land(behind)
    assert stop.value.code == land.UPDATED
    direct = _github(_pr(), _pr(state="MERGED"))
    assert _land(direct, wait=True) == 0
    assert all("--auto" not in args for args in _mutations(direct))


PENDING = [_check("deterministic-source", status="IN_PROGRESS", conclusion=None)]


def test_land_without_wait_stops_on_pending_checks_and_says_pending():
    runner = _github(_pr(queue=True, checks=PENDING, merge_state="BLOCKED"))
    with pytest.raises(land.LandStop, match="still pending") as stop:
        _land(runner)
    assert "deterministic-source: in_progress" in str(stop.value)
    assert "--wait" in str(stop.value)
    assert stop.value.code == 1
    assert _mutations(runner) == []


def test_land_wait_queues_a_pending_pr_then_follows_it_to_merged():
    runner = _github(
        _pr(queue=True, checks=PENDING, merge_state="BLOCKED"),
        _pr(queue=True, checks=PENDING, merge_state="BLOCKED", auto=True),
        _pr(queue=True, auto=True),
        _pr(queue=True, entry=_entry(1)),
        _pr(queue=True, state="MERGED"),
    )
    assert _land(runner, wait=True) == 0
    assert _mutations(runner) == [
        QUEUE_MERGE,
        ["git", "fetch", "origin", "main"],
        ["git", "merge", "--ff-only", "origin/main"],
        ["git", "worktree", "remove", str(LANE)],
        ["git", "branch", "-D", "wt/lane"],
        ["git", "push", "origin", "--delete", "wt/lane"],
    ]


def test_land_wait_does_not_request_auto_merge_twice():
    runner = _github(
        _pr(queue=True, checks=PENDING, merge_state="BLOCKED", auto=True),
        _pr(queue=True, state="MERGED"),
    )
    assert _land(runner, wait=True) == 0
    assert not runner.ran("gh", "pr", "merge")


def test_land_wait_stops_when_a_pending_required_check_fails():
    failed = [_check("deterministic-source", conclusion="FAILURE")]
    runner = _github(
        _pr(queue=True, checks=PENDING, merge_state="BLOCKED"),
        _pr(queue=True, checks=PENDING, merge_state="BLOCKED", auto=True),
        _pr(queue=True, checks=failed, merge_state="BLOCKED", auto=True),
    )
    with pytest.raises(land.LandStop, match="failed") as stop:
        _land(runner, wait=True)
    assert "deterministic-source: failure" in str(stop.value)
    assert stop.value.code == 1
    assert _mutations(runner) == [QUEUE_MERGE]


def test_land_wait_stops_when_the_pr_starts_conflicting():
    runner = _github(
        _pr(queue=True, checks=PENDING, merge_state="BLOCKED"),
        _pr(queue=True, checks=PENDING, merge_state="BLOCKED", auto=True),
        _pr(queue=True, auto=True, mergeable="CONFLICTING", merge_state="DIRTY"),
    )
    with pytest.raises(land.LandStop, match="conflicts with its base") as stop:
        _land(runner, wait=True)
    assert stop.value.code == 1
    assert _mutations(runner) == [QUEUE_MERGE]


def test_land_wait_refuses_to_queue_a_conflicting_pending_pr():
    runner = _github(
        _pr(queue=True, checks=PENDING, mergeable="CONFLICTING", merge_state="DIRTY")
    )
    with pytest.raises(land.LandStop, match="conflicts with its base"):
        _land(runner, wait=True)
    assert _mutations(runner) == []


@pytest.mark.parametrize(
    ("log", "infrastructure"),
    [
        (
            "error: Failed to download distribution due to network timeout. "
            "Try increasing UV_HTTP_TIMEOUT (current value: 30s).",
            True,
        ),
        ("FAILED tests/test_x.py::test_y - AssertionError", False),
    ],
    ids=["download-timeout", "test-failure"],
)
def test_land_wait_names_the_ejecting_check_and_flags_infrastructure(
    log, infrastructure
):
    runner = _github(
        _pr(queue=True, checks=PENDING, merge_state="BLOCKED"),
        _pr(queue=True, checks=PENDING, merge_state="BLOCKED", auto=True),
        _pr(queue=True, entry=_entry(1)),
        _pr(queue=True),
    )
    runs = [
        {
            "databaseId": 41,
            "name": "python-quality",
            "conclusion": "failure",
            "headBranch": f"gh-readonly-queue/main/pr-7-{HEAD}",
            "url": "https://example/run/41",
        }
    ]
    runner.replies[("gh", "run", "list")] = (0, json.dumps(runs))
    runner.replies[("gh", "run", "view")] = (0, log)
    with pytest.raises(land.LandStop, match="left the merge queue") as stop:
        _land(runner, wait=True)
    assert "python-quality" in str(stop.value)
    assert ("infrastructure failure" in str(stop.value)) is infrastructure
    assert stop.value.code == 1
    [view] = runner.ran("gh", "run", "view")
    assert runner.calls[view][0][3] == "41"
    assert _mutations(runner) == [QUEUE_MERGE]


def test_blocked_merge_state_waits_when_the_pr_has_a_queue_entry():
    queued = _pr(queue=True, merge_state="BLOCKED", entry=_entry(2))
    assert land.merge_gate(queued) == "ok"
    with pytest.raises(land.LandStop, match="branch protection"):
        land.merge_gate(_pr(queue=True, merge_state="BLOCKED"))


def test_land_wait_keeps_waiting_while_blocked_behind_another_queue_entry():
    runner = _github(
        _pr(queue=True, merge_state="BLOCKED", entry=_entry(2)),
        _pr(queue=True, merge_state="BLOCKED", entry=_entry(1)),
        _pr(queue=True, state="MERGED"),
    )
    assert _land(runner, wait=True) == 0
    assert not runner.ran("gh", "pr", "merge")


class _FlakyPolls:
    """Fails the first graphql polls after the initial read with a gh error."""

    def __init__(self, inner: FakeRunner, stderr: str, failures: int):
        self.inner, self.stderr, self.failures = inner, stderr, failures
        self.graphql_calls = 0

    def __call__(self, args, *, cwd=None, check=True, env=None):
        if list(args[:3]) == ["gh", "api", "graphql"]:
            self.graphql_calls += 1
            if self.graphql_calls > 2 and self.failures > 0:
                self.failures -= 1
                raise subprocess.CalledProcessError(1, args, "", self.stderr)
        return self.inner(args, cwd=cwd, check=check, env=env)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _polled(stderr: str, failures: int) -> tuple[_FlakyPolls, list[float]]:
    pauses: list[float] = []
    runner = _FlakyPolls(
        _github(
            _pr(queue=True),
            _pr(queue=True, entry=_entry(1)),
            _pr(queue=True, entry=_entry(1)),
            _pr(queue=True, state="MERGED"),
        ),
        stderr,
        failures,
    )
    return runner, pauses


def _wait(runner, pauses: list[float]) -> int:
    return land.Lander(
        runner,
        repo="owner/repo",
        remote="origin",
        dry_run=False,
        number=7,
        wait=True,
        sleep=pauses.append,
        poll_seconds=1,
        timeout_seconds=50,
    ).land()


def test_land_wait_retries_a_transient_network_error_during_polling():
    runner, pauses = _polled("error connecting to api.github.com", 2)
    assert _wait(runner, pauses) == 0
    assert pauses.count(land.RETRY_PAUSE_SECONDS) == 2


def test_land_wait_gives_up_after_bounded_transient_retries():
    runner, pauses = _polled("dial tcp: i/o timeout", 99)
    with pytest.raises(land.LandStop, match="timeout"):
        _wait(runner, pauses)
    assert pauses.count(land.RETRY_PAUSE_SECONDS) == land.POLL_RETRIES - 1


def test_land_wait_stops_at_once_on_a_real_api_error():
    runner, pauses = _polled("HTTP 401: Bad credentials", 1)
    with pytest.raises(land.LandStop, match="Bad credentials"):
        _wait(runner, pauses)
    assert land.RETRY_PAUSE_SECONDS not in pauses


# ---------- suite.py watchdog and memory cap ----------


def test_stall_watch_fires_only_after_the_log_state_stops_changing():
    now = [0.0]
    watch = suite.StallWatch(60, clock=lambda: now[0])
    assert not watch.stalled((10, 1))
    now[0] = 50
    assert not watch.stalled((10, 1))
    now[0] = 55
    assert not watch.stalled((20, 2))
    now[0] = 114
    assert not watch.stalled((20, 2))
    now[0] = 115
    assert watch.stalled((20, 2))


def test_tee_terminates_a_silent_process_and_reports_the_last_line(tmp_path):
    script = "import time; print('tests/a.py::test_x', flush=True); time.sleep(30)"
    log = tmp_path / "run.log"
    status, last = suite.tee(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={},
        log=log,
        stall_seconds=0.4,
        poll_seconds=0.1,
    )
    assert status != 0
    assert last == "tests/a.py::test_x"
    assert "test_x" in log.read_text()


def test_tee_without_a_stall_returns_the_exit_status(tmp_path):
    status, last = suite.tee(
        [sys.executable, "-c", "print('ok')"],
        cwd=tmp_path,
        env={},
        log=tmp_path / "run.log",
        stall_seconds=5,
        poll_seconds=0.1,
    )
    assert (status, last) == (0, None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("11G", 11 * 2**30), ("512m", 512 * 2**20), ("1024", 1024), ("0", 0)],
)
def test_parse_size(text, expected):
    assert suite.parse_size(text) == expected


def test_memory_limit_defaults_to_seventy_percent_of_physical_memory():
    assert suite.memory_limit(None, 1000) == 700
    assert suite.memory_limit("2G", 1000) == 2 * 2**30
    assert suite.memory_limit("0", 1000) == 0
    assert suite.memory_limit(None, None) == 0


def test_memory_cap_wraps_the_command_in_a_user_scope():
    command, note = suite.with_memory_cap(["pytest"], 4096, lambda: True)
    assert command == [
        "systemd-run",
        "--user",
        "--scope",
        "--collect",
        "--quiet",
        "-p",
        "MemoryMax=4096",
        "pytest",
    ]
    assert "MemoryMax=4096" in note or "MemoryMax=" in note


def test_memory_cap_is_off_when_disabled_or_systemd_is_unavailable():
    assert suite.with_memory_cap(["pytest"], 0, lambda: True) == (
        ["pytest"],
        "memory cap: off (disabled)",
    )
    command, note = suite.with_memory_cap(["pytest"], 4096, lambda: False)
    assert command == ["pytest"]
    assert note.startswith("memory cap: off")
