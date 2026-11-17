#!/usr/bin/env python3
"""Land one pull request: verify, squash-merge, fast-forward, clean up.

  land.py <pr-number> [--wait] [--dry-run] [--repo OWNER/NAME] [--remote origin]

Each step runs only when the previous one succeeded and stops with a message
otherwise:

1. every required check on the PR's current head commit is green (the
   checks are read from that exact commit, so a run for an older head
   never counts);
2. GitHub reports the PR mergeable; without a merge queue, a branch behind
   its base is updated and the script exits: re-run it once CI is green on
   the new head. With a merge queue a behind branch is left alone (the queue
   tests the PR on top of the base);
3. squash-merge, pinned to the verified head commit (never ``--admin``); with
   a merge queue the command enqueues the PR (``--auto``) instead of merging;
4. the PR state is confirmed MERGED. A queued PR exits with status 4 and its
   queue position; ``--wait`` polls until it is MERGED or leaves the queue
   (an ejection names the failing check);
5. the main checkout fast-forwards its base branch to include the merge;
6. only then is the lane's worktree removed and its local branch deleted,
   and the remote branch is deleted (never for a PR that is not MERGED).

A PR that is already MERGED resumes at step 5; one already in the queue
resumes at step 4. ``--dry-run`` performs the read-only checks and prints
every mutating command instead of running it.

Exit status: 0 landed (or planned), 1 stopped, 3 branch updated: re-run later,
4 queued and not merged yet: re-run later or with ``--wait``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

from worktree import (
    Runner,
    WorktreeError,
    list_worktrees,
    removal_blockers,
    remove_worktree,
    run,
)

UPDATED = 3
QUEUED = 4
POLL_SECONDS = 30
WAIT_TIMEOUT_SECONDS = 7200
PASSING_CONCLUSIONS = {"SUCCESS", "SKIPPED", "NEUTRAL"}
MERGEABLE_STATES = {"CLEAN", "HAS_HOOKS", "UNSTABLE"}
PR_QUERY = """
query($owner: String!, $repo: String!, $n: Int!) {
  repository(owner: $owner, name: $repo) {
    mergeQueue { id }
    pullRequest(number: $n) {
      number state isDraft isCrossRepository
      mergeQueueEntry { state position }
      headRefName headRefOid baseRefName mergeable mergeStateStatus
      mergeCommit { oid }
      commits(last: 1) { nodes { commit { oid statusCheckRollup { contexts(first: 100) {
        nodes {
          __typename
          ... on CheckRun { name status conclusion isRequired(pullRequestNumber: $n) }
          ... on StatusContext { context state isRequired(pullRequestNumber: $n) }
        }
      } } } } }
    }
  }
}
"""


class LandStop(RuntimeError):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def check_problems(pr: dict) -> list[str]:
    """Why the required checks on the PR's current head are not all green."""
    commits = pr["commits"]["nodes"]
    if not commits:
        return ["the PR has no commits"]
    commit = commits[-1]["commit"]
    if commit["oid"] != pr["headRefOid"]:
        return [
            f"checks were reported for {commit['oid']}, but the head is "
            f"{pr['headRefOid']}; wait for CI on the current head"
        ]
    rollup = commit.get("statusCheckRollup") or {}
    contexts = (rollup.get("contexts") or {}).get("nodes") or []
    required = [context for context in contexts if context.get("isRequired")]
    if not required:
        return [f"no required checks have reported on {commit['oid']} yet"]
    problems = []
    for context in required:
        if context["__typename"] == "CheckRun":
            name, status = context["name"], context["status"]
            if status != "COMPLETED":
                problems.append(f"{name}: {status.lower()}")
            elif context["conclusion"] not in PASSING_CONCLUSIONS:
                problems.append(f"{name}: {str(context['conclusion']).lower()}")
        elif context["state"] != "SUCCESS":
            problems.append(f"{context['context']}: {context['state'].lower()}")
    return problems


def merge_gate(pr: dict) -> str:
    """``ok`` or ``behind``; raises LandStop for every other merge state."""
    mergeable, status = pr["mergeable"], pr["mergeStateStatus"]
    if mergeable == "UNKNOWN" or status == "UNKNOWN":
        raise LandStop("GitHub is still computing mergeability; re-run shortly")
    if mergeable != "MERGEABLE":
        raise LandStop(
            f"the PR is not mergeable ({mergeable.lower()}); resolve conflicts"
        )
    if status == "BEHIND":
        return "behind"
    if status not in MERGEABLE_STATES:
        raise LandStop(
            f"GitHub reports merge state {status}: "
            + {
                "BLOCKED": "branch protection is not satisfied (reviews or checks)",
                "DIRTY": "the merge has conflicts",
                "DRAFT": "the PR is a draft",
            }.get(status, "not mergeable now")
        )
    return "ok"


