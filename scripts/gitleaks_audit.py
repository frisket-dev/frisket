#!/usr/bin/env python3
"""Run gitleaks over this repository, or refuse loudly when it cannot.

The pre-commit hook (`.pre-commit-config.yaml`, id `gitleaks-staged`) calls
`staged` on every commit. A secret scanner that silently does nothing is
worse than no scanner at all -- it reports success and teaches everyone the
repository is clean -- so a missing binary is a FAILURE here, with the
install command in the message, not a skip.

Modes:
  staged   what the hook runs: scan the staged diff only (fast).
  worktree scan uncommitted changes, staged or not, PLUS untracked files.
           Three passes, all of which must pass:
             1. `gitleaks git --staged`     -- staged, tracked changes.
             2. `gitleaks git --pre-commit` -- unstaged, tracked changes
                (this is plain `git diff`, despite the flag's name).
             3. `gitleaks dir <path>`, once per path reported by
                `git ls-files --others --exclude-standard` -- untracked
                files, which neither of the above ever sees. gitleaks
                `dir`'s positional argument is ONE path (verified against
                the installed binary: passing more than one silently drops
                the extras and scans the process's cwd instead -- it does
                not error, and it does not scan the requested list), so
                this is one invocation per untracked file rather than one
                call given the whole list.
  history  scan every commit reachable from HEAD (slow; the pre-publication
           gate, since a rewrite cannot un-leak what a push already served).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".gitleaks.toml"
INSTALL_HINT = (
    "gitleaks is not on PATH. Install it (https://github.com/gitleaks/gitleaks"
    " -- `brew install gitleaks`, or download a release binary) and re-run."
)

GIT_MODES = {
    "staged": ["--staged"],
    "pre_commit": ["--pre-commit"],
    "history": [],
}


def _config_args() -> list[str]:
    return ["--config", str(CONFIG)] if CONFIG.is_file() else []


def build_git_command(git_mode: str) -> list[str]:
    return [
        "gitleaks",
        "git",
        *GIT_MODES[git_mode],
        "--redact",
        "--no-banner",
        "--exit-code",
        "1",
        *_config_args(),
        str(ROOT),
    ]


def build_dir_command(path: Path) -> list[str]:
    """Scan exactly one file/directory. See the module docstring: `gitleaks
    dir` does not accept a file list -- extra positional args are silently
    dropped and the scan falls back to cwd -- so a caller wanting several
    specific paths must invoke this once per path."""
    return [
        "gitleaks",
        "dir",
        str(path),
        "--redact",
        "--no-banner",
        "--exit-code",
        "1",
        *_config_args(),
    ]


def untracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [ROOT / line for line in completed.stdout.splitlines() if line.strip()]


def _run(command: list[str], *, label: str) -> int:
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode == 0:
        return 0
    if completed.returncode == 1:
        print(
            f"gitleaks audit ({label}): findings above are REDACTED; the"
            " secret itself is in your working tree. Remove it and rotate the"
            " credential -- deleting the line does not un-leak a pushed one.",
            file=sys.stderr,
        )
        return 1
    print(
        f"gitleaks audit ({label}): scanner failed with exit code"
        f" {completed.returncode}; treating as a failure rather than a pass.",
        file=sys.stderr,
    )
    return completed.returncode


def _worst(a: int, b: int) -> int:
    """0 (clean) < 1 (leak found) < anything else (scanner itself failed) --
    a scanner failure always wins so it can never be masked by a plain
    leak-found result from an earlier pass."""
    rank = lambda code: 0 if code == 0 else (1 if code == 1 else 2)  # noqa: E731
    return b if rank(b) >= rank(a) else a


def run_worktree_mode() -> int:
    """Both tracked-change passes AND untracked files -- see module
    docstring. Every pass runs (one invocation reports every finding
    instead of stopping at the first) and the results combine via
    `_worst`."""
    result = 0
    result = _worst(result, _run(build_git_command("staged"), label="worktree/staged"))
    result = _worst(
        result, _run(build_git_command("pre_commit"), label="worktree/pre-commit")
    )

    paths = untracked_files()
    for path in paths:
        label = f"worktree/untracked:{path.relative_to(ROOT)}"
        result = _worst(result, _run(build_dir_command(path), label=label))

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=["staged", "worktree", "history"], nargs="?", default="staged"
    )
    args = parser.parse_args(argv)

    if shutil.which("gitleaks") is None:
        print(f"gitleaks audit ({args.mode}): {INSTALL_HINT}", file=sys.stderr)
        return 2

    if args.mode == "worktree":
        return run_worktree_mode()
    return _run(build_git_command(args.mode), label=args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
