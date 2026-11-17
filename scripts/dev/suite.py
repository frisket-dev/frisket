#!/usr/bin/env python3
"""Run the Python suite: one full run per machine, failures classified.

  suite.py [--wait] [--known FILE]            full suite, machine-wide lock
  suite.py -- tests/engine -k foo             targeted run, no lock, -n <= 4

The full suite takes an exclusive lock file (pid + worktree) under the user
cache directory; a second full run refuses, or waits with ``--wait``. A lock
whose pid is no longer alive is stale and is taken over. Workers are sized
from CPU count minus the current load average (at most 8; targeted at most 4).

Tests run on the main checkout's ``.venv`` with ``PYTHONPATH=<worktree>/src``,
so this works unchanged from any lane worktree. Output is teed to a log, and
the run is classified against a known-failures file (default
``scripts/dev/known_local_failures.txt`` when present): new failures, known
failures, and known failures that no longer fail. The exit status is non-zero
only for new failures or when pytest itself could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from worktree import WorktreeError, main_checkout, main_python

FULL_CAP = 8
TARGETED_CAP = 4
WAIT_SECONDS = 15
KNOWN_DEFAULT = Path("scripts/dev/known_local_failures.txt")
PYTEST_FLAGS = ("-q", "--dist", "loadfile", "-p", "no:cacheprovider", "-rfE")


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "frisket"


def lock_path() -> Path:
    return cache_dir() / "suite.lock"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_holder(text: str, alive: Callable[[int], bool]) -> Optional[dict]:
    """The live holder recorded in a lock file's text, or None when it is stale."""
    try:
        holder = json.loads(text)
        pid = int(holder["pid"])
    except (ValueError, KeyError, TypeError):
        return None
    return holder if pid > 0 and alive(pid) else None


def try_lock(path: Path, worktree: Path, alive: Callable[[int], bool] = pid_alive):
    """Take the lock; return None on success or the live holder's record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps(
        {
            "pid": os.getpid(),
            "worktree": str(worktree),
            "started": datetime.now().isoformat(),
        }
    )
    for _attempt in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                text = path.read_text()
            except FileNotFoundError:
                continue
            holder = lock_holder(text, alive)
            if holder is not None:
                return holder
            # A crashed run's lock: its pid is gone, so nothing else can be
            # writing the suite. Remove only the text we judged stale.
            if path.read_text() == text:
                path.unlink()
            continue
        with os.fdopen(descriptor, "w") as handle:
            handle.write(record)
        return None
    return {"pid": "?", "worktree": "unknown (lock contended)"}


def release_lock(path: Path) -> None:
    try:
        holder = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return
    if holder.get("pid") == os.getpid():
        path.unlink()


def worker_count(cpus: Optional[int], load: float, cap: int) -> int:
    return max(1, min(cap, int((cpus or 1) - load)))


def read_known(path: Path) -> dict[str, str]:
    """Node id -> diagnosis; ``#`` starts a comment, blank lines are ignored."""
    known: dict[str, str] = {}
    for line in path.read_text().splitlines():
        node, _, note = line.partition(" #")
        node = node.strip()
        if node and not node.startswith("#"):
            known[node] = note.strip()
    return known


@dataclass
class Outcomes:
    failed: set[str] = field(default_factory=set)
    passed: set[str] = field(default_factory=set)


def junit_outcomes(xml_text: str) -> Outcomes:
    """Node ids by outcome from pytest's xunit1 JUnit report."""
    outcomes = Outcomes()
    for case in ElementTree.fromstring(xml_text).iter("testcase"):
        file = case.get("file") or ""
        module = file[: -len(".py")].replace("/", ".") if file.endswith(".py") else ""
        classname = case.get("classname") or ""
        classes = (
            classname[len(module) + 1 :] if classname.startswith(module + ".") else ""
        )
        node = "::".join(
            [file, *filter(None, classes.split(".")), case.get("name") or ""]
        )
        if case.find("failure") is not None or case.find("error") is not None:
            outcomes.failed.add(node)
        elif case.find("skipped") is None:
            outcomes.passed.add(node)
    outcomes.passed -= outcomes.failed
    return outcomes


