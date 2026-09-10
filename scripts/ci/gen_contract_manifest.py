#!/usr/bin/env python3
"""Regenerate sdk/contract-manifest.json from the Python contract source.

Python is the source of truth for the shipped plugin contract surface
(contract-manifest-generated-from-python-v1); the pure builder lives in
src/frisket/authoring/workbench/contract_manifest.py. This script is the file-writing
wrapper around it.

Usage:
    uv run python scripts/ci/gen_contract_manifest.py           # write the file
    uv run python scripts/ci/gen_contract_manifest.py --check   # freshness gate (CI)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from frisket.authoring.workbench.contract_manifest import (  # noqa: E402
    build_contract_manifest,
    render_contract_manifest,
)

MANIFEST_PATH = ROOT / "sdk" / "contract-manifest.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit nonzero if the committed manifest is stale relative to the "
        "Python contract source, without writing",
    )
    args = parser.parse_args()

    rendered = render_contract_manifest(build_contract_manifest())

    if args.check:
        committed = (
            MANIFEST_PATH.read_text(encoding="utf-8")
            if MANIFEST_PATH.exists()
            else None
        )
        if committed != rendered:
            sys.stderr.write(
                f"{MANIFEST_PATH} is stale relative to the Python contract source "
                "(src/frisket/authoring/workbench/contracts.py, src/frisket/contracts/plugin.py). "
                "Run `uv run python scripts/ci/gen_contract_manifest.py` to regenerate "
                "and commit the diff.\n"
            )
            return 1
        print(f"{MANIFEST_PATH} is fresh")
        return 0

    MANIFEST_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {MANIFEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
