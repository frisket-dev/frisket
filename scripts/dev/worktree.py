#!/usr/bin/env python3
"""Create, provision and remove lane worktrees of this repository.

A lane worktree has no virtualenv of its own. It runs Python against the main
checkout's ``.venv`` with ``PYTHONPATH=<worktree>/src`` and borrows the main
checkout's untracked build products (the native vec1 library, web
``node_modules``, the generated SDK contracts). ``provision`` sets that up and
proves ``import frisket`` resolves to the worktree before printing the exact
commands to use.

  worktree.py create <name> [--base <ref>] [--prefix <prefix>]
  worktree.py provision [<path>]
  worktree.py remove <name> [--prefix <prefix>] [--force]

Every git command that adds or removes a worktree runs in the main checkout,
which is resolved from ``git worktree list --porcelain`` (its first entry), so
the result does not depend on which worktree the caller is standing in.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

DEFAULT_PREFIX = "wt"
ENV_FILE = ".worktree-env"
NATIVE_DIR = Path("src/frisket/retrieval/_native")
SDK_CONTRACTS = Path("sdk/src/contracts.gen.ts")
SDK_MANIFEST = Path("sdk/contract-manifest.json")
NODE_MODULES = (Path("web/node_modules"), Path("sdk/node_modules"))
VENV_FIX = "uv sync --inexact --frozen"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class WorktreeError(RuntimeError):
    """A refusal with a message meant for the person running the script."""


def run(
    args: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    check: bool = True,
    env: Optional[dict] = None,
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(args), cwd=cwd, check=check, capture_output=True, text=True, env=env
    )


def git(runner: Runner, cwd: Path, *args: str) -> str:
    try:
        return runner(["git", *args], cwd=cwd).stdout.strip()
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise WorktreeError(f"git {' '.join(args)} failed in {cwd}: {detail}") from None


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str
    branch: Optional[str]


def parse_porcelain(text: str) -> list[Worktree]:
    """Parse ``git worktree list --porcelain``; the first entry is the main checkout."""
    entries: list[Worktree] = []
    for block in text.strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        if "worktree" not in fields:
            continue
        branch = fields.get("branch")
        entries.append(
            Worktree(
                path=Path(fields["worktree"]),
                head=fields.get("HEAD", ""),
                branch=branch[len("refs/heads/") :] if branch else None,
            )
        )
    return entries


def list_worktrees(runner: Runner = run, cwd: Optional[Path] = None) -> list[Worktree]:
    text = git(runner, cwd or Path.cwd(), "worktree", "list", "--porcelain")
    entries = parse_porcelain(text)
    if not entries:
        raise WorktreeError("git worktree list returned no worktrees")
    return entries


def main_checkout(runner: Runner = run, cwd: Optional[Path] = None) -> Path:
    return list_worktrees(runner, cwd)[0].path


def main_python(main: Path) -> Path:
    return main / ".venv" / "bin" / "python"


def removal_blockers(runner: Runner, path: Path, branch: Optional[str]) -> list[str]:
    """Reasons a worktree cannot be removed without losing work."""
    blockers: list[str] = []
    status = git(runner, path, "status", "--porcelain")
    if status:
        count = len(status.splitlines())
        blockers.append(f"{count} uncommitted or untracked path(s) in {path}")
    if branch:
        unpushed = git(
            runner, path, "rev-list", "--count", branch, "--not", "--remotes"
        )
        if unpushed != "0":
            blockers.append(
                f"{unpushed} commit(s) on {branch} are not on any remote branch"
            )
    return blockers


def remove_worktree(
    runner: Runner,
    main: Path,
    tree: Worktree,
    *,
    force: bool,
    delete_branch: bool,
    require_pushed: bool = True,
) -> None:
    """Remove a lane worktree, refusing to lose uncommitted or unpushed work.

    ``require_pushed=False`` is for a caller that has already proved the
    branch tip is the head of a merged pull request.
    """
    if tree.path == main:
        raise WorktreeError("refusing to remove the main checkout")
    blockers = removal_blockers(
        runner, tree.path, tree.branch if require_pushed else None
    )
    if blockers and not force:
        raise WorktreeError(
            "refusing to remove "
            + str(tree.path)
            + ":\n  - "
            + "\n  - ".join(blockers)
            + "\nCommit and push, or pass --force to discard."
        )
    git(
        runner,
        main,
        "worktree",
        "remove",
        *(["--force"] if force else []),
        str(tree.path),
    )
    print(f"removed worktree {tree.path}")
    if delete_branch and tree.branch:
        git(runner, main, "branch", "-D" if force else "-d", tree.branch)
        print(f"deleted local branch {tree.branch}")


PROBE = r"""
import importlib, importlib.metadata as md, json, re, sys, tomllib
from pathlib import Path

