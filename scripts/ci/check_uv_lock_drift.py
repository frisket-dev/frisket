"""Guard against accidental uv.lock drift from rolling exclude-newer timestamps.

``pyproject.toml`` sets ``[tool.uv] exclude-newer = "1 week"``, so running
bare ``uv run`` or ``uv sync`` re-resolves packages and dirties ``uv.lock``
even when no dependency changed. This hook fails a commit when a ``uv.lock``
file is staged without its sibling ``pyproject.toml``, unless
``FRISKET_ALLOW_LOCK_ONLY=1`` is set for an intentional lock refresh.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

ALLOW_ENV = "FRISKET_ALLOW_LOCK_ONLY"
REPO = Path(__file__).resolve().parent.parent.parent


def _normalize(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).as_posix()


def check_staged_paths(
    staged: Iterable[str],
    *,
    allow_lock_only: bool = False,
) -> list[str]:
    """Return staged uv.lock paths missing their sibling pyproject.toml."""
    if allow_lock_only:
        return []
    normalized = {_normalize(path) for path in staged if path.strip()}
    offenders: list[str] = []
    for path in sorted(normalized):
        posix = PurePosixPath(path)
        if posix.name != "uv.lock":
            continue
        sibling_manifest = (posix.parent / "pyproject.toml").as_posix()
        if sibling_manifest not in normalized:
            offenders.append(path)
    return offenders


def staged_files(repo: Path = REPO) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in completed.stdout.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    _ = argv
    allow = os.environ.get(ALLOW_ENV) == "1"
    offenders = check_staged_paths(staged_files(), allow_lock_only=allow)
    if not offenders:
        return 0
    for lock_path in offenders:
        manifest = (PurePosixPath(lock_path).parent / "pyproject.toml").as_posix()
        print(
            f"uv-lock-guard: {lock_path} is staged without {manifest}.",
            file=sys.stderr,
        )
    print(
        "pyproject.toml sets exclude-newer = '1 week', so bare `uv run` / `uv sync` "
        "dirties uv.lock without a dependency change.\n"
        "  - To revert accidental drift: `git reset HEAD uv.lock && git checkout -- uv.lock` "
        "(and pass `--frozen` to `uv run` / `uv sync`).\n"
        f"  - For an intentional lock-only refresh (`uv lock --upgrade`): "
        f"`{ALLOW_ENV}=1 git commit ...`.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
