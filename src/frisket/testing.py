from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_LLM_CACHE_PATH = REPO_ROOT / "tests" / "cache" / "llm_cache.db"
TEST_CACHE_ENV = "FRISKET_TEST_CACHE_PATH"
REFRESH_CACHE_ENV = "FRISKET_CACHE_REFRESH"
KEEP_TEST_CACHE_ENV = "FRISKET_KEEP_TEST_CACHE"
TEST_CACHE_CLEANUP_ENV = "FRISKET_TEST_CACHE_CLEANUP"
TEST_CACHE_PRUNE_ENV = "FRISKET_TEST_CACHE_PRUNE"
TEST_CACHE_PRUNE_HOURS_ENV = "FRISKET_TEST_CACHE_PRUNE_HOURS"
DEFAULT_TEST_CACHE_PRUNE_HOURS = 48
_created_cache_dirs: set[Path] = set()
_cleanup_registered = False


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _falsey(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {
        "0",
        "false",
        "no",
        "off",
        "never",
    }


def refresh_cache_enabled() -> bool:
    """True when tests should write the committed replay fixture directly."""
    return _truthy(os.environ.get(REFRESH_CACHE_ENV))


def _cache_cleanup_enabled() -> bool:
    return not (
        _truthy(os.environ.get(KEEP_TEST_CACHE_ENV))
        or _falsey(os.environ.get(TEST_CACHE_CLEANUP_ENV))
    )


def _cache_prune_hours() -> float:
    raw = os.environ.get(TEST_CACHE_PRUNE_HOURS_ENV)
    try:
        value = float(raw) if raw else DEFAULT_TEST_CACHE_PRUNE_HOURS
    except ValueError:
        return DEFAULT_TEST_CACHE_PRUNE_HOURS
    return value if value > 0 else DEFAULT_TEST_CACHE_PRUNE_HOURS


def isolated_llm_cache_root() -> Path:
    return Path(tempfile.gettempdir()) / "frisket" / "llm-cache"


def cleanup_isolated_llm_caches() -> None:
    """Remove disposable replay-cache copies created by this process."""
    if not _cache_cleanup_enabled():
        return
    for cache_dir in list(_created_cache_dirs):
        shutil.rmtree(cache_dir, ignore_errors=True)
        _created_cache_dirs.discard(cache_dir)
    existing = os.environ.get(TEST_CACHE_ENV)
    if existing and not Path(existing).exists():
        os.environ.pop(TEST_CACHE_ENV, None)


def _register_cache_cleanup(cache_dir: Path) -> None:
    global _cleanup_registered
    _created_cache_dirs.add(cache_dir)
    if not _cleanup_registered:
        atexit.register(cleanup_isolated_llm_caches)
        _cleanup_registered = True


def prune_stale_isolated_llm_caches(*, now: float | None = None) -> list[Path]:
    """Remove old pytest/eval replay-cache copies left by interrupted runs."""
    if _falsey(os.environ.get(TEST_CACHE_PRUNE_ENV)):
        return []
    root = isolated_llm_cache_root()
    if not root.exists():
        return []
    cutoff = (now if now is not None else time.time()) - (_cache_prune_hours() * 3600)
    removed: list[Path] = []
    current = {p.resolve() for p in _created_cache_dirs}
    for child in root.iterdir():
        if not child.is_dir() or child.resolve() in current:
            continue
        if not (child.name.startswith("pytest-") or child.name.startswith("eval-")):
            continue
        try:
            if child.stat().st_mtime >= cutoff:
                continue
        except FileNotFoundError:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed.append(child)
    return removed


def ensure_isolated_llm_cache(*, run_id: str | None = None) -> Path:
    """Return the LLM cache path tests should use.

    Normal tests copy the committed real-response cache to a temp location and
    set ``FRISKET_TEST_CACHE_PATH`` so every router in the same run writes to
    the same disposable DB. Explicit refresh runs opt out with
    ``FRISKET_CACHE_REFRESH=1`` and write ``tests/cache/llm_cache.db``.
    """
    if refresh_cache_enabled():
        return CANONICAL_LLM_CACHE_PATH

    existing = os.environ.get(TEST_CACHE_ENV)
    if existing:
        return Path(existing)

    if not CANONICAL_LLM_CACHE_PATH.exists():
        raise FileNotFoundError(
            f"missing canonical LLM cache: {CANONICAL_LLM_CACHE_PATH}"
        )

    prune_stale_isolated_llm_caches()
    run = run_id or f"pytest-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    dest_dir = isolated_llm_cache_root() / run
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "llm_cache.db"
    shutil.copy2(CANONICAL_LLM_CACHE_PATH, dest)
    os.environ[TEST_CACHE_ENV] = str(dest)
    _register_cache_cleanup(dest_dir)
    return dest


def llm_cache_path() -> Path:
    """Path to use when constructing ``ResponseCache`` in tests."""
    return (
        CANONICAL_LLM_CACHE_PATH
        if refresh_cache_enabled()
        else ensure_isolated_llm_cache()
    )