root = Path(sys.argv[1])
report = {"frisket": None, "broken": [], "drift": []}
try:
    import frisket
    report["frisket"] = str(Path(frisket.__file__).resolve())
except Exception as error:
    report["broken"].append(f"frisket: {type(error).__name__}: {error}")
locked = {
    package["name"]: package.get("version")
    for package in tomllib.loads((root / "uv.lock").read_text())["package"]
}
modules = md.packages_distributions()
by_dist = {}
for module, dists in modules.items():
    for dist in dists:
        by_dist.setdefault(re.sub(r"[-_.]+", "-", dist).lower(), []).append(module)
project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
for requirement in project.get("dependencies", []):
    if ";" in requirement:
        continue
    name = re.sub(r"[-_.]+", "-", re.match(r"[A-Za-z0-9_.-]+", requirement)[0]).lower()
    try:
        installed = md.version(name)
    except md.PackageNotFoundError:
        report["broken"].append(f"{name}: not installed")
        continue
    if locked.get(name) not in (None, installed):
        report["drift"].append(f"{name}: venv {installed}, uv.lock {locked[name]}")
    for module in sorted(set(by_dist.get(name, [])))[:1]:
        if module.startswith("_"):
            continue
        try:
            importlib.import_module(module)
        except Exception as error:
            report["broken"].append(f"{name} (import {module}): {type(error).__name__}: {error}")
print(json.dumps(report))
"""


def probe_environment(runner: Runner, main: Path, tree: Path) -> dict:
    """Import frisket and its top-level dependencies the way tests will."""
    python = main_python(main)
    if not python.exists():
        raise WorktreeError(
            f"main checkout venv is missing ({python}); create it in {main} first"
        )
    env = {**os.environ, "PYTHONPATH": str(tree / "src")}
    result = runner(
        [str(python), "-c", PROBE, str(tree)], cwd=tree, check=False, env=env
    )
    if result.returncode != 0:
        raise WorktreeError(
            f"environment probe crashed under {python}:\n{result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def check_probe(report: dict, main: Path, tree: Path) -> list[str]:
    """Refusals for a probe report; returns the non-fatal drift lines."""
    if report["broken"]:
        raise WorktreeError(
            "the main checkout venv cannot import what this worktree needs:\n  - "
            + "\n  - ".join(report["broken"])
            + f"\nFix it additively (nothing is removed):\n  cd {main} && {VENV_FIX}"
        )
    expected = (tree / "src" / "frisket").resolve()
    resolved = Path(report["frisket"] or "")
    if expected not in resolved.parents:
        raise WorktreeError(
            f"import frisket resolved to {resolved}, not {expected}/; "
            "PYTHONPATH is being overridden (check for a .pth file or "
            "PYTHONSAFEPATH/-I in the environment)"
        )
    return list(report["drift"])


def link_node_modules(main: Path, tree: Path) -> list[str]:
    notes = []
    for relative in NODE_MODULES:
        source, target = main / relative, tree / relative
        if not source.is_dir() or not target.parent.is_dir():
            continue
        if target.is_symlink() or target.exists():
            notes.append(f"kept existing {relative}")
            continue
        target.symlink_to(source, target_is_directory=True)
        notes.append(f"linked {relative} -> {source}")
    return notes


def copy_native(main: Path, tree: Path) -> str:
    source = main / NATIVE_DIR
    if not source.is_dir():
        return f"main checkout has no {NATIVE_DIR}; retrieval tests that need vec1 will skip"
    shutil.copytree(source, tree / NATIVE_DIR, dirs_exist_ok=True)
    return f"copied {NATIVE_DIR}"


def stage_sdk_contracts(runner: Runner, main: Path, tree: Path) -> str:
    """Copy the main checkout's generated contracts, regenerating when stale."""
    same_manifest = (main / SDK_MANIFEST).is_file() and (
        (main / SDK_MANIFEST).read_bytes() == (tree / SDK_MANIFEST).read_bytes()
    )
    if (main / SDK_CONTRACTS).is_file() and same_manifest:
        shutil.copy2(main / SDK_CONTRACTS, tree / SDK_CONTRACTS)
        return f"copied {SDK_CONTRACTS}"
    if shutil.which("node") is None:
        raise WorktreeError(
            f"{SDK_CONTRACTS} must be generated from this worktree's {SDK_MANIFEST} "
            "but node is not on PATH"
        )
    try:
        runner(["node", "sdk/scripts/gen-contracts.mjs"], cwd=tree)
    except subprocess.CalledProcessError as error:
        raise WorktreeError(
            f"gen-contracts.mjs failed: {error.stderr.strip()}"
        ) from None
    return f"generated {SDK_CONTRACTS} from this worktree's {SDK_MANIFEST}"