def queue_summary(pr: dict) -> str:
    entry = pr.get("mergeQueueEntry")
    if entry is None:
        return "auto-merge is enabled and the PR will enter the merge queue once its checks pass"
    return (
        f"in the merge queue at position {entry['position']} ({entry['state'].lower()})"
    )


class Lander:
    def __init__(
        self,
        runner: Runner,
        *,
        repo: str,
        remote: str,
        dry_run: bool,
        number: int,
        wait: bool = False,
        sleep=time.sleep,
        poll_seconds: float = POLL_SECONDS,
        timeout_seconds: float = WAIT_TIMEOUT_SECONDS,
    ) -> None:
        self.wait = wait
        self.sleep = sleep
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self.repo = repo
        self.remote = remote
        self.dry_run = dry_run
        self.number = number

    def read(self, args: Sequence[str], cwd: Optional[Path] = None) -> str:
        try:
            return self.runner(list(args), cwd=cwd).stdout.strip()
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or "").strip()
            raise LandStop(f"{' '.join(args[:3])} failed: {detail}") from None

    def mutate(self, args: Sequence[str], cwd: Optional[Path] = None) -> None:
        if "--admin" in args:
            raise LandStop("refusing to run with --admin")
        if self.dry_run:
            print(f"  would run: {' '.join(args)}" + (f"   (in {cwd})" if cwd else ""))
            return
        print(f"  running: {' '.join(args)}")
        self.read(args, cwd)

    def pull_request(self) -> dict:
        owner, _, name = self.repo.partition("/")
        text = self.read(
            [
                "gh",
                "api",
                "graphql",
                "-F",
                f"owner={owner}",
                "-F",
                f"repo={name}",
                "-F",
                f"n={self.number}",
                "-f",
                f"query={PR_QUERY}",
            ]
        )
        repository = json.loads(text)["data"]["repository"]
        pr = repository["pullRequest"]
        if pr is None:
            raise LandStop(f"PR #{self.number} not found in {self.repo}")
        return {**pr, "hasMergeQueue": repository.get("mergeQueue") is not None}

    def merge(self, pr: dict) -> None:
        problems = check_problems(pr)
        if problems:
            raise LandStop(
                "required checks are not green on the current head:\n  - "
                + "\n  - ".join(problems)
            )
        print(f"checks: required checks green on {pr['headRefOid']}")
        queued = pr["hasMergeQueue"]
        if merge_gate(pr) == "behind" and not queued:
            self.mutate(
                ["gh", "pr", "update-branch", str(self.number), "--repo", self.repo]
            )
            raise LandStop(
                f"{pr['headRefName']} was behind {pr['baseRefName']} and has been "
                f"updated; CI now runs on the new head. Re-run land.py {self.number} "
                "once its required checks are green.",
                UPDATED,
            )
        print("mergeable: yes")
        command = [
            "gh",
            "pr",
            "merge",
            str(self.number),
            "--repo",
            self.repo,
            "--squash",
        ]
        if queued:
            command.append("--auto")
        self.mutate([*command, "--match-head-commit", pr["headRefOid"]])

    def ejection_reason(self, pr: dict) -> str:
        """The failing queue check named for a PR that left the queue unmerged."""
        try:
            runs = json.loads(
                self.read(
                    [
                        "gh",
                        "run",
                        "list",
                        "--repo",
                        self.repo,
                        "--event",
                        "merge_group",
                        "--limit",
                        "50",
                        "--json",
                        "name,conclusion,headBranch,url",
                    ]
                )
            )
        except (LandStop, ValueError):
            runs = []
        marker = f"/pr-{self.number}-"
        failed = [
            run
            for run in runs
            if marker in run.get("headBranch", "")
            and run.get("conclusion") not in (None, "", "success", "skipped")
        ]
        if not failed:
            return "no failing queue check could be found; open the PR for details"
        return "failing check: " + ", ".join(
            f"{run['name']} ({run.get('url', 'no url')})" for run in failed
        )

    def settle(self, pr: dict) -> dict:
        """The PR once MERGED; LandStop if it is queued, ejected or closed."""
        if pr["state"] == "MERGED":
            return pr
        if not pr["hasMergeQueue"]:
            raise LandStop(
                f"after the merge command the PR is {pr['state']}, not MERGED; "
                "nothing was cleaned up. Re-run land.py once it has merged."
            )
        print(f"queue: {queue_summary(pr)}")
        if not self.wait:
            raise LandStop(
                f"PR #{self.number} is {queue_summary(pr)}; nothing was cleaned up. "
                f"Re-run land.py {self.number} --wait, or once it has merged.",
                QUEUED,
            )
        seen = pr.get("mergeQueueEntry") is not None
        waited = 0.0
        while True:
            if waited >= self.timeout_seconds:
                raise LandStop(
                    f"PR #{self.number} is still {queue_summary(pr)} after "
                    f"{int(waited)}s; re-run land.py to keep waiting",
                    QUEUED,
                )
            self.sleep(self.poll_seconds)
            waited += self.poll_seconds
            latest = self.pull_request()
            if latest["state"] == "MERGED":
                return latest
            if latest["state"] == "CLOSED":
                raise LandStop("the PR was closed without merging")
            entry = latest.get("mergeQueueEntry")
            if entry is not None:
                seen = True
                if entry != (pr.get("mergeQueueEntry") or {}):
                    print(f"queue: {queue_summary(latest)}")
            elif seen:
                raise LandStop(
                    f"PR #{self.number} left the merge queue without merging; "
                    f"{self.ejection_reason(latest)}"
                )
            pr = latest

    def fast_forward(self, main: Path, pr: dict) -> None:
        base = pr["baseRefName"]
        current = self.read(["git", "symbolic-ref", "--short", "HEAD"], main)
        if current != base:
            raise LandStop(f"the main checkout {main} is on {current}, not {base}")
        if self.read(["git", "status", "--porcelain", "--untracked-files=no"], main):
            raise LandStop(f"the main checkout {main} has uncommitted changes")
        self.mutate(["git", "fetch", self.remote, base], main)
        self.mutate(["git", "merge", "--ff-only", f"{self.remote}/{base}"], main)
        if not self.dry_run:
            merged = (pr.get("mergeCommit") or {}).get("oid")
            self.read(["git", "merge-base", "--is-ancestor", merged, "HEAD"], main)
            print(f"main checkout: {base} includes {merged}")

    def clean_up(self, main: Path, pr: dict) -> None:
        branch = pr["headRefName"]
        lane = next(
            (t for t in list_worktrees(self.runner, main)[1:] if t.branch == branch),
            None,
        )
        tip = self.runner(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=main,
            check=False,
        ).stdout.strip()
        if tip and tip != pr["headRefOid"]:
            raise LandStop(
                f"local {branch} is at {tip}, not the merged head {pr['headRefOid']}; "
                "it has work the PR did not include, so it is kept"
            )
        if lane is not None:
            if self.dry_run:
                blockers = removal_blockers(self.runner, lane.path, None)
                print(f"  would remove worktree {lane.path}")
                for blocker in blockers:
                    print(f"    but it would refuse: {blocker}")
            else:
                try:
                    remove_worktree(
                        self.runner,
                        main,
                        lane,
                        force=False,
                        delete_branch=False,
                        require_pushed=False,
                    )
                except WorktreeError as error:
                    raise LandStop(str(error)) from None
        if tip:
            self.mutate(["git", "branch", "-D", branch], main)
        if pr["isCrossRepository"]:
            return
        exists = self.runner(
            ["git", "ls-remote", "--exit-code", "--heads", self.remote, branch],
            cwd=main,
            check=False,
        )
        if exists.returncode == 0:
            self.mutate(["git", "push", self.remote, "--delete", branch], main)

    def land(self) -> int:
        pr = self.pull_request()
        main = list_worktrees(self.runner)[0].path
        print(
            f"PR #{self.number} {pr['headRefName']} -> {pr['baseRefName']} ({pr['state']})"
        )
        if pr["state"] == "CLOSED":
            raise LandStop("the PR is closed without merging")
        if pr["isDraft"]:
            raise LandStop("the PR is a draft")
        if pr["state"] == "OPEN" and pr.get("mergeQueueEntry") is not None:
            pr = self.settle(pr)
        elif pr["state"] == "OPEN":
            self.merge(pr)
            if self.dry_run:
                print("  would confirm the PR is MERGED, then:")
                self.fast_forward(main, {**pr, "mergeCommit": None})
                self.clean_up(main, pr)
                print(f"dry run: planned PR #{self.number}")
                return 0
            pr = self.settle(self.pull_request())
        print(f"merged: {(pr.get('mergeCommit') or {}).get('oid')}")
        self.fast_forward(main, pr)
        self.clean_up(main, pr)
        print(f"{'dry run: planned' if self.dry_run else 'landed'} PR #{self.number}")
        return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("number", type=int, help="pull request number")
    parser.add_argument(
        "--wait",
        action="store_true",
        help="with a merge queue, poll until the PR is merged or leaves the queue",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    parser.add_argument("--repo", help="OWNER/NAME (default: the main checkout's repo)")
    parser.add_argument("--remote", default="origin", help="git remote of the base")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)
    try:
        repo = (
            args.repo
            or run(
                [
                    "gh",
                    "repo",
                    "view",
                    "--json",
                    "nameWithOwner",
                    "-q",
                    ".nameWithOwner",
                ],
                cwd=list_worktrees(run)[0].path,
            ).stdout.strip()
        )
        lander = Lander(
            run,
            repo=repo,
            remote=args.remote,
            dry_run=args.dry_run,
            number=args.number,
            wait=args.wait,
        )
        return lander.land()
    except LandStop as stop:
        print(f"land.py: stopped: {stop}", file=sys.stderr)
        return stop.code
    except (WorktreeError, subprocess.CalledProcessError) as error:
        print(f"land.py: stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
