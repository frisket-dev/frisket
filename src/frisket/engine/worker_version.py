"""Code-identity stamping for workers and the queue (worker-version-guard-v1).

This guards against a stale worker sharing a live workspace's ``.queue.db``
with the server and silently claiming jobs with divergent code. Without a
version check, cells can be written by code the user never saw running and
there is no direct signal that this happened.

``code_version()`` is the cheapest reliable identity for "what code is this
process actually running": ``git describe`` against the checkout that owns
this file, falling back to a root ``VERSION`` file (packaged Docker deploys
without a ``.git`` dir), then installed package metadata (wheel installs),
and finally the literal string ``"unknown"`` (never raises — a version guard
must not itself break a worker).

Cached per-process (``functools.lru_cache``): this is called once per queue
enqueue and once per worker startup, not per row/job, so a single
subprocess call per process is cheap. Tests needing a fresh read call
``code_version.cache_clear()``.
"""

from __future__ import annotations

import subprocess
import re
from functools import lru_cache
from importlib import metadata as importlib_metadata
from pathlib import Path

from frisket import DISTRIBUTION_NAME

VERSION_FILE_NAME = "VERSION"
UNKNOWN_CODE_VERSION = "unknown"
_VERSION_IDENTITY_RE = re.compile(r"^[0-9a-f]{40}(?:\+[A-Za-z0-9._-]{1,87})?$")


def _repo_root() -> Path:
    """Walk up from this file looking for a `.git` dir or `pyproject.toml`
    (the checkout root), falling back to the frisket package root."""
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").exists():
            return candidate
    return here.parents[1]


def _git_describe(root: Path) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603, S607 — fixed argv, no shell
            ["git", "-C", str(root), "describe", "--always", "--dirty", "--abbrev=12"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _version_file(root: Path) -> str | None:
    try:
        value = (root / VERSION_FILE_NAME).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if _VERSION_IDENTITY_RE.fullmatch(value) is None:
        return None
    return value


def _installed_package_version() -> str | None:
    try:
        value = importlib_metadata.version(DISTRIBUTION_NAME).strip()
    except Exception:  # noqa: BLE001 — identity lookup must never break a worker
        return None
    return value or None


@lru_cache(maxsize=1)
def code_version() -> str:
    """This process's code identity. Never raises."""
    root = _repo_root()
    return (
        _git_describe(root)
        or _version_file(root)
        or _installed_package_version()
        or UNKNOWN_CODE_VERSION
    )


def require_known_code_identity(value: str, *, runtime: str) -> str:
    """Reject an identity-less product process before it can serve or work.

    Source checkouts may use ``git describe`` values (including ``-dirty``),
    while published images bake a full source SHA into ``VERSION``.  This
    boundary deliberately rejects only the missing/unknown state; callers
    decide whether their runtime is strict.
    """
    identity = value.strip()
    if not identity or identity.lower() == UNKNOWN_CODE_VERSION:
        raise RuntimeError(f"{runtime} requires a known code identity")
    return identity
