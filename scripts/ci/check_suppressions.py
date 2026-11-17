#!/usr/bin/env python3
"""Suppression budget: lint/type/coverage suppressions and debt skips only shrink.

Counts, per file and per kind, the comments and markers that silence a check
instead of satisfying it (KINDS below) across Python (source_scan.PY_ROOTS)
and JavaScript/TypeScript (source_scan.JS_ROOTS), and compares them with
BASELINE. Any count above the baseline, or any offending file absent from it,
fails; ``.only`` (HARD_BAN) is never allowed. ``--update`` lowers the baseline
and refuses to raise any count or add any file.

Test skips are classified. A skip whose condition probes the environment
(``importorskip``, ``shutil.which``, ``find_spec``, platform, environment
variables, an optional import that failed, or a helper whose name or body does
one of those) is "environment" and is allowed freely: tests self-skip when an
optional extra is absent. An unconditional skip/xfail, one with a constant
condition, or one whose condition probes nothing recognisable is debt and
counts against the budget.

Fix a failure by satisfying the check (type the value, handle the exception,
fix the test) rather than silencing it. A renamed file keeps its counts:
rename its key in the baseline by hand.
"""

from __future__ import annotations

import argparse
import ast
import functools
import json
import re
import sys
from collections import Counter

from source_scan import (
    JS_ROOTS,
    JS_SUFFIXES,
    PY_ROOTS,
    PY_SUFFIXES,
    REPO,
    js_code_lines,
    js_comments,
    python_comments,
    tracked_files,
)

BASELINE = REPO / "scripts/ci/suppressions_baseline.json"

PY_COMMENT_KINDS = {
    "py-noqa": re.compile(r"#\s*noqa\s*:\s*[A-Z]+[0-9]+", re.IGNORECASE),
    "py-noqa-bare": re.compile(r"#\s*noqa\b(?!\s*:\s*[A-Z]+[0-9]+)", re.IGNORECASE),
    "py-type-ignore": re.compile(r"#\s*type:\s*ignore\b"),
    "py-pyright-ignore": re.compile(r"#\s*pyright:\s*ignore\b"),
    "py-pragma-no-cover": re.compile(r"#\s*pragma:\s*no\s*cover\b"),
    "py-fmt-off": re.compile(r"#\s*fmt:\s*(off|skip)\b"),
}
PY_ANY_COMMENT_KIND = re.compile(
    "|".join(p.pattern for p in PY_COMMENT_KINDS.values()), re.IGNORECASE
)
JS_COMMENT_KINDS = {
    "js-eslint-disable": re.compile(r"\beslint-disable"),
    "ts-ignore": re.compile(r"@ts-ignore\b"),
    "ts-expect-error": re.compile(r"@ts-expect-error\b"),
    "ts-nocheck": re.compile(r"@ts-nocheck\b"),
}
JS_CODE_KINDS = {
    "ts-as-any": re.compile(r"\bas\s+any\b"),
    "ts-as-unknown-as": re.compile(r"\bas\s+unknown\s+as\b"),
    "ts-as-never": re.compile(r"\bas\s+never\b"),
    "js-test-only": re.compile(r"\b(?:it|describe|test|suite)\.only\b"),
    "js-test-todo": re.compile(r"\b(?:it|describe|test|suite)\.todo\b"),
}
JS_SKIP = re.compile(r"\b(?:it|describe|test|suite)\.skip(If)?\b")
HARD_BAN = {"js-test-only"}
KINDS = (
    *PY_COMMENT_KINDS,
    "py-skip-debt",
    *JS_COMMENT_KINDS,
    *JS_CODE_KINDS,
    "js-skip-debt",
)

# Text that marks a skip condition as an environment probe. Names are resolved
# through assignments, same-module helpers and repo imports before matching.
PROBE = re.compile(
    r"importorskip|find_spec|\bwhich\b|sys\.platform|os\.name\b|\bplatform\b"
    r"|environ|getenv|version_info|geteuid|getuid|sysconfig|machine\(\)"
    r"|(?i:available|installed|provisioned|present|enabled|supported|missing)"
    r"|(?:\b|_)(has|have|can|requires?|needs?)_\w+|\b_?(HAS|HAVE)_\w+|_URL\b"
    r"|\.exists\(|\.is_file\(|\.is_dir\(|\bhasattr\((os|signal|socket|sys|time)\b"
    r"|if_nameindex"
    r"|\b(posix|nt|win32|darwin|linux)\b"
)
ENV_EXCEPTIONS = {
    "ImportError",
    "ModuleNotFoundError",
    "OSError",
    "PermissionError",
    "FileNotFoundError",
    "NotImplementedError",
    "EnvironmentError",
}
JS_PROBE = re.compile(
    r"process\.(env|platform|arch|versions)|\bhas[A-Z]\w*\(|browserName"
    r"|isWindows|isMac|isLinux|(?i:available|installed)"
)

