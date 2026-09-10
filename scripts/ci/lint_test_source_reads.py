#!/usr/bin/env python3
"""Lint: tests never read repository source as text.

Scans tests/ and sidecar/tests/ for reads (read_text/read_bytes/open) whose
path resolves under src/, web/, scripts/, .github/, Dockerfile, or
docker-compose*. Heuristic and false-negative-tolerant by design; a legitimate
survivor (executes the artifact, or diffs two independently maintained
sources) suppresses a finding with `# rule19: <reason>` on the offending line
or the line above.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TEST_DIRS = ("tests", "sidecar/tests")

# Source-tree path literals and build manifests.
SOURCE_TOKEN = re.compile(
    r"""["'](?:\.{0,2}/)*(?:src|web|scripts|\.github)["'/]"""
    r"""|["'][^"']*(?:Dockerfile|docker-compose)"""
)
READ_CALL = re.compile(r"\.read_text\(|\.read_bytes\(|(?<![\w.])open\(")
RECEIVER = re.compile(
    r"([A-Za-z_]\w*)\s*\.\s*read_(?:text|bytes)\(|open\(\s*([A-Za-z_]\w*)"
)
ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)")
FOR_TARGET = re.compile(r"^\s*(?:async\s+)?for\s+([A-Za-z_]\w*)\s+in\s+(.*)")
MODULE_FILE = re.compile(r"\b[A-Za-z_]\w*\s*\.\s*__file__\b")
MARKER = re.compile(r"#\s*rule19:\s*\S")


def scan_file(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    offenders: list[tuple[int, str]] = []
    tainted: set[str] = set()
    for idx, line in enumerate(lines):
        sourcey = bool(SOURCE_TOKEN.search(line) or MODULE_FILE.search(line))
        assign = ASSIGN.match(line)
        if assign:
            # Join balanced continuations, with a bounded scan.
            rhs = line.split("=", 1)[1]
            depth = rhs.count("(") + rhs.count("[") - rhs.count(")") - rhs.count("]")
            for cont in lines[idx + 1 : idx + 8]:
                if depth <= 0:
                    break
                rhs += " " + cont
                depth += cont.count("(") + cont.count("[")
                depth -= cont.count(")") + cont.count("]")
            rhs_names = set(re.findall(r"[A-Za-z_]\w*", rhs))
            if (
                SOURCE_TOKEN.search(rhs)
                or MODULE_FILE.search(rhs)
                or (rhs_names & tainted)
            ):
                tainted.add(assign.group(1))
        loop = FOR_TARGET.match(line)
        if loop:
            iterable = loop.group(2)
            iterable_names = set(re.findall(r"[A-Za-z_]\w*", iterable))
            if (
                SOURCE_TOKEN.search(iterable)
                or MODULE_FILE.search(iterable)
                or (iterable_names & tainted)
            ):
                tainted.add(loop.group(1))
        if not READ_CALL.search(line):
            continue
        receivers = {m.group(1) or m.group(2) for m in RECEIVER.finditer(line)}
        receivers.discard(None)
        if receivers:
            hit = sourcey or (receivers & tainted)
        else:
            # Complex receiver: fall back to tainted names on the line.
            used_names = set(re.findall(r"[A-Za-z_]\w*", line))
            hit = sourcey or (used_names & tainted)
        if not hit:
            continue
        if MARKER.search(line) or (idx > 0 and MARKER.search(lines[idx - 1])):
            continue
        offenders.append((idx + 1, line.strip()))
    return offenders


def main() -> int:
    failures = 0
    for test_dir in TEST_DIRS:
        root = REPO / test_dir
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            for lineno, text in scan_file(path):
                rel = path.relative_to(REPO)
                print(f"{rel}:{lineno}: {text}")
                failures += 1
    if failures:
        print(
            f"\n{failures} test line(s) read repository source as text.\n"
            "Legitimate survivors (executes the artifact, or diffs two "
            "independently maintained sources) add `# rule19: <reason>` "
            "on the line or the line above.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
