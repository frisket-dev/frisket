"""Embeddings doctor / live-proof harness (Lane E, native-embedding-doctor).

``report_embeddings`` returns a DETERMINISTIC structured report (pure data, no
printing, no network when nothing is configured) describing the whole embedding
product path: fastembed availability + model cache, provider key PRESENCE (by
NAME only — never a value), sidecar embedding-route health, the scheduler config,
and the current live-proof skip/run status. It mirrors the existing CLI doctor's
INFO-probe ethos: missing config is a NOTE, never a failure, and a probe never
crashes the report.

Secrets discipline (load-bearing): provider keys are reported as
``key_present: bool`` derived from ``router.providers()`` / env-var PRESENCE. No
key value is ever read into the report. A test asserts no secret substring
appears anywhere in ``json.dumps(report)``.

The live_proofs section mirrors the runtime gates in the live tests:
- ``local_real`` ← ``tests/ai/test_native_embedding_real_local.py`` (skip when
  ``local_embedder()`` is None: fastembed absent OR FRISKET_DISABLE_LOCAL_EMBED=1)
- ``remote_<provider>`` ← ``tests/ai/test_remote_embeddings_live.py`` (skip when the
  provider's key env is not set).
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path
from typing import Any

# Provider -> the key env var the live remote-embedding test gates on
# (mirrors tests/ai/test_remote_embeddings_live.py _CASES + OpenRouter).
_PROVIDER_KEY_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

# Providers whose REMOTE embedding live-proof is key-gated (the local-real proof
# is handled separately). anthropic has no embedding live test today, so it is a
# provider-key row but not a remote live-proof row.
_REMOTE_LIVE_PROVIDERS: tuple[str, ...] = ("openai", "gemini", "openrouter")


def _env_value(env: dict[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def _key_present(env: dict[str, str], router: Any, provider: str, key_env: str) -> bool:
    """Key presence by NAME only: env-var PRESENCE OR the router advertising the
    adapter. Never reads or returns the value."""
    if _env_value(env, key_env):
        return True
    if router is not None:
        try:
            return provider in set(router.providers())
        except Exception:  # noqa: BLE001 — doctor reports, never crashes
            return False
    return False


def _fastembed_section(env: dict[str, str]) -> dict[str, Any]:
    installed = importlib.util.find_spec("fastembed") is not None
    disabled = env.get("FRISKET_DISABLE_LOCAL_EMBED") == "1"
    # The resolved local model id (the catalog default; the same id local_embedder
    # hands fastembed). Imported lazily so the doctor works even if semantic.py's
    # heavy deps are partly absent.
    try:
        from frisket.semantic import LOCAL_MODEL

        model_id = LOCAL_MODEL
    except Exception:  # noqa: BLE001
        model_id = None

    cache = _fastembed_cache_status(env)
    return {
        "installed": installed,
        "disabled_by_env": disabled,
        "active": installed and not disabled,
        "local_model_id": model_id,
        "cache": cache,
    }


def _fastembed_cache_dir(env: dict[str, str]) -> Path:
    """Where fastembed stores model weights: FASTEMBED_CACHE_PATH or the default
    ``<tempdir>/fastembed_cache`` (matches fastembed's define_cache_dir, also
    noted in search.py). ``tempfile.gettempdir()`` checks TMPDIR/TEMP/TMP and
    the platform-specific Windows fallback locations -- no hand-written
    literal ``/tmp`` fallback."""
    explicit = _env_value(env, "FASTEMBED_CACHE_PATH")
    if explicit:
        return Path(explicit)
    return Path(tempfile.gettempdir()) / "fastembed_cache"


def _fastembed_cache_status(env: dict[str, str]) -> dict[str, Any]:
    path = _fastembed_cache_dir(env)
    exists = False
    approx_bytes: int | None = None
    try:
        exists = path.is_dir()
        if exists:
            approx_bytes = _approx_dir_bytes(path)
    except OSError:
        exists = False
        approx_bytes = None
    return {
        "path": str(path),
        "exists": exists,
        "approx_bytes": approx_bytes,
    }


def _approx_dir_bytes(path: Path) -> int | None:
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return None
    return total


def _providers_section(env: dict[str, str], router: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for provider, key_env in _PROVIDER_KEY_ENV.items():
        out[provider] = {
            "key_env": key_env,  # the NAME of the var, not its value
            "key_present": _key_present(env, router, provider, key_env),
        }
    return out


def _redact_url(url: str) -> str:
    """Strip any embedded credentials (``user:pass@host``) from a URL so the doctor
    never echoes a secret carried in FRISKET_MODELS_URL (the report's no-secret-leak
    guarantee covers the sidecar URL, not just provider keys)."""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        if "@" in parts.netloc:
            parts = parts._replace(netloc=parts.netloc.rsplit("@", 1)[1])
        return urlunsplit(parts)
    except Exception:  # noqa: BLE001 — a malformed URL is reported as-redacted-best-effort
        return url


def _url_userinfo(url: str) -> str | None:
    """The ``user:pass`` credential substring of a URL, if present, so it can be
    scrubbed from free-text error details."""
    try:
        from urllib.parse import urlsplit

        netloc = urlsplit(url).netloc
        return netloc.rsplit("@", 1)[0] if "@" in netloc else None
    except Exception:  # noqa: BLE001
        return None


def _sidecar_section(env: dict[str, str]) -> dict[str, Any]:
    """Configured = FRISKET_MODELS_URL set. UNCONFIGURED is a quiet, no-network
    note (mirrors cli.py doctor). When configured we probe GET /capabilities the
    same way the existing doctor does; any error is reported, never raised. The URL
    is REDACTED of any embedded credentials before it enters the report."""
    base = _env_value(env, "FRISKET_MODELS_URL")
    if not base:
        return {
            "configured": False,
            "status": "unconfigured",
            "detail": "FRISKET_MODELS_URL unset (sidecar engines disabled)",
        }

    section: dict[str, Any] = {"configured": True, "url": _redact_url(base)}
    token = _env_value(env, "FRISKET_MODELS_TOKEN")
    try:
        import httpx

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = httpx.get(
            f"{base.rstrip('/')}/capabilities", headers=headers, timeout=5.0
        )
        resp.raise_for_status()
        body = resp.json()
        engines = body.get("engines") or []
        section["status"] = "reachable"
        section["reachable"] = True
        section["version"] = body.get("version")
        section["engines"] = [
            {"name": e.get("name"), "available": bool(e.get("available"))}
            for e in engines
        ]
    except Exception as exc:  # noqa: BLE001 — doctor reports, never crashes
        section["status"] = "unreachable"
        section["reachable"] = False
        # Scrub any credential carried in the URL from the (URL-bearing) error text.
        detail = str(exc).replace(base, _redact_url(base))
        creds = _url_userinfo(base)
        if creds:
            detail = detail.replace(creds, "***")
        section["detail"] = detail
    return section


def _scheduler_section(
    env: dict[str, str], workspace_root: str | Path | None
) -> dict[str, Any]:
    """FRISKET_EMBEDDING_SCHEDULER_INTERVAL (0/unset = disabled) + a count of
    maintenance-scheduled indexes across the workspace. The count is skipped (not
    failed) when no workspace_root is supplied — the report stays pure data."""
    raw = _env_value(env, "FRISKET_EMBEDDING_SCHEDULER_INTERVAL")
    interval: float | None
    try:
        interval = float(raw) if raw else 0.0
    except ValueError:
        interval = None

    if interval is None:
        enabled = False
        interval_note: Any = f"invalid ({raw!r})"
    else:
        enabled = interval > 0
        interval_note = interval

    section: dict[str, Any] = {
        "interval_seconds": interval_note,
        "enabled": enabled,
        "status": "enabled" if enabled else "disabled",
    }
    if workspace_root is None:
        section["scheduled_indexes"] = "skipped (no workspace_root)"
        return section

    section["scheduled_indexes"] = _count_scheduled_indexes(workspace_root)
    return section


def _count_scheduled_indexes(workspace_root: str | Path) -> int | str:
    """Mirror enqueue_due_scheduled_refreshes' workspace walk: every ``*.frisket``
    bundle with a manifest, count its maintenance-scheduled indexes. Any error is
    reported as a note, never raised."""
    try:
        from frisket.engine.jobs.embeddings import find_scheduled_indexes
        from frisket.engine.store import Project

        root = Path(workspace_root)
        if not root.is_dir():
            return 0
        total = 0
        for bundle in sorted(root.glob("*.frisket")):
            if not (bundle / "manifest.json").exists():
                continue
            project = Project(bundle)
            try:
                total += len(find_scheduled_indexes(project))
            finally:
                try:
                    project.close()
                except Exception:  # noqa: BLE001
                    pass
        return total
    except Exception as exc:  # noqa: BLE001 — doctor reports, never crashes
        return f"unavailable ({exc})"


def _local_embedder_available(env: dict[str, str]) -> bool:
    """Mirror local_embedder()'s availability gate WITHOUT loading a model:
    FRISKET_DISABLE_LOCAL_EMBED!=1 AND fastembed importable."""
    if env.get("FRISKET_DISABLE_LOCAL_EMBED") == "1":
        return False
    return importlib.util.find_spec("fastembed") is not None


def _live_proofs_section(env: dict[str, str], router: Any) -> dict[str, Any]:
    """Mirror the runtime gates of the optional live tests."""
    proofs: dict[str, Any] = {}

    # local-real proof: tests/ai/test_native_embedding_real_local.py
    if _local_embedder_available(env):
        proofs["local_real"] = {
            "optional": True,
            "test": "tests/ai/test_native_embedding_real_local.py",
            "status": "would_run",
            "reason": "bundled local embedder available (fastembed, not disabled)",
        }
    elif env.get("FRISKET_DISABLE_LOCAL_EMBED") == "1":
        proofs["local_real"] = {
            "optional": True,
            "test": "tests/ai/test_native_embedding_real_local.py",
            "status": "would_skip",
            "reason": "FRISKET_DISABLE_LOCAL_EMBED=1 forces the non-local path",
        }
    else:
        proofs["local_real"] = {
            "optional": True,
            "test": "tests/ai/test_native_embedding_real_local.py",
            "status": "would_skip",
            "reason": "bundled fastembed runtime missing (reinstall Frisket)",
        }

    # remote-live proofs: tests/ai/test_remote_embeddings_live.py (one per provider)
    for provider in _REMOTE_LIVE_PROVIDERS:
        key_env = _PROVIDER_KEY_ENV[provider]
        present = _key_present(env, router, provider, key_env)
        proofs[f"remote_{provider}"] = {
            "optional": True,
            "key_gated": True,
            "test": "tests/ai/test_remote_embeddings_live.py",
            "provider": provider,
            "key_env": key_env,
            "status": "would_run" if present else "would_skip",
            "reason": (
                f"{key_env} present"
                if present
                else f"{key_env} not set — remote live check skipped"
            ),
        }
    return proofs


def report_embeddings(
    *,
    env: dict[str, str] | None = None,
    router: Any = None,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    """Deterministic embeddings doctor report (pure data, no printing).

    ``env`` defaults to ``os.environ`` (tests pass a dict for determinism).
    ``router`` is consulted by NAME ONLY (``router.providers()``); pass a fake or
    None. ``workspace_root`` enables the scheduled-index count (skipped when None).
    No network call happens unless FRISKET_MODELS_URL is configured.
    """
    env = dict(os.environ) if env is None else env
    return {
        "fastembed": _fastembed_section(env),
        "providers": _providers_section(env, router),
        "sidecar": _sidecar_section(env),
        "scheduler": _scheduler_section(env, workspace_root),
        "live_proofs": _live_proofs_section(env, router),
    }
