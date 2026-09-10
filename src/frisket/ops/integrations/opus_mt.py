"""In-process Opus-MT / CTranslate2 local translation runtime.

Loads a frisket-pulled CT2 pair from the model cache
(``<model_dir>/opus-mt/<src>-<tgt>/``) and translates in-process on CPU (int8),
with the Opus-MT SentencePiece tokenizers -- no torch, ~412 chars/s (the
benchmark that made Opus-MT the local default). Packaged behind the optional
``standard`` tier (``ctranslate2`` + ``sentencepiece``); absent-runtime ->
``runtime_available()`` is False and the engine reports unavailable with a
``pip install 'frisket[standard]'`` remediation, exactly like local ASR.

This module lives on its own so the heavy runtime import is isolated;
``TranslateRecipe.execute`` calls in with a thin dispatch hook.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from frisket.ops.integrations.translate_common import SUPPORTED_LANGUAGE_NAMES

REMEDIATION = "Install the local translation runtime: pip install 'frisket[standard]'"

# name (lowercased) -> ISO-639-1 code, inverted from the shared roster so the
# recipe can accept either a code ("es") or an English name ("Spanish") for the
# target while the pair id stays code-based.
_NAME_TO_CODE = {name.lower(): code for code, name in SUPPORTED_LANGUAGE_NAMES.items()}

_CT2_REQUIRED_FILES = ("model.bin", "source.spm", "target.spm")

# One loaded Translator per pair, guarded (CT2 Translators are reusable across
# calls; loading is the expensive part).
_TRANSLATOR_CACHE: dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()

# Per-pair provisioning locks: a per-host (per-process) mutex so concurrent
# translate rows needing the SAME uninstalled pair provision it once, not N
# times.
_PROVISION_LOCKS: dict[str, threading.Lock] = {}
_PROVISION_LOCKS_GUARD = threading.Lock()


class OpusPairNotInstalled(RuntimeError):
    """The requested CT2 pair is not present in the model cache."""


class OpusRuntimeUnavailable(RuntimeError):
    """The ``translate`` extra (ctranslate2 / sentencepiece) is not installed."""


def runtime_available() -> bool:
    """Whether the in-process CT2 runtime deps import."""
    try:
        import ctranslate2  # noqa: F401
        import sentencepiece  # noqa: F401
    except ImportError:
        return False
    return True


def resolve_pair_codes(source: str, target: str) -> tuple[str, str]:
    """Map a (source, target) selection to a ``(src, tgt)`` ISO-639-1 pair.

    Accepts either an ISO code ("es") or an English name ("Spanish") on each
    side. Raises ``ValueError`` if either side is unrecognized (the recipe
    surfaces this as ``invalid_source``/``invalid_target``)."""

    def _code(value: str, role: str) -> str:
        v = (value or "").strip()
        if not v:
            raise ValueError(f"missing {role} language")
        low = v.lower()
        if low in SUPPORTED_LANGUAGE_NAMES:  # already a code
            return low
        if low in _NAME_TO_CODE:
            return _NAME_TO_CODE[low]
        raise ValueError(f"unsupported {role} language {value!r}")

    return _code(source, "source"), _code(target, "target")


def _pair_dir(src: str, tgt: str, cache_root: Path | None) -> Path:
    from frisket.engine.jobs.artifact_ref import normalize_artifact_ref
    from frisket.ai.models import model_cache

    art = normalize_artifact_ref(f"opus-mt:{src}-{tgt}")
    return model_cache.artifact_install_dir(art, root=cache_root)


def _dir_has_pair(directory: Path) -> bool:
    return all((directory / name).is_file() for name in _CT2_REQUIRED_FILES)


def installed_pairs(cache_root: Path | None = None) -> list[str]:
    """Canonical pair ids (``en-es``) present on disk (model.bin + both SP
    tokenizers). This is the translate catalog's INSTALLED-pair list."""
    from frisket.ai.models import model_cache

    base = (cache_root or model_cache.default_cache_root()) / "opus-mt"
    if not base.is_dir():
        return []
    out: list[str] = []
    for child in base.iterdir():
        if child.is_dir() and _dir_has_pair(child):
            out.append(child.name)
    return sorted(out)


def is_pair_installed(src: str, tgt: str, cache_root: Path | None = None) -> bool:
    return _dir_has_pair(_pair_dir(src, tgt, cache_root))


def _load_translator(directory: Path) -> Any:
    # Key by (dir, model.bin mtime) so a fresh pull that atomically replaces
    # the weights (os.replace -> new mtime) is never served from a cache
    # entry loaded off the OLD bytes while provenance claims the new ones.
    # mtime keying works cross-process (the pull worker and the run worker
    # may differ), unlike an in-process invalidate-on-promote hook.
    try:
        mtime_ns = (directory / "model.bin").stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    key = f"{directory}:{mtime_ns}"
    with _CACHE_LOCK:
        cached = _TRANSLATOR_CACHE.get(key)
        if cached is not None:
            return cached
    import ctranslate2

    translator = ctranslate2.Translator(
        str(directory), device="cpu", compute_type="int8"
    )
    with _CACHE_LOCK:
        # Drop any stale entry for the same directory (a superseded mtime) so
        # the cache does not grow unbounded across re-pulls of the same pair.
        prefix = f"{directory}:"
        for stale in [
            k for k in _TRANSLATOR_CACHE if k.startswith(prefix) and k != key
        ]:
            _TRANSLATOR_CACHE.pop(stale, None)
        _TRANSLATOR_CACHE[key] = translator
    return translator


