#!/usr/bin/env python3
"""Keep container-type re-guards out of the action definition prechecks.

Every registered ``ActionPrecheck`` receives an ``ActionSpec`` that pydantic
has already validated, so ``params`` arrives typed ``dict[str, Any]`` — a
``isinstance(params, dict)`` check inside a precheck is unreachable and its
refusal branch is dead wire behavior (the real refusal is
``invalid_action_spec`` at the envelope). WARM-01 deleted the 82 such guards;
this gate keeps them deleted. The shared partial-row validator
``ingest/row_validation.py::precheck_import_rows_params`` is outside the
``_precheck_*`` naming scope this gate walks, so its guard stays untouched.

Structural (``ast``), not a grep: a mention in a docstring is not a guard.
This lives outside pytest because a source-text check is not a test.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
DEFINITIONS = REPO / "src" / "frisket" / "contracts" / "actions" / "definitions"

GUARDED_SUBJECTS = {"params", "action.params"}


def container_guards(fn: ast.FunctionDef) -> list[int]:
    """Line numbers of ``isinstance(params|action.params, dict)`` calls."""
    hits: list[int] = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and len(node.args) == 2
            and ast.unparse(node.args[0]) in GUARDED_SUBJECTS
            and "dict"
            in {n.id for n in ast.walk(node.args[1]) if isinstance(n, ast.Name)}
        ):
            hits.append(node.lineno)
    return hits


def main() -> int:
    failures: list[str] = []
    for path in sorted(DEFINITIONS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for stmt in tree.body:
            if isinstance(stmt, ast.FunctionDef) and stmt.name.startswith("_precheck_"):
                for lineno in container_guards(stmt):
                    rel = path.relative_to(REPO)
                    failures.append(
                        f"{rel}:{lineno}: {stmt.name} re-guards the params container"
                    )

    if failures:
        print(
            "precheck container-guard gate failed — registered ActionPrechecks "
            "start from the typed ActionSpec (params is dict[str, Any]); do not "
            "revalidate the container:",
            file=sys.stderr,
        )
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
