"""Token and AST lints over test sources, driven by one rule table.

Each rule scans every ``*.py`` under tests/ and sidecar/tests/ for offending
lines or test functions. A rule's line marker on the offending line or the line
above suppresses that match (for multi-line AST constructs, also on the line
above the enclosing statement or decorator); its file marker anywhere in the
file suppresses the whole file.

Allowlists live in lint_test_tokens_allowlist.json, keyed by rule then file,
and only shrink. A counted rule maps a file to the number of offending lines
it may keep: more fails, fewer fails until the entry is ratcheted down. A
reasoned rule maps a file to the reason all its lines are exempt. Either way,
an entry for a clean or missing file fails until it is deleted.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TEST_DIRS = ("tests", "sidecar/tests")
ALLOWLIST = Path(__file__).with_name("lint_test_tokens_allowlist.json")

Candidates = Callable[[list[str]], Iterator[int]]

RULE19_MARKER = re.compile(r"#\s*rule19:\s*\S")
INVARIANT_MARKER = re.compile(r"#\s*invariant:\s*\S")
CONDITIONAL_MARKER = re.compile(r"#\s*(?:conditional-assert|invariant):\s*\S")

# Source-tree path literals and build manifests.
SOURCE_PATH = re.compile(
    r"""(?:\.{0,2}/)*(?:src|web|scripts|\.github)(?:/|$)"""
    r"""|(?:^|/)Dockerfile(?:\.|$)|docker-compose"""
)
PATH_SEGMENTS = frozenset({"src", "web", "scripts", ".github", "Dockerfile"})
SOURCE_INSPECT_CALLS = frozenset(
    {"getsourcefile", "getfile", "getsource", "getsourcelines"}
)
GETSOURCE_CALLS = frozenset({"getsource", "getsourcelines"})
ALLOWED_SURFACE_CALLS = frozenset(
    {
        "hasattr",
        "getattr",
        "callable",
        "tuple",
        "set",
        "list",
        "sorted",
        "import_module",
        "globals",
        "vars",
        "dir",
    }
)


def token_lines(token: re.Pattern[str]) -> Candidates:
    def scan(lines: list[str]) -> Iterator[int]:
        return (idx for idx, line in enumerate(lines) if token.search(line))

    return scan


def _marked_in_span(
    lines: list[str], marker: re.Pattern[str], start_1based: int, end_1based: int
) -> bool:
    lo = max(0, start_1based - 2)
    hi = min(len(lines), end_1based)
    return any(marker.search(lines[idx]) for idx in range(lo, hi))


def _expr_has_source_origin(
    node: ast.AST, tainted: set[str], *, in_path_expr: bool = False
) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if SOURCE_PATH.search(sub.value):
                return True
            if in_path_expr and sub.value in PATH_SEGMENTS:
                return True
        elif isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Div):
            if _expr_has_source_origin(
                sub.left, tainted, in_path_expr=True
            ) or _expr_has_source_origin(sub.right, tainted, in_path_expr=True):
                return True
        elif isinstance(sub, ast.Attribute) and sub.attr == "__file__":
            return True
        elif isinstance(sub, ast.Call):
            if _call_is_source_origin(sub, tainted):
                return True
        elif isinstance(sub, ast.Name) and sub.id in tainted:
            return True
    return False


def _call_is_source_origin(call: ast.Call, tainted: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in SOURCE_INSPECT_CALLS:
        return True
    if isinstance(func, ast.Name) and func.id in GETSOURCE_CALLS:
        return True
    if isinstance(func, ast.Name) and func.id == "Path":
        return any(
            _expr_has_source_origin(arg, tainted, in_path_expr=True)
            for arg in call.args
        )
    return False


def _scope_statements(stmts: list[ast.stmt]) -> Iterator[ast.stmt]:
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield stmt
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.stmt) and not isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                yield from _scope_statements([child])


def _bound_names(target: ast.AST) -> Iterator[str]:
    for sub in ast.walk(target):
        if isinstance(sub, ast.Name):
            yield sub.id


def _taint_targets(stmt: ast.stmt, tainted: set[str]) -> list[str]:
    if isinstance(stmt, ast.Assign) and _expr_has_source_origin(stmt.value, tainted):
        return [name for target in stmt.targets for name in _bound_names(target)]
    if (
        isinstance(stmt, ast.AnnAssign)
        and stmt.value is not None
        and _expr_has_source_origin(stmt.value, tainted)
    ):
        return list(_bound_names(stmt.target))
    if isinstance(stmt, (ast.For, ast.AsyncFor)) and _expr_has_source_origin(
        stmt.iter, tainted
    ):
        return list(_bound_names(stmt.target))
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        names: list[str] = []
        for item in stmt.items:
            if item.optional_vars is not None and _expr_has_source_origin(
                item.context_expr, tainted
            ):
                names.extend(_bound_names(item.optional_vars))
        return names
    return []


def _propagate_taint(stmts: list[ast.stmt], seed: set[str]) -> set[str]:
    tainted = set(seed)
    scope = list(_scope_statements(stmts))
    changed = True
    while changed:
        changed = False
        for stmt in scope:
            for name in _taint_targets(stmt, tainted):
                if name not in tainted:
                    tainted.add(name)
                    changed = True
    return tainted


def _is_source_read_call(call: ast.Call, tainted: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in GETSOURCE_CALLS:
        return True
    if isinstance(func, ast.Name) and func.id in GETSOURCE_CALLS:
        return True
    if isinstance(func, ast.Attribute) and func.attr in {"read_text", "read_bytes"}:
        return _expr_has_source_origin(func.value, tainted)
    if isinstance(func, ast.Name) and func.id == "open" and call.args:
        return _expr_has_source_origin(call.args[0], tainted)
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "parse"
        and isinstance(func.value, ast.Name)
        and func.value.id == "ast"
        and call.args
    ):
        return _expr_has_source_origin(call.args[0], tainted)
    return False


def _param_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names = {
        arg.arg for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)
    }
    if fn.args.vararg is not None:
        names.add(fn.args.vararg.arg)
    if fn.args.kwarg is not None:
        names.add(fn.args.kwarg.arg)
    return names


def source_read_lines(lines: list[str]) -> Iterator[int]:
    """AST-based detector for reads, ``getsource``, and ``ast.parse`` reaching repo source.

    Catches single-line and multiline chains (e.g. ``(Path(...) / "...").read_text()``)
    while scoping tainted variable names per module and function so unrelated
    locals in sibling tests never collide.
    """
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError:
        return
    hits: set[int] = set()

    def scan_exprs(node: ast.AST, stmt: ast.stmt, tainted: set[str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(node, ast.Call) and _is_source_read_call(node, tainted):
            start = min(stmt.lineno, node.lineno)
            end = max(node.end_lineno or node.lineno, node.lineno)
            if not _marked_in_span(lines, RULE19_MARKER, start, end):
                hits.add(node.lineno - 1)
        for child in ast.iter_child_nodes(node):
            scan_exprs(child, stmt, tainted)

    def scan_block(stmts: list[ast.stmt], parent_taint: set[str]) -> None:
        tainted = _propagate_taint(stmts, parent_taint)
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scan_block(stmt.body, tainted - _param_names(stmt))
                continue
            if isinstance(stmt, ast.ClassDef):
                scan_block(stmt.body, tainted)
                continue
            scan_exprs(stmt, stmt, tainted)
            for child in ast.iter_child_nodes(stmt):
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    scan_block([child], tainted)
                elif isinstance(child, ast.stmt):
                    scan_block([child], tainted)

    scan_block(tree.body, set())
    yield from sorted(hits)


def _iter_test_functions(
    tree: ast.Module,
) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in tree.body:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name.startswith("test_"):
            yield node
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(
                    item, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and item.name.startswith("test_"):
                    yield item


def _fn_header_start(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    if fn.decorator_list:
        return min(dec.lineno for dec in fn.decorator_list)
    return fn.lineno


def _is_surface_assert(expr: ast.expr) -> bool:
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        return _is_surface_assert(expr.operand)
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id in {"hasattr", "callable"}
    ):
        return True
    if isinstance(expr, ast.Compare):
        if (
            len(expr.ops) == 1
            and isinstance(expr.ops[0], ast.IsNot)
            and isinstance(expr.comparators[0], ast.Constant)
            and expr.comparators[0].value is None
            and isinstance(expr.left, ast.Name)
        ):
            return True
        if any(
            isinstance(sub, ast.Attribute)
            and sub.attr in {"__name__", "__all__", "__module__"}
            for sub in ast.walk(expr)
        ):
            return True
    if isinstance(expr, ast.BoolOp):
        return all(_is_surface_assert(value) for value in expr.values)
    return False


def _is_pure_surface_test(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    asserts: list[ast.Assert] = []
    calls: list[ast.Call] = []
    for stmt in fn.body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Assert):
                asserts.append(sub)
            elif isinstance(sub, ast.Call):
                calls.append(sub)
    if not asserts or not all(_is_surface_assert(item.test) for item in asserts):
        return False
    for call in calls:
        func = call.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else (func.id if isinstance(func, ast.Name) else "")
        )
        if name not in ALLOWED_SURFACE_CALLS:
            return False
    return True


def pure_surface_test_lines(lines: list[str]) -> Iterator[int]:
    """Tests whose assertions check only ``hasattr``/``callable``/``__name__``/``__all__``."""
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError:
        return
    for fn in _iter_test_functions(tree):
        if not _is_pure_surface_test(fn):
            continue
        start = _fn_header_start(fn)
        end = fn.end_lineno or fn.lineno
        if not _marked_in_span(lines, INVARIANT_MARKER, start, end):
            yield fn.lineno - 1


def _is_nonempty_literal(expr: ast.expr, known_nonempty: set[str]) -> bool:
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)) and len(expr.elts) > 0:
        return True
    if isinstance(expr, ast.Dict) and len(expr.keys) > 0:
        return True
    if isinstance(expr, ast.Name) and expr.id in known_nonempty:
        return True
    if isinstance(expr, ast.Call):
        return _call_is_nonempty_iterable(expr, known_nonempty)
    return False


def _call_is_nonempty_iterable(call: ast.Call, known_nonempty: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        if (
            func.id in {"enumerate", "reversed", "sorted", "list", "tuple"}
            and call.args
        ):
            return _is_nonempty_literal(call.args[0], known_nonempty)
        if func.id == "zip" and call.args:
            return all(_is_nonempty_literal(arg, known_nonempty) for arg in call.args)
        if (
            func.id == "range"
            and len(call.args) == 1
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, int)
            and call.args[0].value > 0
        ):
            return True
    if isinstance(func, ast.Attribute) and func.attr in {"items", "keys", "values"}:
        return _is_nonempty_literal(func.value, known_nonempty)
    return False


def _collect_nonempty_bindings(stmts: list[ast.stmt], seed: set[str]) -> set[str]:
    known = set(seed)
    for stmt in stmts:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and _is_nonempty_literal(stmt.value, known)
        ):
            known.add(stmt.targets[0].id)
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.value is not None
            and _is_nonempty_literal(stmt.value, known)
        ):
            known.add(stmt.target.id)
    return known


def _has_assertion_call(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else (func.id if isinstance(func, ast.Name) else "")
            )
            if (
                name in {"raises", "warns", "fail", "xfail", "skip", "importorskip"}
                or "assert" in name.lower()
            ):
                return True
    return False


def _stmt_is_unconditional_check(stmt: ast.stmt, known: set[str]) -> bool:
    if isinstance(stmt, ast.Assert):
        return True
    if isinstance(stmt, ast.Expr):
        return _has_assertion_call(stmt.value)
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return any(
            _has_assertion_call(item.context_expr) for item in stmt.items
        ) or _has_unconditional_check(stmt.body, known)
    if isinstance(stmt, ast.Try):
        if _has_unconditional_check(stmt.body, known) or _has_unconditional_check(
            stmt.finalbody, known
        ):
            return True
        if stmt.orelse and any(_has_assertion_call(sub) for sub in stmt.orelse):
            return True
        if (
            stmt.handlers
            and stmt.orelse
            and all(_has_unconditional_check(h.body, known) for h in stmt.handlers)
            and _has_unconditional_check(stmt.orelse, known)
        ):
            return True
        return False
    if isinstance(stmt, ast.For):
        return _is_nonempty_literal(stmt.iter, known) and _has_unconditional_check(
            stmt.body, known
        )
    if isinstance(stmt, ast.If):
        return (
            bool(stmt.orelse)
            and _has_unconditional_check(stmt.body, known)
            and _has_unconditional_check(stmt.orelse, known)
        )
    return False


def _has_unconditional_check(stmts: list[ast.stmt], seed: set[str]) -> bool:
    known = _collect_nonempty_bindings(stmts, seed)
    return any(_stmt_is_unconditional_check(stmt, known) for stmt in stmts)


def _has_direct_assert(stmts: list[ast.stmt]) -> bool:
    for stmt in _scope_statements(stmts):
        if isinstance(stmt, ast.Assert):
            return True
    return False


def conditional_only_test_lines(lines: list[str]) -> Iterator[int]:
    """Tests whose assertions all sit inside conditional or dynamic-loop blocks."""
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError:
        return
    mod_nonempty = _collect_nonempty_bindings(tree.body, set())
    for fn in _iter_test_functions(tree):
        if not _has_direct_assert(fn.body):
            continue
        if _has_unconditional_check(fn.body, mod_nonempty):
            continue
        start = _fn_header_start(fn)
        if not _marked_in_span(lines, CONDITIONAL_MARKER, start, fn.lineno):
            yield fn.lineno - 1


@dataclass(frozen=True)
class Rule:
    name: str
    candidates: Candidates
    line_marker: re.Pattern[str]
    file_marker: re.Pattern[str] | None
    exempt: frozenset[str]
    counted: bool
    noun: str
    remedy: str
    summary: str


RULES = (
    Rule(
        name="source-reads",
        candidates=source_read_lines,
        line_marker=RULE19_MARKER,
        file_marker=None,
        exempt=frozenset(),
        counted=True,
        noun="source-read",
        remedy=(
            "assert on runtime behavior instead; a legitimate survivor (executes "
            "the artifact, or diffs two independently maintained sources) "
            "annotates `# rule19: <reason>` on the line or the line above"
        ),
        summary="tests never read repository source as text",
    ),
    Rule(
        name="deterministic-time",
        candidates=token_lines(
            re.compile(r"\btime\.sleep\(|\btime\.monotonic\(|\bthreading\.Thread\(")
        ),
        line_marker=re.compile(r"#\s*realtime:\s*\S"),
        file_marker=re.compile(r"pytest\.mark\.realtime"),
        # The controlled_time harness is the one module allowed real time.
        exempt=frozenset({"tests/deterministic_time.py"}),
        counted=True,
        noun="real-time",
        remedy=(
            "migrate to tests/deterministic_time.controlled_time, mark the file "
            "pytest.mark.realtime (positive assertions only), or annotate the "
            "line `# realtime: <reason>`"
        ),
        summary=(
            "tests must use controlled time; real-clock tests are limited to "
            "positive latency/bound assertions"
        ),
    ),
    Rule(
        name="subprocess-spawns",
        candidates=token_lines(re.compile(r"\bsys\.executable\b")),
        line_marker=re.compile(r"#\s*subprocess-boundary:\s*\S"),
        file_marker=None,
        exempt=frozenset(),
        # An allowlist reason names the process property under test: signals or
        # crash exit codes, child liveness, env across exec, sandbox isolation,
        # cold-import semantics, entry-point wiring, or cross-process visibility.
        counted=False,
        noun="sys.executable",
        remedy=(
            "drive the CLI in-process via tests/helpers.py:run_cli, or (only for "
            "a genuine process-boundary assertion) annotate `# subprocess-boundary: "
            "<reason>`"
        ),
        summary="tests/helpers.py run_cli is the in-process route",
    ),
    Rule(
        name="pure-surface-assertions",
        candidates=pure_surface_test_lines,
        line_marker=INVARIANT_MARKER,
        file_marker=None,
        exempt=frozenset(),
        counted=True,
        noun="pure-surface test",
        remedy=(
            "test runtime behavior instead of hasattr/__name__/__all__ existence; "
            "a genuine structural boundary invariant annotates `# invariant: <reason>` "
            "on or above the test definition"
        ),
        summary="tests assert behavior rather than pure reflection surface",
    ),
    Rule(
        name="conditional-only-assertions",
        candidates=conditional_only_test_lines,
        line_marker=CONDITIONAL_MARKER,
        file_marker=None,
        exempt=frozenset(),
        counted=True,
        noun="conditional-only test",
        remedy=(
            "add an unconditional non-empty precondition (e.g. `assert items`) so "
            "empty iterables or untaken branches cannot silently pass"
        ),
        summary="every asserting test runs at least one unconditional check",
    ),
)


def offending_lines(rule: Rule, text: str) -> list[tuple[int, str]]:
    if rule.file_marker and rule.file_marker.search(text):
        return []
    lines = text.splitlines()
    marker = rule.line_marker
    return [
        (idx + 1, lines[idx].strip())
        for idx in rule.candidates(lines)
        if not (
            marker.search(lines[idx]) or (idx > 0 and marker.search(lines[idx - 1]))
        )
    ]


def rule_failures(
    rule: Rule, sources: dict[str, str], allowlist: dict[str, int | str]
) -> list[str]:
    found: dict[str, list[tuple[int, str]]] = {}
    for rel, text in sources.items():
        if rel in rule.exempt:
            continue
        offenders = offending_lines(rule, text)
        if offenders:
            found[rel] = offenders

    failures: list[str] = []
    for rel, offenders in sorted(found.items()):
        if not rule.counted and rel in allowlist:
            continue
        allowed = int(allowlist.get(rel, 0)) if rule.counted else 0
        if len(offenders) > allowed:
            for lineno, line in offenders:
                print(f"{rel}:{lineno}: {line}")
            grant = f", allowlist grants {allowed}" if rule.counted else ""
            failures.append(
                f"{rel}: {len(offenders)} {rule.noun} line(s){grant} — {rule.remedy}"
            )
        elif len(offenders) < allowed:
            failures.append(
                f"{rel}: allowlist grants {allowed} but only {len(offenders)} "
                f"remain — ratchet the entry down to {len(offenders)}"
            )
    for rel in sorted(set(allowlist) - set(found)):
        failures.append(f"{rel}: allowlisted but clean (or gone) — delete its entry")
    return failures


def main() -> int:
    allowlists = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    names = {rule.name for rule in RULES}
    if set(allowlists) != names:
        print(
            f"{ALLOWLIST.name} must key exactly the rules {sorted(names)}",
            file=sys.stderr,
        )
        return 1
    sources: dict[str, str] = {}
    for test_dir in TEST_DIRS:
        root = REPO / test_dir
        if root.is_dir():
            for path in sorted(root.rglob("*.py")):
                sources[path.relative_to(REPO).as_posix()] = path.read_text(
                    encoding="utf-8"
                )

    status = 0
    for rule in RULES:
        failures = rule_failures(rule, sources, allowlists[rule.name])
        if failures:
            print(f"\n{rule.name} lint failed: {rule.summary}:", file=sys.stderr)
            for failure in failures:
                print(f"  {failure}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
