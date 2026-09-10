#!/usr/bin/env python3
"""Refuse the retired documentation tree and internal-corpus paths.

This repo started from a clean export; the development corpus lives in a
private repo. If any of these paths reappears here (a stray commit from
an old checkout, a tool re-creating a scratch file), CI goes red before
the content lands on anyone's clone.
"""

from __future__ import annotations

import subprocess

FORBIDDEN_PREFIXES = (
    "docs/",
    ".architecture-evaluation",
    "design_handoff",
)
FORBIDDEN_FILES = (
    "RESEARCH.md",
    "DESIGN.md",
)


def main() -> int:
    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    bad = [
        p for p in tracked if p in FORBIDDEN_FILES or p.startswith(FORBIDDEN_PREFIXES)
    ]
    if bad:
        print("internal-corpus paths must not exist in this repository:")
        for p in bad:
            print(f"  {p}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
