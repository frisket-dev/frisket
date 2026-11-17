#!/usr/bin/env python3
"""Near-duplicate function ratchet over production Python.

Two checks share one normaliser (local names, parameter names and string
literals erased; docstrings dropped; structure, attributes and numbers kept):

* near: functions with at least MIN_STATEMENTS statements and MIN_LINES lines
  whose normalised bodies are identical form a group;
* private: a private helper (``_name``) defined with the same name and the same
  normalised body in MIN_MODULES or more modules, at any size.

Groups are recorded by member (``path::qualname``) in BASELINE. A group fails
when it is not contained in a recorded group: a new duplicate, or a recorded
group that gained a member. Fix it by calling the existing function instead of
copying it. ``--update`` rewrites the baseline only when every current group
is already contained in it, so the baseline only shrinks. A renamed file or
function keeps its entry: rename it in the baseline by hand.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
BASELINE = REPO / "scripts/ci/duplicate_functions_baseline.json"
ROOTS = ("src/frisket", "sidecar", "scripts")
SKIP_DIRS = {"__pycache__", "build", "dist", "node_modules", "tests"}
MIN_STATEMENTS = 3
MIN_LINES = 5
MIN_MODULES = 3

Groups = dict[str, list[list[str]]]
Function = ast.FunctionDef | ast.AsyncFunctionDef


def shape(node: object) -> str:
    """ast.dump-like text with local names, parameters and strings erased."""
    if isinstance(node, list):
        return "[" + ",".join(shape(item) for item in node) + "]"
    if isinstance(node, ast.Name):
        return "Name"
    if isinstance(node, ast.arg):
        return "arg"
    if isinstance(node, ast.Constant):
        return "S" if isinstance(node.value, str) else repr(node.value)
    if isinstance(node, ast.AST):
        fields = ",".join(shape(value) for _, value in ast.iter_fields(node))
        return f"{type(node).__name__}({fields})"
    return repr(node)


def body_without_docstring(node: Function) -> list[ast.stmt]:
    body = node.body
    first = body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        if isinstance(first.value.value, str):
            return body[1:]
    return body


def fingerprint(body: list[ast.stmt]) -> str:
    return hashlib.sha1(shape(body).encode()).hexdigest()


def python_files() -> Iterator[Path]:
    for root in ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            parts = path.relative_to(REPO).parts[:-1]
            if SKIP_DIRS.intersection(parts) or any(p.startswith(".") for p in parts):
                continue
            if path.name.startswith("test_") or path.name == "conftest.py":
                continue
            yield path


def functions(tree: ast.Module) -> Iterator[tuple[str, Function]]:
    def visit(body: list[ast.stmt], prefix: str) -> Iterator[tuple[str, Function]]:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield prefix + node.name, node
                yield from visit(node.body, f"{prefix}{node.name}.")
            elif isinstance(node, ast.ClassDef):
                yield from visit(node.body, f"{prefix}{node.name}.")

    yield from visit(tree.body, "")


def measure() -> Groups:
    near: dict[str, list[str]] = defaultdict(list)
    private: dict[tuple[str, str], list[str]] = defaultdict(list)
    for path in python_files():
        rel = path.relative_to(REPO).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        seen: dict[str, int] = defaultdict(int)
        for qualname, node in functions(tree):
            seen[qualname] += 1
            ident = f"{rel}::{qualname}"
            if seen[qualname] > 1:
                ident += f"#{seen[qualname]}"
            body = body_without_docstring(node)
            if not body:
                continue
            is_private = node.name.startswith("_") and not node.name.startswith("__")
            lines = (node.end_lineno or node.lineno) - node.lineno + 1
            is_large = lines >= MIN_LINES and (
                sum(isinstance(n, ast.stmt) for s in body for n in ast.walk(s))
                >= MIN_STATEMENTS
            )
            if not (is_private or is_large):
                continue
            digest = fingerprint(body)
            if is_private:
                private[(node.name, digest)].append(ident)
            if is_large:
                near[digest].append(ident)
    return {
        "near": sorted(sorted(g) for g in near.values() if len(g) > 1),
        "private": sorted(
            sorted(g)
            for g in private.values()
            if len({m.partition("::")[0] for m in g}) >= MIN_MODULES
        ),
    }


def regressions(baseline: Groups, current: Groups) -> list[str]:
    """Current groups not contained in any baseline group of the same kind."""
    failures: list[str] = []
    for kind, groups in current.items():
        recorded = [set(g) for g in baseline.get(kind, [])]
        for group in groups:
            members = set(group)
            if any(members <= known for known in recorded):
                continue
            overlap = max(
                recorded, key=lambda known: len(members & known), default=set()
            )
            new = sorted(members - overlap) if members & overlap else sorted(members)
            failures.append(
                f"{kind} duplicate group of {len(group)}: {', '.join(group)}"
                f"\n  not in baseline: {', '.join(new)}"
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="shrink the baseline")
    args = parser.parse_args(argv)

    baseline: Groups = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = measure()
    failures = regressions(baseline, current)
    if failures:
        print("\n".join(failures))
        print(
            f"check_duplicate_functions: {len(failures)} group(s) not in the baseline. "
            "Call the existing function (or extract one shared helper) instead of "
            "copying its body. The baseline only shrinks."
        )
        return 1
    if args.update:
        BASELINE.write_text(json.dumps(current, indent=1) + "\n", encoding="utf-8")
        print("check_duplicate_functions: baseline rewritten from current groups.")
        return 0
    sizes = {kind: len(groups) for kind, groups in current.items()}
    print(f"check_duplicate_functions: ok ({sizes}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
