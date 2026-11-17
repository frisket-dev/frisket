#!/usr/bin/env python3
"""Type-checker ratchet: pyright errors in src/frisket only shrink, per file.

Runs PYRIGHT (pinned; configuration in pyrightconfig.json, basic mode) against
the interpreter running this script and compares per-file error counts with
BASELINE. A count above the baseline, or an error in a file absent from it,
fails. ``--update`` lowers the baseline and refuses to raise any count.

Counts depend on the checker version, so BASELINE records it. After bumping
PYRIGHT, the old counts are not comparable: ``--update`` then rewrites the
baseline from the new version's output, and that rewrite ships in the same
commit as the bump. Counts also depend on which optional extras are
installed, so the gate measures the CI environment (``uv sync --dev --extra
entities``) and refuses an interpreter missing a distribution from
EXTRAS. Unresolved imports outside those extras are not errors.

Fix a failure by typing the code correctly (narrow the Optional, annotate the
parameter, return the declared model), not with ``# type: ignore``; the
suppression budget counts those. A renamed file keeps its count: rename its
key in the baseline by hand.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import tomllib

import file_count_ratchet

REPO = Path(__file__).resolve().parent.parent.parent
BASELINE = REPO / "scripts/ci/type_ratchet_baseline.json"
PYRIGHT_VERSION = "1.1.414"
PYRIGHT = ["uvx", "--from", f"pyright[nodejs]=={PYRIGHT_VERSION}", "pyright"]
EXTRAS = ("entities",)


def missing_extras() -> list[str]:
    """Distributions of EXTRAS (per pyproject.toml) this interpreter lacks."""
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    optional = project["project"]["optional-dependencies"]
    missing = []
    for extra in EXTRAS:
        for requirement in optional[extra]:
            name = re.match(r"[A-Za-z0-9._-]+", requirement).group()
            try:
                importlib.metadata.distribution(name)
            except importlib.metadata.PackageNotFoundError:
                missing.append(f"{name} (extra {extra!r})")
    return missing


def measure() -> list[dict]:
    """Pyright's error diagnostics, file paths made repo-relative."""
    proc = subprocess.run(
        [
            *PYRIGHT,
            "--outputjson",
            "--pythonpath",
            sys.executable,
            "-p",
            "pyrightconfig.json",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise SystemExit(
            f"type_ratchet: pyright produced no report (exit {proc.returncode}):\n"
            + proc.stdout[-4000:]
            + proc.stderr[-4000:]
        ) from None
    errors = [d for d in report["generalDiagnostics"] if d["severity"] == "error"]
    for diagnostic in errors:
        diagnostic["file"] = (
            Path(diagnostic["file"]).resolve().relative_to(REPO).as_posix()
        )
    return errors


def describe(diagnostic: dict) -> str:
    line = diagnostic["range"]["start"]["line"] + 1
    message = diagnostic["message"].splitlines()[0]
    return (
        f"{diagnostic['file']}:{line}: [{diagnostic.get('rule', '')}] {message[:160]}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="lower the baseline")
    parser.add_argument("--totals", action="store_true", help="print top rules/files")
    args = parser.parse_args(argv)

    missing = missing_extras()
    if missing:
        print(
            "type_ratchet: the baseline is measured with the CI extras installed; "
            f"this interpreter lacks {', '.join(missing)}. Run it from an environment "
            "synced with `uv sync --dev --extra entities`."
        )
        return 1
    recorded = json.loads(BASELINE.read_text(encoding="utf-8"))
    errors = measure()
    current = Counter(d["file"] for d in errors)
    if args.totals:
        for rule, n in Counter(d.get("rule", "") for d in errors).most_common(10):
            print(f"{rule:32} {n}")
        for rel, n in current.most_common(10):
            print(f"{rel:70} {n}")

    def lower(counts: dict[str, int]) -> None:
        payload = {"pyright": PYRIGHT_VERSION, "files": dict(sorted(counts.items()))}
        BASELINE.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")

    if recorded["pyright"] != PYRIGHT_VERSION:
        if not args.update:
            print(
                f"type_ratchet: baseline was recorded with pyright {recorded['pyright']}, "
                f"the gate pins {PYRIGHT_VERSION}. Run with --update in the commit that "
                "bumps the pin."
            )
            return 1
        lower(dict(current))
        print(f"type_ratchet: baseline re-recorded for pyright {PYRIGHT_VERSION}.")
        return 0
    return file_count_ratchet.enforce(
        "type_ratchet",
        recorded["files"],
        dict(current),
        unit="type error(s)",
        fix=(
            "Type the code correctly (narrow the Optional, annotate the parameter, "
            "return the declared model) instead of adding `# type: ignore`; run "
            "`python scripts/ci/type_ratchet.py` locally to see the errors."
        ),
        update=lower if args.update else None,
        detail=lambda rel: [describe(d) for d in errors if d["file"] == rel][:20],
    )


if __name__ == "__main__":
    sys.exit(main())
