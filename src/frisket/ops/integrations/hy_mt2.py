"""In-process Hy-MT2 1.8B GGUF local translation runtime.

The experimental "higher-quality still-local" translate tier: a single pinned
GGUF (Q4_K_M, ~1.13GB) loaded via ``llama-cpp-python`` on CPU. Sequenced AFTER
Opus-MT (the v1 default) and labelled experimental with NO quality claim.

Mirrors ``integrations/opus_mt.py``'s shape so the recipe hook stays thin:
- ``runtime_available()`` gates on the ``translate-gguf`` extra (llama.cpp);
- ``ensure_installed()`` provisions the pinned single-file artifact worker-
  locally on a runtime miss, with a per-host
  lock so concurrent rows pull it once;
- ``translate_texts()`` runs the model with the card's documented translation
  prompt through the GGUF's embedded chat template, deterministically.

Prompt format (verified against the model card, NOT guessed): the card's default
English translation instruction, sent as a single user message applied through
the model's embedded chat template (``apply_chat_template`` / llama.cpp
``--jinja``). No system prompt (the card states there is no default one). The
target is the FULL language name (the card requires this). Decoding is greedy
(``temperature=0``) with a small ``repeat_penalty`` -- deterministic, no
sampling variance across rows, and hardened against the degenerate-loop trap the
Opus-MT EOS bug exposed by a bounded ``max_tokens`` + repetition penalty.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from frisket.ops.integrations.translate_common import SUPPORTED_LANGUAGE_NAMES
from frisket.ai.models.artifact_manifest import HY_MT2_REF

REMEDIATION = "Install the local GGUF translation runtime: pip install 'frisket-data[translate-gguf]'"

# Deterministic per-row decoding: greedy, no sampling variance. The card
# recommends temperature 0.7 for general use, but per-row translation wants
# reproducibility, so we override to greedy and lean on repeat_penalty +
# a bounded max_tokens to stay faithful and never runaway-loop.
_TEMPERATURE = 0.0
_REPEAT_PENALTY = 1.05
_MAX_TOKENS = 2048
_N_CTX = 4096

# ISO code -> English name (Hy-MT2 wants the FULL target language name).
_CODE_TO_NAME = dict(SUPPORTED_LANGUAGE_NAMES)

# One loaded model per (path, mtime), guarded -- loading a ~1.1GB GGUF is the
# expensive part; mtime keys so a re-pull that replaces the bytes reloads
# (the same cross-process discipline as opus_mt's translator cache).
_MODEL_CACHE: dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()

_PROVISION_LOCK = threading.Lock()


class HyMt2NotInstalled(RuntimeError):
    """The pinned GGUF is not present in the model cache."""


class HyMt2RuntimeUnavailable(RuntimeError):
    """The ``translate-gguf`` extra (llama-cpp-python) is not installed."""


def runtime_available() -> bool:
    """Whether the in-process llama.cpp runtime imports."""
    try:
        import llama_cpp  # noqa: F401
    except ImportError:
        return False
    return True


def target_language_name(target: str) -> str:
    """Resolve a target to the FULL language name Hy-MT2 expects. Accepts an ISO
    code ("es" -> "Spanish") or a name ("Spanish" -> "Spanish", passthrough for
    any name the roster does not list)."""
    v = (target or "").strip()
    if not v:
        raise ValueError("missing target language")
    return _CODE_TO_NAME.get(v.lower(), v)


def _ref():
    from frisket.engine.jobs.artifact_ref import normalize_artifact_ref

    return normalize_artifact_ref(HY_MT2_REF)


def _model_path(cache_root: Path | None) -> Path:
    from frisket.ai.models import model_cache

    return model_cache.hf_install_path(_ref(), root=cache_root)


def is_installed(cache_root: Path | None = None) -> bool:
    from frisket.ai.models import artifact_manifest, model_cache

    pinned = artifact_manifest.hy_mt2_artifact()
    if pinned is None:
        return False
    return model_cache.is_installed(
        _ref(), [(f.repo_relpath, f.size) for f in pinned.files], root=cache_root
    )


def ensure_installed(
    cache_root: Path | None = None, client: Any = None, should_cancel: Any = None
) -> None:
    """Lazy pull-on-first-use: if the pinned
    GGUF is not on THIS worker, download + verify + install it worker-locally.
    Manifest-gated (checksum + license pinned) so no acknowledgment is needed.
    Raises :class:`HyMt2NotInstalled` if the artifact is not pinned."""
    if is_installed(cache_root=cache_root):
        return
    from frisket.engine.jobs import artifact_pull
    from frisket.ai.models import artifact_manifest

    pinned = artifact_manifest.hy_mt2_artifact()
    if pinned is None:
        raise HyMt2NotInstalled("Hy-MT2 GGUF is not pinned in the manifest")

    with _PROVISION_LOCK:
        if is_installed(cache_root=cache_root):
            return
        art = _ref()
        if client is not None:
            artifact_pull.provision_pinned(
                art,
                pinned,
                cache_root=cache_root,
                client=client,
                should_cancel=should_cancel,
            )
            return
        import httpx

        with httpx.Client() as owned_client:
            artifact_pull.provision_pinned(
                art,
                pinned,
                cache_root=cache_root,
                client=owned_client,
                should_cancel=should_cancel,
            )


def _load_model(path: Path) -> Any:
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    key = f"{path}:{mtime_ns}"
    with _CACHE_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached
    from llama_cpp import Llama

    model = Llama(
        model_path=str(path),
        n_ctx=_N_CTX,
        n_gpu_layers=0,
        verbose=False,
    )
    with _CACHE_LOCK:
        prefix = f"{path}:"
        for stale in [k for k in _MODEL_CACHE if k.startswith(prefix) and k != key]:
            _MODEL_CACHE.pop(stale, None)
        _MODEL_CACHE[key] = model
    return model


def _build_prompt(target_name: str, source_text: str) -> str:
    # The card's default English translation instruction (verified, not guessed).
    return (
        f"Translate the following text into {target_name}. Note that you should "
        "only output the translated result without any additional explanation:"
        f"\n\n{source_text}"
    )


def translate_texts(
    target: str,
    texts: list[str],
    *,
    cache_root: Path | None = None,
) -> list[str]:
    """Translate a batch into ``target`` (an ISO code or full name), in-process.

    Hy-MT2 auto-detects the source language (the card's default prompt names only
    the target), so no source hint is required. Deterministic greedy decoding.
    Raises :class:`HyMt2RuntimeUnavailable` if the extra is absent and
    :class:`HyMt2NotInstalled` if the GGUF is not in the cache."""
    if not runtime_available():
        raise HyMt2RuntimeUnavailable(REMEDIATION)
    path = _model_path(cache_root)
    if not path.is_file():
        raise HyMt2NotInstalled("Hy-MT2 GGUF is not installed")
    target_name = target_language_name(target)
    model = _load_model(path)

    out: list[str] = []
    for source_text in texts:
        messages = [
            {"role": "user", "content": _build_prompt(target_name, source_text)}
        ]
        result = model.create_chat_completion(
            messages=messages,
            temperature=_TEMPERATURE,
            repeat_penalty=_REPEAT_PENALTY,
            max_tokens=_MAX_TOKENS,
        )
        choice = (result.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        out.append(content.strip())
    return out


def _clear_model_cache() -> None:
    """Test/uninstall seam: drop loaded models (e.g. after an uninstall)."""
    with _CACHE_LOCK:
        _MODEL_CACHE.clear()


__all__ = [
    "HY_MT2_REF",
    "REMEDIATION",
    "HyMt2NotInstalled",
    "HyMt2RuntimeUnavailable",
    "ensure_installed",
    "is_installed",
    "runtime_available",
    "target_language_name",
    "translate_texts",
]