def translate_texts(
    src: str,
    tgt: str,
    texts: list[str],
    *,
    cache_root: Path | None = None,
) -> list[str]:
    """Translate a batch from ``src`` to ``tgt`` (ISO codes) in-process.

    Raises :class:`OpusRuntimeUnavailable` if the extra is absent and
    :class:`OpusPairNotInstalled` if the pair is not in the cache. Blocking CPU
    work -- callers on an event loop should wrap it in ``asyncio.to_thread``.
    """
    if not runtime_available():
        raise OpusRuntimeUnavailable(REMEDIATION)
    directory = _pair_dir(src, tgt, cache_root)
    if not _dir_has_pair(directory):
        raise OpusPairNotInstalled(f"opus-mt:{src}-{tgt} is not installed")

    import sentencepiece

    sp_src = sentencepiece.SentencePieceProcessor(
        model_file=str(directory / "source.spm")
    )
    sp_tgt = sentencepiece.SentencePieceProcessor(
        model_file=str(directory / "target.spm")
    )
    translator = _load_translator(directory)

    out: list[str] = []
    # Opus-MT / Marian models REQUIRE the source terminated with the EOS token
    # `</s>`: the SentencePiece pieces alone leave the decoder with no sentence
    # boundary and it degenerates into a repetition loop ("Hola hola hola...").
    # Appending `</s>` (as the MarianTokenizer does) is what makes the CT2
    # weights produce faithful output ("Hello, world." -> "Hola, mundo."). A
    # small beam gives a stable hypothesis over greedy decoding.
    batch_tokens = [sp_src.encode(text, out_type=str) + ["</s>"] for text in texts]
    results = translator.translate_batch(batch_tokens, beam_size=4)
    for result in results:
        hypothesis = result.hypotheses[0] if result.hypotheses else []
        # Drop any trailing EOS the model emits before detokenizing.
        if hypothesis and hypothesis[-1] == "</s>":
            hypothesis = hypothesis[:-1]
        out.append(sp_tgt.decode(hypothesis))
    return out


def _pair_provision_lock(pair: str) -> threading.Lock:
    with _PROVISION_LOCKS_GUARD:
        lock = _PROVISION_LOCKS.get(pair)
        if lock is None:
            lock = threading.Lock()
            _PROVISION_LOCKS[pair] = lock
        return lock


def ensure_pair_installed(
    src: str,
    tgt: str,
    *,
    cache_root: Path | None = None,
    client: Any = None,
    should_cancel: Any = None,
) -> None:
    """Lazy pull-on-first-use: if the pinned pair is not present on
    THIS worker, download + verify + install it worker-locally, then return.

    This is what makes team "lazy-per-worker" provisioning honest -- a translate
    row can land on any worker (the run queue has no host affinity), so the
    executing worker provisions the pair the first time it needs it. The pair is
    manifest-gated (checksum + license pinned), so NO acknowledgment is needed.
    A per-host (per-process) lock + a re-check dedupes concurrent rows so the
    pair is pulled once, not once per row.

    Raises :class:`OpusPairNotInstalled` if the pair is not a pinned manifest
    entry (an unpinned pair cannot be provisioned on-use), or the underlying
    provisioning error on a download/checksum failure."""
    if is_pair_installed(src, tgt, cache_root=cache_root):
        return
    from frisket.engine.jobs import artifact_pull
    from frisket.engine.jobs.artifact_ref import normalize_artifact_ref
    from frisket.ai.models import artifact_manifest

    pair = f"{src}-{tgt}"
    art = normalize_artifact_ref(f"opus-mt:{pair}")
    pinned = artifact_manifest.lookup(art.canonical)
    if pinned is None:
        raise OpusPairNotInstalled(f"opus-mt:{pair} is not a pinned pair")

    with _pair_provision_lock(pair):
        # Re-check under the lock: another row may have provisioned it while we
        # waited (no double-pull).
        if is_pair_installed(src, tgt, cache_root=cache_root):
            return
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


def _clear_translator_cache() -> None:
    """Test/uninstall seam: drop loaded Translators (e.g. after an uninstall)."""
    with _CACHE_LOCK:
        _TRANSLATOR_CACHE.clear()


__all__ = [
    "REMEDIATION",
    "OpusPairNotInstalled",
    "OpusRuntimeUnavailable",
    "ensure_pair_installed",
    "installed_pairs",
    "is_pair_installed",
    "resolve_pair_codes",
    "runtime_available",
    "translate_texts",
]
