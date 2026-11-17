#!/usr/bin/env python3
"""Dead-symbol gate: production code must reach every symbol and module in src/frisket.

A function, class or method defined in src/frisket offends when its name is
referenced nowhere in production code other than its own definition. Tests do
not count as production. Production is src/frisket plus the CONSUMERS below
(and, with ``--downstream PATH``, a frisket-cloud checkout). A module offends
when no production file imports it, names its dotted path in a string, or
names its file.

Name-based and deliberately conservative: any same-named reference anywhere
(attribute access, call, string constant such as a getattr or registry key,
aliased import) keeps a symbol alive; plain re-export imports, ``__all__``
entries, docstrings, comments and self-references do not. Definitions under a
registering decorator (routes, validators, fixtures, CLI commands) and dunder
or framework-protocol methods are never candidates.

Existing offenders live in ALLOWLIST, one per line: ``<id>  <reason>``. The
gate fails on an offender missing from it and on an entry that no longer
offends, so the list only shrinks. ``--update`` drops stale entries and never
adds. Entries whose reason is ``frisket-cloud`` are used downstream: with
``--downstream`` they must be alive there, without it they must offend.
"""

from __future__ import annotations

import argparse
import ast
import collections.abc
import contextlib
import html.parser
import http.server
import importlib.abc
import io
import json
import logging
import re
import string
import subprocess
import sys
import threading
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SRC = REPO / "src"
PACKAGE = SRC / "frisket"
ALLOWLIST = REPO / "scripts/ci/dead_symbols_allowlist.txt"
CONSUMERS = (
    "sidecar",
    "scripts",
    "desktop",
    "sdk",
    "guides",
    "skills",
    "web/src",
    "deploy",
    ".github",
    "pyproject.toml",
    "hatch_build.py",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.heavy.yml",
    "docker-compose.models-gpu.yml",
)
SKIP_DIRS = {
    ".claude",
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "__tests__",
    "tests",
    "worktrees",
}
# Gate data names modules and symbols without using them.
SKIP_FILES = {
    "comment_history_baseline.json",
    "dead_symbols_allowlist.txt",
    "duplicate_functions_baseline.json",
    "import_boundaries.json",
    "ruff_ratchet_baseline.json",
    "suppressions_baseline.json",
    "test_collection_allowlist.txt",
    "type_ratchet_baseline.json",
    "web_tests_type_baseline.json",
}
TEXT_SUFFIXES = {
    ".cjs", ".js", ".json", ".md", ".mjs", ".rs", ".sh", ".toml", ".ts", ".tsx",
    ".yaml", ".yml", "",
}  # fmt: skip
TEST_FILE = re.compile(r"(^test_|_test\.py$|\.(test|spec|stories)\.)")
DOWNSTREAM_REASON = "frisket-cloud"
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FILENAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.py\b")
DOTTED = re.compile(r"\bfrisket(?:[./][A-Za-z_][A-Za-z0-9_]*)+")
# Decorators that leave a definition an ordinary callable/attribute; any other
# decorator is assumed to register the definition somewhere.
TRANSPARENT_DECORATORS = {
    "abstractmethod", "asynccontextmanager", "cache", "cached_property",
    "classmethod", "contextmanager", "dataclass", "final", "lru_cache",
    "override", "property", "runtime_checkable", "staticmethod",
    "total_ordering", "setter", "getter", "deleter", "wraps",
}  # fmt: skip
# Methods invoked by the stdlib or frameworks rather than by name in our code:
# anything a common stdlib base class defines, plus third-party hook names.
PROTOCOL_BASES = (
    argparse.Action, ast.NodeTransformer, collections.abc.AsyncGenerator,
    collections.abc.Generator, collections.abc.MutableMapping,
    collections.abc.MutableSequence, collections.abc.MutableSet,
    contextlib.AbstractContextManager, html.parser.HTMLParser,
    http.server.BaseHTTPRequestHandler, importlib.abc.Loader,
    importlib.abc.MetaPathFinder, io.BufferedIOBase, io.RawIOBase, io.TextIOBase,
    json.JSONEncoder, logging.Filter, logging.Formatter, logging.Handler,
    string.Formatter, threading.Thread,
)  # fmt: skip
PROTOCOL_METHODS = {name for base in PROTOCOL_BASES for name in dir(base)} | {
    "force_flush", "get_response", "get_route_handler", "handle_async_request",
    "handle_request", "model_post_init", "shutdown",
}  # fmt: skip
PROTOCOL_PATTERN = re.compile(r"^(visit_\w+|do_[A-Z]+|handle_\w+)$")


