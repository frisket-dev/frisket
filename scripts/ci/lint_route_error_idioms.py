#!/usr/bin/env python3
"""Keep per-handler HTTP error mapping out of ``server/routes``.

A route raises through its service: ``RouteError`` or a typed error mapped
once with ``register_typed_error`` renders the response. Two idioms bypass
that and are counted here per route function:

- ``raise HTTPException(...)`` inside an ``except`` block (an exception type
  mapped to an HTTP shape at the call site);
- ``return JSONResponse(status_code=...)`` (a hand-built error response).

The allowlist names what is deliberately left (broad ``ValueError`` /
``KeyError`` mappings that cannot be app-wide handlers, and responses forked
from another service's status) and only shrinks: a new offender or a higher
count fails, and so does an entry that no longer offends. ``--update`` lowers
or drops entries; it never adds one.

Structural (``ast``), not a grep. This lives outside pytest because a
source-text check is not a test.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
ROUTES = REPO / "src" / "frisket" / "server" / "routes"
ALLOWLIST = Path(__file__).with_name("lint_route_error_idioms_allowlist.json")


def _is_call_to(node: ast.AST | None, name: str) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == name) or (
        isinstance(func, ast.Attribute) and func.attr == name
    )


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.handler_depth = 0
        self.hits: Counter[str] = Counter()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        depth, self.handler_depth = self.handler_depth, 0
        self.generic_visit(node)
        self.handler_depth = depth
        self.scope.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.handler_depth += 1
        self.generic_visit(node)
        self.handler_depth -= 1

    def _record(self, pattern: str) -> None:
        self.hits[f"{'.'.join(self.scope) or '<module>'}: {pattern}"] += 1

    def visit_Raise(self, node: ast.Raise) -> None:
        if self.handler_depth and _is_call_to(node.exc, "HTTPException"):
            self._record("except -> raise HTTPException")
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if _is_call_to(node.value, "JSONResponse") and any(
            keyword.arg == "status_code"
            for keyword in node.value.keywords  # type: ignore[union-attr]
        ):
            self._record("return JSONResponse(status_code=)")
        self.generic_visit(node)


def offenders(root: Path = ROUTES) -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        visitor = _Visitor()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        rel = path.relative_to(REPO).as_posix()
        for key, count in visitor.hits.items():
            found[f"{rel}::{key}"] = count
    return found


def compare(found: dict[str, int], allowed: dict[str, int]) -> list[str]:
    problems = []
    for key, count in sorted(found.items()):
        limit = allowed.get(key, 0)
        if count > limit:
            problems.append(f"{key} x{count} (allowlist: {limit})")
    for key, limit in sorted(allowed.items()):
        if found.get(key, 0) < limit:
            problems.append(
                f"{key} x{found.get(key, 0)} no longer offends at x{limit}; "
                "shrink the allowlist (--update)"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args(argv)
    allowed: dict[str, int] = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    found = offenders()
    if args.update:
        shrunk = {
            key: min(limit, found[key])
            for key, limit in allowed.items()
            if found.get(key, 0) > 0
        }
        ALLOWLIST.write_text(json.dumps(shrunk, indent=1, sort_keys=True) + "\n")
        allowed = shrunk
    problems = compare(found, allowed)
    if problems:
        print(
            "route error idiom gate failed: map an exception type once with "
            "register_typed_error, or raise RouteError from the service.",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"lint_route_error_idioms: ok ({sum(found.values())} allowlisted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
