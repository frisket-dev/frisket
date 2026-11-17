#!/usr/bin/env python3
"""Every test file is collected by a runner; a file no runner collects never runs.

Each mode asks the runners themselves which files they collect and compares
that with the test files on disk (git's view):

* ``--python``: ``pytest --collect-only`` from the repository root, once with
  the default marker expression and once with ``gap or network or
  plugin_contract`` (the union counts), against ``tests/**/test_*.py`` and ``*_test.py``. Modules
  that skip at import count as collected. Also fails on any file under tests/
  that defines ``test_*`` functions but whose name does not match pytest's
  ``python_files``.
* ``--sidecar``: the same pytest check for ``sidecar/tests`` from ``sidecar/``
  (run it with the sidecar environment's interpreter).
* ``--web``: ``vitest list`` and every ``web/playwright/*.config.ts``'s
  ``playwright test --list`` against ``web/**/*.test.{ts,tsx}`` and
  ``*.spec.{ts,tsx}``.

A file in none of them fails, unless ALLOWLIST names it (one path per line,
``<path>  <reason>``); an allowlisted file that is now collected also fails,
so the list only shrinks. Fix a failure by moving or renaming the file into a
runner's pattern, or by widening the runner's include, never by deleting the
test.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
WEB = REPO / "web"
ALLOWLIST = REPO / "scripts/ci/test_collection_allowlist.txt"
# tests/conftest.py deselects gap tests unless the expression asks for them
# positively, so the default suite and the opt-in markers are two runs.
MARKER_RUNS = ([], ["-m", "gap or network or plugin_contract"])
PY_TEST_NAME = re.compile(r"^(test_.*|.*_test)\.py$")
WEB_TEST_NAME = re.compile(r"\.(test|spec)\.tsx?$")
COLLECTED_LINE = re.compile(r"^(?P<file>[^\s:]+\.py): \d+$")
SKIPPED_LINE = re.compile(r"^SKIPPED \[\d+\] (?P<file>[^\s:]+\.py)")


def git_files(root: str) -> list[str]:
    listed = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            root,
        ],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout.decode()
    rels = sorted({rel for rel in listed.split("\0") if rel})
    return [rel for rel in rels if "node_modules" not in rel and (REPO / rel).is_file()]


def pytest_collected(cwd: Path, args: list[str]) -> set[str]:
    """Repo-relative files pytest collects (or skips at import) from cwd."""
    found: set[str] = set()
    for markers in MARKER_RUNS:
        found |= _pytest_collected_once(cwd, [*markers, *args])
    return found


def _pytest_collected_once(cwd: Path, args: list[str]) -> set[str]:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-qq",
            "-rs",
            "-p",
            "no:cacheprovider",
        ]
        + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 5):
        raise SystemExit(
            f"check_test_collection: pytest collection failed in {cwd}:\n"
            + proc.stdout[-4000:]
            + proc.stderr[-4000:]
        )
    found: set[str] = set()
    for line in proc.stdout.splitlines():
        match = COLLECTED_LINE.match(line) or SKIPPED_LINE.match(line)
        if match:
            found.add((cwd / match["file"]).resolve().relative_to(REPO).as_posix())
    return found


def misnamed_python_tests(root: str, patterns: list[str]) -> list[str]:
    """Files under root defining test_* functions that python_files never matches."""
    out = []
    for rel in git_files(root):
        name = rel.rsplit("/", 1)[-1]
        if not name.endswith(".py") or any(fnmatch.fnmatch(name, p) for p in patterns):
            continue
        if "/fixtures/" in rel:
            continue
        try:
            tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        scopes = [tree.body] + [
            n.body for n in tree.body if isinstance(n, ast.ClassDef)
        ]
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
            for body in scopes
            for node in body
        ):
            out.append(rel)
    return out


def check_python(cwd: Path, root: str, args: list[str]) -> list[str]:
    on_disk = {
        rel
        for rel in git_files(root)
        if PY_TEST_NAME.match(rel.rsplit("/", 1)[-1]) and "/fixtures/" not in rel
    }
    missing = sorted(on_disk - pytest_collected(cwd, args))
    if root == "tests":
        config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        patterns = config["tool"]["pytest"]["ini_options"]["python_files"]
        missing += misnamed_python_tests(root, patterns)
    return sorted(set(missing))


def vitest_collected() -> set[str]:
    proc = subprocess.run(
        ["npx", "--no-install", "vitest", "list", "--filesOnly", "--json"],
        cwd=WEB,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"check_test_collection: vitest list failed:\n{proc.stderr[-4000:]}"
        )
    return {
        Path(item["file"]).resolve().relative_to(REPO).as_posix()
        for item in json.loads(proc.stdout)
    }


def playwright_collected(config: Path) -> set[str]:
    proc = subprocess.run(
        [
            "npx",
            "--no-install",
            "playwright",
            "test",
            "--list",
            "--reporter=json",
            "--config",
            str(config),
        ],
        cwd=WEB,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise SystemExit(
            f"check_test_collection: playwright --list failed for {config.name}:\n"
            + proc.stderr[-4000:]
        ) from None
    if report.get("errors"):
        raise SystemExit(
            f"check_test_collection: playwright --list errors for {config.name}: "
            + json.dumps(report["errors"])[:4000]
        )
    root = Path(report["config"]["rootDir"])
    return {
        (root / suite["file"]).resolve().relative_to(REPO).as_posix()
        for suite in report["suites"]
    }


def check_web() -> list[str]:
    on_disk = {rel for rel in git_files("web") if WEB_TEST_NAME.search(rel)}
    collected = vitest_collected()
    for config in sorted((WEB / "playwright").glob("*.config.ts")):
        collected |= playwright_collected(config)
    return sorted(on_disk - collected)


def read_allowlist() -> set[str]:
    if not ALLOWLIST.exists():
        return set()
    lines = ALLOWLIST.read_text(encoding="utf-8").splitlines()
    return {
        line.split()[0] for line in lines if line.strip() and not line.startswith("#")
    }


def verdict(missing: list[str], prefix: str) -> int:
    allowed = {rel for rel in read_allowlist() if rel.startswith(prefix)}
    new = [rel for rel in missing if rel not in allowed]
    stale = sorted(allowed - set(missing))
    for rel in new:
        print(f"{rel}: no test runner collects this file")
    for rel in stale:
        print(f"{rel}: collected now; delete its line from {ALLOWLIST.name}")
    if new or stale:
        print(
            f"check_test_collection: {len(new)} uncollected file(s), {len(stale)} stale "
            "allowlist entr(ies). Rename or move the file into a runner's pattern "
            "(pytest python_files, vitest include, a Playwright testDir/testMatch) or "
            "widen that pattern; the allowlist only shrinks."
        )
        return 1
    print(f"check_test_collection: ok ({len(allowed)} allowlisted under {prefix}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--python", action="store_true", help="root pytest suite")
    mode.add_argument("--sidecar", action="store_true", help="sidecar/tests")
    mode.add_argument("--web", action="store_true", help="vitest and Playwright")
    args = parser.parse_args(argv)
    if args.python:
        return verdict(check_python(REPO, "tests", []), "tests/")
    if args.sidecar:
        return verdict(
            check_python(REPO / "sidecar", "sidecar/tests", ["tests"]), "sidecar/"
        )
    return verdict(check_web(), "web/")


if __name__ == "__main__":
    sys.exit(main())