@dataclass
class Classification:
    new: list[str]
    known: list[str]
    fixed: list[str]


def classify(outcomes: Outcomes, known: Iterable[str]) -> Classification:
    known = set(known)
    return Classification(
        new=sorted(outcomes.failed - known),
        known=sorted(outcomes.failed & known),
        fixed=sorted(outcomes.passed & known),
    )


def report(
    result: Classification, known: dict[str, str], source: Optional[Path]
) -> str:
    against = f" (known list: {source})" if source else " (no known-failures list)"
    lines = [f"Classification{against}:"]
    sections = (
        ("NEW failures", result.new),
        ("known failures", result.known),
        ("known, now passing (delete from the list)", result.fixed),
    )
    for title, nodes in sections:
        lines.append(f"  {title}: {len(nodes)}")
        lines.extend(
            f"    {node}" + (f"  # {known[node]}" if known.get(node) else "")
            for node in nodes
        )
    return "\n".join(lines)


def tee(command: list[str], *, cwd: Path, env: dict, log: Path) -> int:
    with (
        log.open("w") as sink,
        subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ) as process,
    ):
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sink.write(line)
    return process.returncode


def acquire(path: Path, worktree: Path, *, wait: bool) -> bool:
    while True:
        holder = try_lock(path, worktree)
        if holder is None:
            return True
        message = (
            f"another full suite run holds {path}: pid {holder.get('pid')} "
            f"in {holder.get('worktree')} since {holder.get('started', '?')}"
        )
        if not wait:
            print(f"suite.py: {message}; re-run with --wait to queue", file=sys.stderr)
            return False
        print(f"suite.py: waiting: {message}", file=sys.stderr)
        time.sleep(WAIT_SECONDS)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--wait", action="store_true", help="queue behind a running full suite"
    )
    parser.add_argument(
        "--known", type=Path, help="known-failures file to classify against"
    )
    parser.add_argument(
        "--log", type=Path, help="log path (default: under the user cache dir)"
    )
    parser.add_argument(
        "-n", type=int, dest="workers", help="override the worker count"
    )
    parser.add_argument(
        "pytest_args", nargs="*", help="after --: a targeted pytest selection"
    )
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)

    worktree = Path.cwd()
    try:
        worktree = Path(
            subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        python = main_python(main_checkout())
    except (subprocess.CalledProcessError, WorktreeError) as error:
        print(
            f"suite.py: run inside a checkout of this repository ({error})",
            file=sys.stderr,
        )
        return 2
    if not python.exists():
        print(f"suite.py: main checkout venv missing: {python}", file=sys.stderr)
        return 2

    targeted = bool(args.pytest_args)
    cap = TARGETED_CAP if targeted else FULL_CAP
    workers = (
        min(cap, args.workers)
        if args.workers
        else worker_count(os.cpu_count(), os.getloadavg()[0], cap)
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log = args.log or cache_dir() / f"suite-{worktree.name}-{stamp}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    junit = log.with_suffix(".xml")
    known_path = args.known or (worktree / KNOWN_DEFAULT)
    known = read_known(known_path) if known_path.is_file() else {}

    lock = lock_path()
    if not targeted and not acquire(lock, worktree, wait=args.wait):
        return 3
    try:
        command = [
            str(python),
            "-m",
            "pytest",
            *PYTEST_FLAGS,
            "-n",
            str(workers),
            f"--junitxml={junit}",
            "-o",
            "junit_family=xunit1",
            *args.pytest_args,
        ]
        env = {**os.environ, "PYTHONPATH": str(worktree / "src")}
        print(f"suite.py: {' '.join(command)}\nsuite.py: log {log}", flush=True)
        status = tee(command, cwd=worktree, env=env, log=log)
    finally:
        if not targeted:
            release_lock(lock)

    if status not in (0, 1) or not junit.is_file():
        print(f"suite.py: pytest exited {status} without a classifiable run; see {log}")
        return status or 2
    result = classify(junit_outcomes(junit.read_text()), known)
    summary = report(result, known, known_path if known else None)
    print(summary)
    with log.open("a") as sink:
        sink.write(summary + "\n")
    return 1 if result.new else 0


if __name__ == "__main__":
    raise SystemExit(main())
