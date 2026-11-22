"""Shrink-only per-file count baseline shared by the scripts/ci ratchets.

A baseline is a JSON object mapping repo-relative paths to a count. A file
whose current count exceeds its baseline entry (absent means zero) fails; a
lower count passes, and ``update`` writes the lower counts only when nothing
is above the baseline, so the baseline never grows.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

Counts = dict[str, int]


def compare(baseline: Counts, current: Counts) -> tuple[list[str], list[str]]:
    """Return (files above baseline, files below it)."""
    above = sorted(rel for rel, n in current.items() if n > baseline.get(rel, 0))
    below = sorted(rel for rel, n in baseline.items() if current.get(rel, 0) < n)
    return above, below


def slack(baseline: Counts, current: Counts) -> tuple[int, list[str]]:
    """Return (cleared baseline slots, sorted entries below baseline)."""
    _, below = compare(baseline, current)
    cleared = sum(baseline[rel] - current.get(rel, 0) for rel in below)
    return cleared, below


def read(path: Path) -> Counts:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, counts: Counts) -> None:
    path.write_text(
        json.dumps(dict(sorted(counts.items())), indent=1) + "\n", encoding="utf-8"
    )


def enforce(
    name: str,
    baseline: Counts,
    current: Counts,
    *,
    unit: str,
    fix: str,
    update: Callable[[Counts], None] | None,
    detail: Callable[[str], list[str]] = lambda rel: [],
    update_command: str | None = None,
) -> int:
    """Print the verdict for one ratchet and return its exit code.

    ``update`` (when the caller was asked to lower the baseline) receives the
    current counts once nothing is above the baseline. ``detail`` returns the
    lines describing a failing file's offences.
    """
    current = {rel: n for rel, n in current.items() if n}
    above, below = compare(baseline, current)
    if above:
        for rel in above:
            print(f"{rel}: {current[rel]} {unit} > baseline {baseline.get(rel, 0)}")
            for line in detail(rel):
                print(f"  {line}")
        print(
            f"{name}: {len(above)} file(s) above baseline. {fix} The baseline only shrinks."
        )
        return 1
    total = sum(current.values())
    baseline_total = sum(baseline.values())
    cleared = baseline_total - total
    if update is not None:
        update(current)
        print(
            f"{name}: baseline lowered ({baseline_total} -> {total} {unit}, "
            f"{cleared} baseline slot(s) cleared across {len(below)} file(s))."
        )
        return 0
    for rel in below:
        print(f"{rel}: {current.get(rel, 0)} < baseline {baseline[rel]}")
    if below:
        cmd = update_command or f"python scripts/ci/{name}.py --update"
        print(
            f"{name}: ok ({total} {unit} in {len(current)} files; "
            f"{cleared} baseline slot(s) cleared across {len(below)} file(s), "
            f"baseline still freezes {baseline_total}). Run `{cmd}` to lock in."
        )
    else:
        print(f"{name}: ok ({total} {unit} frozen in {len(current)} files).")
    return 0