def env_lines(main: Path, tree: Path) -> list[str]:
    python = main_python(main)
    return [
        f"FRISKET_MAIN_CHECKOUT={main}",
        f"FRISKET_PYTHON={python}",
        f"PYTHONPATH={tree / 'src'}",
        f"FRISKET_TEST_PYTHON={python}",
    ]


def usage_lines(main: Path, tree: Path) -> list[str]:
    python = main_python(main)
    return [
        "Python tests (targeted, at most -n 4):",
        f"  cd {tree} && PYTHONPATH={tree / 'src'} {python} -m pytest -q "
        "-p no:cacheprovider tests/<path>",
        "Full Python suite (one machine-wide run at a time):",
        f"  {tree / 'scripts/dev/suite.py'}",
        "Web:",
        f"  cd {tree / 'web'} && FRISKET_TEST_PYTHON={python} npx vitest run",
        f"  cd {tree / 'web'} && npx tsc -b && npm run lint",
        f"Lint: {main / '.venv/bin/ruff'} check src tests",
        f"Or: set -a; . {tree / ENV_FILE}; set +a",
    ]


def provision(runner: Runner, tree: Path) -> None:
    tree = Path(git(runner, tree, "rev-parse", "--show-toplevel")).resolve()
    main = main_checkout(runner, tree).resolve()
    if tree == main:
        raise WorktreeError(f"{tree} is the main checkout; provision a lane worktree")
    drift = check_probe(probe_environment(runner, main, tree), main, tree)
    notes = [copy_native(main, tree), *link_node_modules(main, tree)]
    notes.append(stage_sdk_contracts(runner, main, tree))
    (tree / ENV_FILE).write_text("\n".join(env_lines(main, tree)) + "\n")
    notes.append(f"wrote {ENV_FILE}")
    print(f"provisioned {tree} against {main}")
    for note in notes:
        print(f"  {note}")
    print(f"  import frisket -> {tree / 'src/frisket'} (verified)")
    for line in drift:
        print(f"  warning: venv differs from this worktree's uv.lock: {line}")
    if drift:
        print(f"  (imports work; to align: cd {main} && {VENV_FIX})")
    print()
    print("\n".join(usage_lines(main, tree)))


def create(
    runner: Runner, name: str, base: str, prefix: str, *, provision_it: bool
) -> Path:
    main = main_checkout(runner)
    branch = f"{prefix}/{name}"
    path = main.parent / f"{prefix}-{name}"
    if path.exists():
        raise WorktreeError(f"{path} already exists")
    if (
        runner(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=main,
            check=False,
        ).returncode
        == 0
    ):
        raise WorktreeError(f"branch {branch} already exists")
    commit = git(runner, main, "rev-parse", "--verify", f"{base}^{{commit}}")
    git(runner, main, "worktree", "add", "-b", branch, str(path), commit)
    print(f"created {path} on {branch}")
    print(f"base {base} = {commit}")
    if provision_it:
        provision(runner, path)
    return path


def find_lane(runner: Runner, name: str, prefix: str) -> Worktree:
    branch = f"{prefix}/{name}"
    for tree in list_worktrees(runner)[1:]:
        if tree.branch == branch or tree.path.name == f"{prefix}-{name}":
            return tree
    raise WorktreeError(f"no worktree for branch {branch}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create_cmd = commands.add_parser(
        "create", help="add a worktree from the main checkout"
    )
    create_cmd.add_argument("name")
    create_cmd.add_argument("--base", default="main", help="commit-ish to branch from")
    create_cmd.add_argument("--prefix", default=DEFAULT_PREFIX, help="branch prefix")
    create_cmd.add_argument(
        "--no-provision", action="store_true", help="skip provisioning after create"
    )
    provision_cmd = commands.add_parser("provision", help="make a worktree runnable")
    provision_cmd.add_argument("path", nargs="?", type=Path, default=Path.cwd())
    remove_cmd = commands.add_parser("remove", help="remove a worktree and its branch")
    remove_cmd.add_argument("name")
    remove_cmd.add_argument("--prefix", default=DEFAULT_PREFIX, help="branch prefix")
    remove_cmd.add_argument(
        "--force", action="store_true", help="discard uncommitted or unpushed work"
    )
    remove_cmd.add_argument(
        "--keep-branch", action="store_true", help="keep the local branch"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            create(
                run,
                args.name,
                args.base,
                args.prefix,
                provision_it=not args.no_provision,
            )
        elif args.command == "provision":
            provision(run, args.path.resolve())
        else:
            remove_worktree(
                run,
                main_checkout(run),
                find_lane(run, args.name, args.prefix),
                force=args.force,
                delete_branch=not args.keep_branch,
            )
    except WorktreeError as error:
        print(f"worktree.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