def walk(root: Path) -> Iterator[Path]:
    """Yield non-test files under root, preferring git's view (no untracked junk)."""
    if root.is_file():
        yield root
        return
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout.decode()
        paths = [Path(rel) for rel in listed.split("\0") if rel]
    except (OSError, subprocess.CalledProcessError):
        paths = [p.relative_to(root) for p in root.rglob("*") if p.is_file()]
    for rel in paths:
        if SKIP_DIRS.isdisjoint(rel.parts[:-1]) and rel.name not in SKIP_FILES:
            if not TEST_FILE.search(rel.name):
                yield root / rel


def parse(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


def decorator_name(node: ast.expr) -> str:
    while isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def is_registered(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> bool:
    return any(
        decorator_name(d) not in TRANSPARENT_DECORATORS for d in node.decorator_list
    )


def method_binding(node: ast.AST) -> tuple[str, str] | None:
    """Return (name, target) for a class-body ``name = module.target`` binding.

    Such a binding defines a method exactly as ``def name`` would, so it is a
    candidate, not a use of the function it binds.
    """
    if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
        return None
    target = node.targets[0]
    if not (isinstance(target, ast.Name) and isinstance(node.value, ast.Attribute)):
        return None
    if not isinstance(node.value.value, ast.Name):
        return None
    return target.id, node.value.attr


class References(ast.NodeVisitor):
    """Count name references, excluding a definition's references to itself."""

    def __init__(self) -> None:
        self.names: Counter[str] = Counter()
        self.modules: set[str] = set()
        self._scope: list[str] = []
        self._package = ""

    def scan(self, tree: ast.Module, module: str, is_package: bool) -> None:
        self._package = module if is_package else module.rpartition(".")[0]
        self.visit(tree)

    def _add(self, name: str) -> None:
        if name not in self._scope:
            self.names[name] += 1

    def _definition(self, node: ast.AST, name: str) -> None:
        self._scope.append(name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._definition(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._definition(node, node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        for child in ast.iter_child_nodes(node):
            bound = method_binding(child)
            if bound is None:
                self.visit(child)
            elif bound[1] != bound[0]:
                # `name = module.other` keeps `other` alive; the binding itself
                # is a definition of `name`, never a reference to it.
                self._add(bound[1])
        self._scope.pop()

    def visit_Name(self, node: ast.Name) -> None:
        self._add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._add(node.attr)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            for word in WORD.findall(node.value):
                self._add(word)
            for dotted in DOTTED.findall(node.value):
                self.modules.add(dotted.replace("/", "."))
            leaf = node.value.lstrip(".")
            if self._package and leaf.isidentifier():
                self.modules.add(f"{self._package}.{leaf}")

    def visit_Expr(self, node: ast.Expr) -> None:
        if not (
            isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        ):
            self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.modules.add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        if node.level:
            parts = self._package.split(".")
            parts = parts[: len(parts) - node.level + 1] if node.level > 1 else parts
            base = ".".join(p for p in (*parts, base) if p)
        self.modules.add(base)
        for alias in node.names:
            self.modules.add(f"{base}.{alias.name}")
            if alias.asname and alias.asname != alias.name:
                self._add(alias.name)


def module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
    return ".".join(parts)


def scan_consumers(roots: list[Path]) -> tuple[References, Counter[str]]:
    """Collect references and ``*.py`` filename mentions from consumer trees."""
    refs = References()
    filenames = Counter[str]()
    for root in roots:
        for path in walk(root):
            if path.suffix not in TEXT_SUFFIXES and path.suffix != ".py":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            filenames.update(FILENAME.findall(text))
            if path.suffix == ".py":
                tree = parse(text)
                if tree is not None:
                    refs.scan(tree, "", False)
            else:
                refs.names.update(WORD.findall(text))
                refs.modules.update(d.replace("/", ".") for d in DOTTED.findall(text))
    return refs, filenames


def candidates(tree: ast.Module, rel: str) -> Iterator[tuple[str, str]]:
    """Yield (id, name) for module- and class-level definitions."""

    def visit(
        body: list[ast.stmt], prefix: str, in_class: bool
    ) -> Iterator[tuple[str, str]]:
        for node in body:
            bound = method_binding(node) if in_class else None
            if bound is not None:
                if not bound[0].startswith("__"):
                    yield f"{rel}::{prefix}{bound[0]}", bound[0]
                continue
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            name = node.name
            if is_registered(node) or name.startswith("__"):
                continue
            if in_class and (name in PROTOCOL_METHODS or PROTOCOL_PATTERN.match(name)):
                continue
            yield f"{rel}::{prefix}{name}", name
            if isinstance(node, ast.ClassDef):
                yield from visit(node.body, f"{prefix}{name}.", True)

    yield from visit(tree.body, "", False)


def prefixes(module: str) -> Iterator[str]:
    parts = module.split(".")
    for i in range(1, len(parts) + 1):
        yield ".".join(parts[:i])


def find_offenders(downstream: Path | None) -> set[str]:
    trees: dict[Path, ast.Module] = {}
    own_mentions: dict[Path, Counter[str]] = {}
    package_mentions = Counter[str]()
    importers: dict[str, set[Path | None]] = {}
    total = Counter[str]()
    for path in sorted(PACKAGE.rglob("*.py")):
        if SKIP_DIRS.intersection(path.relative_to(PACKAGE).parts):
            continue
        text = path.read_text(encoding="utf-8")
        own_mentions[path] = Counter(FILENAME.findall(text))
        package_mentions.update(own_mentions[path])
        tree = parse(text)
        if tree is None:
            continue
        trees[path] = tree
        refs = References()
        refs.scan(tree, module_name(path), path.name == "__init__.py")
        total.update(refs.names)
        for module in refs.modules:
            for prefix in prefixes(module):
                importers.setdefault(prefix, set()).add(path)
    roots = [REPO / c for c in CONSUMERS if (REPO / c).exists()]
    if downstream is not None:
        roots.append(downstream)
    consumers, consumer_mentions = scan_consumers(roots)
    total.update(consumers.names)
    for module in consumers.modules:
        for prefix in prefixes(module):
            importers.setdefault(prefix, set()).add(None)

    offenders: set[str] = set()
    for path, tree in trees.items():
        rel = path.relative_to(REPO).as_posix()
        offenders.update(
            ident for ident, name in candidates(tree, rel) if total[name] == 0
        )
        if path.name in ("__init__.py", "__main__.py"):
            continue
        if importers.get(module_name(path), set()) - {path}:
            continue
        mentions = package_mentions[path.name] - own_mentions[path][path.name]
        if consumer_mentions[path.name] or mentions:
            continue
        offenders.add(f"{rel}::<module>")
    return offenders


def read_allowlist() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            ident, _, reason = line.partition("  ")
            entries[ident.strip()] = reason.strip()
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--downstream", type=Path, help="frisket-cloud checkout")
    parser.add_argument("--update", action="store_true", help="drop stale entries")
    args = parser.parse_args(argv)

    offenders = find_offenders(args.downstream)
    allowed = read_allowlist()
    downstream_mode = args.downstream is not None
    missing = sorted(offenders - set(allowed))
    stale = sorted(
        ident
        for ident, reason in allowed.items()
        if (ident in offenders) == (downstream_mode and reason == DOWNSTREAM_REASON)
    )

    if args.update:
        if missing:
            print("check_dead_symbols: --update never adds entries; fix these first:")
            print("\n".join(f"  {m}" for m in missing))
            return 1
        kept = [
            line
            for line in ALLOWLIST.read_text(encoding="utf-8").splitlines()
            if line.partition("  ")[0].strip() not in stale
        ]
        ALLOWLIST.write_text("\n".join(kept) + "\n", encoding="utf-8")
        print(f"check_dead_symbols: removed {len(stale)} stale entr(ies).")
        return 0

    for ident in missing:
        print(f"{ident}: not reached by production code (tests do not count)")
    for ident in stale:
        if downstream_mode and allowed[ident] == DOWNSTREAM_REASON:
            print(f"{ident}: allowlisted as {DOWNSTREAM_REASON} but unused there")
        else:
            print(f"{ident}: allowlisted but no longer offends")
    if missing or stale:
        print(
            "check_dead_symbols: delete unreached code (and its tests) rather than "
            "allowlisting it; delete stale lines from "
            f"{ALLOWLIST.relative_to(REPO)} (or run --update)."
        )
        return 1
    print(f"check_dead_symbols: ok ({len(allowed)} allowlisted).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
