"""scripts/gitleaks_audit.py worktree mode (bugfix, 2026-07-31).

The docstring promises worktree mode covers "uncommitted changes, staged or
not," but the implementation mapped `worktree` to a single `--pre-commit`
pass -- which gitleaks defines as plain `git diff` (unstaged, TRACKED
changes only). Two classes of secret slipped straight through:

* a STAGED secret (git add'ed, not yet committed) -- `--pre-commit` never
  sees the index at all;
* an UNTRACKED secret (a brand-new file, never `git add`ed) -- neither
  `--staged` nor `--pre-commit` scans anything gitleaks doesn't already know
  is part of the repository's git-tracked history/index.

The fix runs three passes (`--staged`, `--pre-commit`, and one `gitleaks
dir` call per path from `git ls-files --others --exclude-standard`) and
fails if any of them finds something. This suite proves each of the two
previously-invisible cases independently, plus the true-negative control.

`gitleaks` IS installed in this dev environment (required by the CLAUDE.md
gate instructions for this bugfix), so this test always actually runs here;
the self-skip below only protects a machine that genuinely lacks the
binary.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "gitleaks_audit.py"

pytestmark = pytest.mark.skipif(
    shutil.which("gitleaks") is None,
    reason="gitleaks binary not on PATH",
)

# A generic-api-key-shaped sentinel: reliably matched by gitleaks' default
# (unconfigured) ruleset, with no provider-specific prefix to keep it honest
# about testing the *mechanism*, not one specific rule. `gitleaks:allow`
# keeps this literal from tripping our OWN gitleaks-staged pre-commit hook
# once this test file is committed -- it is a deliberately shaped fixture,
# never a real credential.
SECRET_LINE = 'api_key = "1234567890abcdefghij1234567890ABCDEFGHIJ"\n'  # gitleaks:allow


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "_gitleaks_audit_under_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "README.md").write_text("clean repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _run_worktree(module, repo: Path) -> int:
    original_root, original_config = module.ROOT, module.CONFIG
    module.ROOT = repo
    module.CONFIG = repo / ".gitleaks.toml"  # deliberately absent -> default ruleset
    try:
        return module.main(["worktree"])
    finally:
        module.ROOT, module.CONFIG = original_root, original_config


def test_clean_worktree_passes(tmp_path: Path) -> None:
    module = _load_module()
    repo = _init_repo(tmp_path)
    assert _run_worktree(module, repo) == 0


def test_staged_only_secret_fails(tmp_path: Path) -> None:
    """git add'ed but not committed -- --pre-commit (plain `git diff`) does
    not see it; only the --staged pass does."""
    module = _load_module()
    repo = _init_repo(tmp_path)
    leaking = repo / "config.py"
    leaking.write_text(SECRET_LINE, encoding="utf-8")
    subprocess.run(["git", "add", "config.py"], cwd=repo, check=True)

    assert _run_worktree(module, repo) == 1


def test_untracked_secret_fails(tmp_path: Path) -> None:
    """A brand-new file, never `git add`ed -- invisible to both `--staged`
    and `--pre-commit`; only the `gitleaks dir` pass over
    `git ls-files --others --exclude-standard` sees it."""
    module = _load_module()
    repo = _init_repo(tmp_path)
    leaking = repo / "notes.txt"
    leaking.write_text(SECRET_LINE, encoding="utf-8")
    # Deliberately no `git add` -- this is the untracked case.

    assert _run_worktree(module, repo) == 1


def test_untracked_files_enumerates_via_git_ls_files(tmp_path: Path) -> None:
    """Direct unit check on the enumeration helper, independent of the
    scanner subprocess: an untracked file appears, a tracked/committed one
    does not, and a staged-but-uncommitted one does not (git ls-files
    --others reports only files git doesn't know about at all)."""
    module = _load_module()
    repo = _init_repo(tmp_path)
    (repo / "untracked.txt").write_text("x\n", encoding="utf-8")
    staged = repo / "staged.txt"
    staged.write_text("y\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.txt"], cwd=repo, check=True)

    original_root = module.ROOT
    module.ROOT = repo
    try:
        found = {p.name for p in module.untracked_files()}
    finally:
        module.ROOT = original_root

    assert found == {"untracked.txt"}