Counts = dict[str, dict[str, int]]


class _Resolver:
    """Expands names in a skip condition into the source text that defines them."""

    def __init__(self, tree: ast.Module) -> None:
        self.defs: dict[str, list[ast.AST]] = {}
        self.optional_imports: set[str] = set()
        self.imports: dict[str, tuple[str, str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.defs.setdefault(node.name, []).append(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value:
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name):
                            self.defs.setdefault(name.id, []).append(node.value)
            elif isinstance(node, ast.Try):
                if any(_handler_is_env(h) for h in node.handlers):
                    for stmt in node.body:
                        for alias in getattr(stmt, "names", []):
                            bound = alias.asname or alias.name.split(".")[0]
                            self.optional_imports.add(bound)
                    for h in node.handlers:
                        for stmt in ast.walk(h):
                            if isinstance(stmt, ast.Assign):
                                for t in stmt.targets:
                                    if isinstance(t, ast.Name):
                                        self.optional_imports.add(t.id)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = (node.module, alias.name)

    def text(self, node: ast.AST, depth: int = 3) -> str:
        parts = [ast.unparse(node)]
        if depth == 0:
            return parts[0]
        for name in {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}:
            if name in self.optional_imports:
                parts.append("installed")
            for definition in self.defs.get(name, [])[:4]:
                parts.append(self.text(definition, depth - 1))
            if name in self.imports and name not in self.defs:
                parts.append(_imported_source(*self.imports[name]))
        return "\n".join(parts)


@functools.cache
def _module_body(module: str) -> tuple[ast.stmt, ...]:
    if not module.startswith(("frisket", "tests")):
        return ()
    base = (
        REPO
        / ("src" if module.startswith("frisket") else "")
        / module.replace(".", "/")
    )
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            try:
                return tuple(ast.parse(path.read_text(encoding="utf-8")).body)
            except (SyntaxError, ValueError):
                return ()
    return ()


@functools.cache
def _imported_source(module: str, name: str) -> str:
    for node in _module_body(module):
        if getattr(node, "name", None) == name:
            return ast.unparse(node)
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.unparse(node.value)
    return ""


def _handler_is_env(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return False
    names = {
        n.id if isinstance(n, ast.Name) else n.attr
        for n in ast.walk(handler.type)
        if isinstance(n, (ast.Name, ast.Attribute))
    }
    return bool(names & ENV_EXCEPTIONS)


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _condition_kind(cond: ast.AST | None, resolver: _Resolver) -> str:
    if (
        cond is None
        or isinstance(cond, ast.Constant)
        and not isinstance(cond.value, str)
    ):
        return "debt"
    if isinstance(cond, ast.Constant):
        return "environment" if PROBE.search(cond.value) else "debt"
    return "environment" if PROBE.search(resolver.text(cond)) else "debt"


def python_skips(tree: ast.Module) -> Counter[str]:
    """Counter of skip classifications: "debt" and "environment"."""
    resolver = _Resolver(tree)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    tally: Counter[str] = Counter()
    seen_marks: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name.endswith("importorskip"):
                tally["environment"] += 1
            elif name in ("pytest.skip", "pytest.xfail", "skip", "xfail") and (
                name.startswith("pytest")
                or "pytest" in resolver.imports.get(name, ("",))[0]
            ):
                tally[_guard_kind(node, parents, resolver)] += 1
            elif name.endswith(("mark.skip", "mark.skipif", "mark.xfail")):
                seen_marks.add(id(node.func))
                cond = None
                if not name.endswith("mark.skip"):
                    cond = node.args[0] if node.args else None
                    for kw in node.keywords:
                        if kw.arg == "condition":
                            cond = kw.value
                tally[_condition_kind(cond, resolver)] += 1
        elif isinstance(node, ast.Attribute) and id(node) not in seen_marks:
            if _dotted(node).endswith(("mark.skip", "mark.xfail")):
                parent = parents.get(node)
                if not (isinstance(parent, ast.Call) and parent.func is node):
                    tally["debt"] += 1
    return tally


def _guard_kind(
    node: ast.AST, parents: dict[ast.AST, ast.AST], resolver: _Resolver
) -> str:
    """Classify an imperative skip by the nearest if/except that guards it."""
    child, cur = node, parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.If, ast.IfExp, ast.While)) and child is not cur.test:
            return _condition_kind(cur.test, resolver)
        if isinstance(cur, ast.ExceptHandler):
            return "environment" if _handler_is_env(cur) else "debt"
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            break
        child, cur = cur, parents.get(cur)
    return "debt"


def js_skip_debt(code_lines: list[tuple[int, str]]) -> int:
    """Skips with no environment probe within two lines either side."""
    debt = 0
    for idx, (_, line) in enumerate(code_lines):
        for _match in JS_SKIP.finditer(line):
            window = " ".join(text for _, text in code_lines[max(0, idx - 2) : idx + 3])
            if not JS_PROBE.search(window):
                debt += 1
    return debt


def measure_python(text: str) -> Counter[str]:
    tally: Counter[str] = Counter()
    if PY_ANY_COMMENT_KIND.search(text):
        for _, comment in python_comments(text):
            for kind, pattern in PY_COMMENT_KINDS.items():
                tally[kind] += len(pattern.findall(comment))
    if "skip" not in text and "xfail" not in text:
        return tally
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return tally
    skips = python_skips(tree)
    tally["py-skip-debt"] += skips["debt"]
    tally["py-skip-environment"] += skips["environment"]
    return tally


def measure_js(text: str) -> Counter[str]:
    tally: Counter[str] = Counter()
    for _, comment in js_comments(text):
        for kind, pattern in JS_COMMENT_KINDS.items():
            tally[kind] += len(pattern.findall(comment))
    code = js_code_lines(text)
    for _, line in code:
        for kind, pattern in JS_CODE_KINDS.items():
            tally[kind] += len(pattern.findall(line))
    tally["js-skip-debt"] += js_skip_debt(code)
    return tally


def measure() -> tuple[Counts, Counter[str]]:
    """Return (ratcheted counts, totals of every kind including environment skips)."""
    counts: Counts = {kind: {} for kind in KINDS}
    totals: Counter[str] = Counter()
    jobs = [(rel, measure_python) for rel in tracked_files(PY_ROOTS, PY_SUFFIXES)]
    jobs += [(rel, measure_js) for rel in tracked_files(JS_ROOTS, JS_SUFFIXES)]
    for rel, scan in jobs:
        try:
            text = (REPO / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        tally = scan(text)
        for kind, n in tally.items():
            totals[kind] += n
            if n and kind in counts:
                counts[kind][rel] = n
    return counts, totals


def compare(baseline: Counts, current: Counts) -> tuple[list[str], list[str]]:
    """Return (increases, decreases) as human-readable lines."""
    increases: list[str] = []
    decreases: list[str] = []
    for kind in KINDS:
        base = {} if kind in HARD_BAN else baseline.get(kind, {})
        cur = current.get(kind, {})
        for rel in sorted(set(base) | set(cur)):
            was, now = base.get(rel, 0), cur.get(rel, 0)
            if now > was:
                increases.append(f"{rel}: {kind} {now} > baseline {was}")
            elif now < was:
                decreases.append(f"{rel}: {kind} {now} < baseline {was}")
    return increases, decreases


def write(counts: Counts) -> None:
    payload = {
        kind: dict(sorted(counts.get(kind, {}).items()))
        for kind in KINDS
        if kind not in HARD_BAN
    }
    BASELINE.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="lower the baseline")
    parser.add_argument("--totals", action="store_true", help="print totals per kind")
    args = parser.parse_args(argv)

    baseline: Counts = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    current, totals = measure()
    if args.totals:
        for kind in (*KINDS, "py-skip-environment"):
            print(f"{kind:24} {totals[kind]:6} in {len(current.get(kind, {}))} file(s)")
    increases, decreases = compare(baseline, current)

    if increases:
        for line in increases:
            print(line)
        print(
            f"check_suppressions: {len(increases)} count(s) above baseline. Satisfy the "
            "check instead of silencing it: type the value, narrow the exception, fix "
            "or delete the test. A test that must skip probes its environment "
            "(pytest.importorskip, shutil.which, find_spec, an env var, the platform, "
            "or a has_*/requires_*/*_available helper); `.only` is never allowed. "
            "The baseline only shrinks."
        )
        return 1
    if args.update:
        write(current)
        print(f"check_suppressions: baseline lowered ({len(decreases)} entr(ies)).")
        return 0
    for line in decreases:
        print(line)
    total = sum(sum(files.values()) for files in current.values())
    note = " Run with --update to lock in." if decreases else ""
    print(f"check_suppressions: ok ({total} budgeted suppressions).{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
