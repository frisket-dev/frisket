#!/usr/bin/env python3
"""Ratchet for ruff rules whose existing violations are frozen, not fixed.

Thresholds live in pyproject.toml ([tool.ruff.lint.mccabe] / [tool.ruff.lint.pylint]);
tests are exempt from the complexity rules via per-file-ignores there. This
script runs ruff with RULES selected and compares per-file, per-rule counts
against BASELINE. Any count above the baseline, or any offending file absent
from it, fails. Counts below the baseline pass; ``--update`` writes them down
and refuses to raise any count or add any file.

Fix a failure by restructuring the new code (split the function, pass a typed
object instead of more arguments, import at module top). A renamed file keeps
its counts: rename its key in the baseline by hand.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
BASELINE = REPO / "scripts/ci/ruff_ratchet_baseline.json"
RULES = ("C901", "PLC0415", "PLR0912", "PLR0913", "PLR0915")
PATHS = ("src", "tests")

Counts = dict[str, dict[str, int]]


def measure() -> Counts:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            *PATHS,
            "--select",
            ",".join(RULES),
            "--output-format",
            "json",
            "--exit-zero",
            "--no-cache",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    tally: Counter[tuple[str, str]] = Counter()
    for item in json.loads(proc.stdout):
        rel = Path(item["filename"]).resolve().relative_to(REPO).as_posix()
        tally[(item["code"], rel)] += 1
    counts: Counts = {rule: {} for rule in RULES}
    for (rule, rel), n in sorted(tally.items()):
        counts[rule][rel] = n
    return counts


def compare(baseline: Counts, current: Counts) -> tuple[list[str], list[str]]:
    """Return (increases, decreases) as human-readable lines."""
    increases: list[str] = []
    decreases: list[str] = []
    for rule in RULES:
        base = baseline.get(rule, {})
        cur = current.get(rule, {})
        for rel in sorted(set(base) | set(cur)):
            was, now = base.get(rel, 0), cur.get(rel, 0)
            if now > was:
                increases.append(f"{rel}: {rule} {now} > baseline {was}")
            elif now < was:
                decreases.append(f"{rel}: {rule} {now} < baseline {was}")
    return increases, decreases


def write(counts: Counts) -> None:
    payload = {rule: dict(sorted(files.items())) for rule, files in counts.items()}
    BASELINE.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="lower the baseline")
    args = parser.parse_args(argv)

    baseline: Counts = json.loads(BASELINE.read_text())
    current = measure()
    increases, decreases = compare(baseline, current)

    if increases:
        for line in increases:
            print(line)
        print(
            f"ruff ratchet: {len(increases)} count(s) above baseline. Restructure the "
            "new code (smaller functions, fewer parameters, module-top imports); "
            "see `ruff check <file> --select "
            + ",".join(RULES)
            + "`. The baseline only shrinks."
        )
        return 1
    if args.update:
        write(current)
        print(f"ruff ratchet: baseline lowered ({len(decreases)} entr(ies)).")
        return 0
    for line in decreases:
        print(line)
    total = sum(sum(files.values()) for files in current.values())
    note = " Run with --update to lock in." if decreases else ""
    print(f"ruff ratchet: ok ({total} frozen violations).{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
