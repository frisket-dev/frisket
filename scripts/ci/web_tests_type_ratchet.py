#!/usr/bin/env python3
"""Ratchet for TypeScript errors in the web test suite (web/tsconfig.tests.json).

Vitest and Playwright transpile without type-checking, so a test calling a
fixture with a stale signature still runs. This script type-checks the tests
(``npm run tests:typecheck`` in web/) and compares per-file error counts with
BASELINE. A count above the baseline, or an error in a file absent from it,
fails; ``--update`` lowers the baseline and refuses to raise it.

Fix a failure by correcting the test's types (update the call to the current
signature, type the fixture), not with a cast or ``@ts-expect-error``; the
suppression budget counts those. A renamed file keeps its count: rename its
key in the baseline by hand.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import file_count_ratchet

REPO = Path(__file__).resolve().parent.parent.parent
WEB = REPO / "web"
BASELINE = REPO / "scripts/ci/web_tests_type_baseline.json"
ERROR = re.compile(
    r"^(?P<file>[^\s(][^(]*)\(\d+,\d+\): error (?P<code>TS\d+): (?P<msg>.*)$"
)


def measure() -> tuple[dict[str, list[str]], str]:
    """Return ({repo-relative file: [error lines]}, raw tsc output)."""
    proc = subprocess.run(
        ["npm", "run", "--silent", "tests:typecheck", "--", "--pretty", "false"],
        cwd=WEB,
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    errors: dict[str, list[str]] = {}
    for line in output.splitlines():
        match = ERROR.match(line)
        if match:
            rel = (WEB / match["file"]).resolve().relative_to(REPO).as_posix()
            errors.setdefault(rel, []).append(line)
    if proc.returncode not in (0, 1, 2) or (proc.returncode and not errors):
        raise SystemExit(f"web_tests_type_ratchet: tsc did not run cleanly:\n{output}")
    return errors, output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="lower the baseline")
    parser.add_argument("--totals", action="store_true", help="print top error codes")
    args = parser.parse_args(argv)
    errors, _ = measure()
    if args.totals:
        codes = Counter(
            ERROR.match(e)["code"] for lines in errors.values() for e in lines
        )
        for code, n in codes.most_common(15):
            print(f"{code:8} {n}")
    return file_count_ratchet.enforce(
        "web_tests_type_ratchet",
        file_count_ratchet.read(BASELINE),
        {rel: len(lines) for rel, lines in errors.items()},
        unit="type error(s)",
        fix=(
            "Correct the test's types (call the current signature, type the fixture) "
            "rather than casting; see `npm run tests:typecheck` in web/."
        ),
        update=(lambda counts: file_count_ratchet.write(BASELINE, counts))
        if args.update
        else None,
        detail=lambda rel: errors[rel][:20],
    )


if __name__ == "__main__":
    sys.exit(main())
