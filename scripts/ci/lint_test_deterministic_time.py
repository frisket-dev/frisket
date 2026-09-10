#!/usr/bin/env python3
"""Deterministic-time lint: tests never touch real time primitives.

Rule: a negative assertion may never race a real clock, so raw ``time.sleep(`` /
``time.monotonic(`` / ``threading.Thread(`` are forbidden in tests. The
sanctioned routes are the ``controlled_time`` front door in
``tests/deterministic_time.py`` (exempt below — the one module allowed to
touch real time), the ``pytest.mark.realtime`` file tier (real-clock tests
making positive assertions only), or a line-level ``# realtime: <reason>``.

Existing violations are grandfathered in ALLOWLIST as {file: count} and may
only shrink: a new offending file, a count increase, a stale entry, or a
count decrease that was not ratcheted down here all fail. Heuristic and
false-negative-tolerant by design (a token inside a string literal counts;
suppress a legitimate one with the line marker).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TEST_DIRS = ("tests", "sidecar/tests")
HARNESS = {"tests/deterministic_time.py"}

TOKEN = re.compile(r"\btime\.sleep\(|\btime\.monotonic\(|\bthreading\.Thread\(")
LINE_MARKER = re.compile(r"#\s*realtime:\s*\S")
FILE_MARKER = re.compile(r"pytest\.mark\.realtime")

# Ratchet: counts only decrease; delete clean entries. Prefer controlled_time(),
# pytest.mark.realtime, or a line-level ``# realtime:`` reason.
ALLOWLIST: dict[str, int] = {}


def offending_lines(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    if FILE_MARKER.search(text):
        return []
    lines = text.splitlines()
    offenders: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        if not TOKEN.search(line):
            continue
        if LINE_MARKER.search(line) or (idx > 0 and LINE_MARKER.search(lines[idx - 1])):
            continue
        offenders.append((idx + 1, line.strip()))
    return offenders


def main() -> int:
    found: dict[str, list[tuple[int, str]]] = {}
    for test_dir in TEST_DIRS:
        root = REPO / test_dir
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if rel in HARNESS:
                continue
            offenders = offending_lines(path)
            if offenders:
                found[rel] = offenders

    failures: list[str] = []
    for rel, offenders in sorted(found.items()):
        allowed = ALLOWLIST.get(rel, 0)
        if len(offenders) > allowed:
            for lineno, text in offenders:
                print(f"{rel}:{lineno}: {text}")
            failures.append(
                f"{rel}: {len(offenders)} real-time line(s), allowlist grants "
                f"{allowed} — migrate to tests/deterministic_time.controlled_time, "
                "mark the file pytest.mark.realtime (positive assertions only), "
                "or annotate the line `# realtime: <reason>`"
            )
        elif len(offenders) < allowed:
            failures.append(
                f"{rel}: allowlist grants {allowed} but only {len(offenders)} "
                f"remain — ratchet the entry down to {len(offenders)}"
            )
    for rel in sorted(set(ALLOWLIST) - set(found)):
        failures.append(f"{rel}: allowlisted but clean (or gone) — delete its entry")

    if failures:
        print(
            "\ndeterministic-time lint failed: tests must use controlled time; "
            "real-clock tests are limited to positive latency/bound assertions:",
            file=sys.stderr,
        )
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
