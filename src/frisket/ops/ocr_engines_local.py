"""Local RapidOCR and Tesseract OCR engines."""

from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
from contextlib import asynccontextmanager
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any

from frisket.engine._workers.local_engine_lease import (
    LocalEngineLease,
    LocalEngineLeaseBusy,
    acquire_local_engine_lease,
)
from frisket.engine._workers.rapidocr_models import (
    RapidOCRInvalidLanguage,
    rapidocr_default_model_requirements,
    rapidocr_model_requirements,
    rapidocr_model_root_complete,
)
from frisket.engine._workers.rapidocr_session import (
    RapidOCRProcessPool,
    RapidOCRRowError,
    RapidOCRSessionCancelled,
    RapidOCRSessionFailure,
    rapidocr_topology,
)
from frisket.engine.sandbox.shim import (
    SandboxPolicy,
    SandboxTeardownError,
    run_sandboxed,
)
from frisket.runtime.launch import worker_argv
from frisket.ops.base import RecipeInvocationHalt

LIGHT_ENGINE = "rapidocr"
TESSERACT_LANGUAGE_CODES = {"en": "eng"}


class OcrCancelled(RuntimeError):
    """Cooperative cancellation stopped local OCR before a row result."""


def _tesseract_language_code(language: str | None) -> str | None:
    if language is None or not language.strip():
        return None
    requested = language.strip().lower()
    try:
        return TESSERACT_LANGUAGE_CODES[requested]
    except KeyError as exc:
        choices = ", ".join(sorted(TESSERACT_LANGUAGE_CODES))
        raise ValueError(
            f"unsupported Tesseract language '{language}'; available: {choices}"
        ) from exc


RAPIDOCR_INSTALL_POINTER = (
    "RapidOCR is unavailable even though it is part of the base install. "
    "Repair the environment with `pip install --upgrade --force-reinstall "
    "frisket` or `uv sync`."
)

_RAPIDOCR_BUSY_DETAIL = (
    "another local RapidOCR session is still running; retry after it closes"
)
_RAPIDOCR_ARTIFACT_DETAIL = (
    "the local RapidOCR runtime or offline model files are unavailable; "
    "repair the base install and provision the default models before retrying"
)
_RAPIDOCR_SESSION_DETAIL = (
    "the local RapidOCR process pool stopped; completed rows were preserved "
    "and the run can be resumed"
)


@lru_cache(maxsize=1)
def rapidocr_available() -> tuple[bool, str | None]:
    """Whether the exact worker class is owned by the RapidOCR distribution."""
    try:
        from rapidocr import RapidOCR

        distribution = metadata.distribution("rapidocr")
        class_module = importlib.import_module(RapidOCR.__module__)
        module_file = getattr(class_module, "__file__", None)
        distribution_files = distribution.files
        if module_file is None or distribution_files is None:
            return False, RAPIDOCR_INSTALL_POINTER
        resolved_module = Path(module_file).resolve()
        if not any(
            Path(distribution.locate_file(item)).resolve() == resolved_module
            for item in distribution_files
        ):
            return False, RAPIDOCR_INSTALL_POINTER
    except Exception:
        return False, RAPIDOCR_INSTALL_POINTER
    return True, None


RAPIDOCR_MODELS_NOT_PROVISIONED = "rapidocr models not provisioned (offline)"


def rapidocr_shared_model_cache_dir() -> Path:
    """The machine-shared RapidOCR onnx-model dir dev venvs can point at
    instead of re-downloading. A sibling
    of frisket's existing model-weights cache (``ai.models.model_cache``,
    ``FRISKET_MODEL_CACHE_DIR`` -> ``~/.cache/frisket/models`` by default) —
    same env var, same root, one dev-cache story — under its own
    ``rapidocr/`` subdirectory since these are unpinned pip-extra assets, not
    manifest-tracked artifact-pull ones. ``scripts/dev/link_rapidocr_models.py``
    seeds it from any venv that already has real models; the runtime resolver
    passes a complete set through RapidOCR's ``Global.model_root_dir`` config
    key so a populated cache is used without needing that script run first."""
    from frisket.ai.models.model_cache import default_cache_root

    return default_cache_root() / "rapidocr"


@lru_cache(maxsize=1)
def rapidocr_models_present() -> tuple[bool, str | None]:
    """Whether the package contains one complete default det/cls/rec set.

    ``rapidocr_available`` deliberately answers only whether the pip package
    is installed correctly. This probe additionally resolves filenames through
    RapidOCR's own config/lookup API and verifies the package model root, so
    real-engine tests can skip honestly when offline. Runtime resolution also
    accepts the shared Frisket cache, but only when that *single* root contains
    the same complete set; see ``_rapidocr_model_root_dir``.
    """

    available, error = rapidocr_available()
    if not available:
        return False, error
    requirements = rapidocr_default_model_requirements()
    if requirements is None:
        return False, RAPIDOCR_MODELS_NOT_PROVISIONED
    package_root, filenames = requirements
    if not rapidocr_model_root_complete(package_root, filenames):
        return False, RAPIDOCR_MODELS_NOT_PROVISIONED
    return True, None


def _rapidocr_model_root_dir(language: str | None = None) -> str | None:
    """Resolve the offline model source once per pool, never once per row."""

    available, error = rapidocr_available()
    if not available:
        raise RecipeInvocationHalt(
            "local_artifact_unavailable",
            error or _RAPIDOCR_ARTIFACT_DETAIL,
        )
    try:
        requirements = rapidocr_model_requirements(language)
    except RapidOCRInvalidLanguage:
        # Keep invalid-language behavior row-local and actionable: the worker
        # owns the stable valid-code list and rejects it before model loading.
        return None
    if requirements is None:
        raise RecipeInvocationHalt(
            "local_artifact_unavailable",
            _RAPIDOCR_ARTIFACT_DETAIL,
        )
    package_root, filenames = requirements
    if rapidocr_model_root_complete(package_root, filenames):
        return None
    shared_dir = rapidocr_shared_model_cache_dir()
    if rapidocr_model_root_complete(shared_dir, filenames):
        return str(shared_dir)
    raise RecipeInvocationHalt(
        "local_artifact_unavailable",
        _RAPIDOCR_ARTIFACT_DETAIL,
    )


def _rapidocr_pool_kwargs(
    *,
    topology: Any,
    max_workers: int,
    expected_rows: int,
    language: str | None,
    model_root_dir: str | None,
    should_cancel: Any,
) -> dict[str, Any]:
    """Build one pool from the same host topology used by row admission."""

    return {
        "max_workers": max_workers,
        "expected_rows": expected_rows,
        "language": language,
        "model_root_dir": model_root_dir,
        "onnx_intra_threads": topology.onnx_intra_threads,
        "onnx_inter_threads": topology.onnx_inter_threads,
        "opencv_threads": topology.opencv_threads,
        "should_cancel": should_cancel,
        "memory_mb": topology.memory_mb,
    }


@asynccontextmanager
async def rapidocr_execution_scope(*, expected_rows, language, cancelled=None):
    """One invocation-owned pool, with its lease held until all children stop."""
    if cancelled is not None and cancelled():
        raise OcrCancelled("OCR cancelled")
    model_root = _rapidocr_model_root_dir(language)
    topology = rapidocr_topology(expected_rows)
    try:
        lease = acquire_local_engine_lease(LIGHT_ENGINE)
    except LocalEngineLeaseBusy as exc:
        raise RecipeInvocationHalt("local_engine_busy", _RAPIDOCR_BUSY_DETAIL) from exc
    try:
        pool = RapidOCRProcessPool(
            **_rapidocr_pool_kwargs(
                topology=topology,
                max_workers=topology.workers,
                expected_rows=expected_rows,
                language=language,
                model_root_dir=model_root,
                should_cancel=cancelled,
            )
        )
    except RapidOCRSessionCancelled as exc:
        lease.release()
        raise OcrCancelled("OCR cancelled") from exc
    except RapidOCRSessionFailure as exc:
        lease.release()
        raise RecipeInvocationHalt(
            "local_session_failed", _RAPIDOCR_SESSION_DETAIL
        ) from exc
    except BaseException:
        lease.release()
        raise
    try:
        yield pool
    finally:
        await _close_rapidocr_pool(pool, lease)


async def _close_rapidocr_pool(
    pool: RapidOCRProcessPool,
    lease: LocalEngineLease,
) -> None:
    """Release admission only after every pool child is verified stopped."""

    close_error: BaseException | None = None
    try:
        await pool.close()
    except BaseException as error:
        close_error = error
    if pool.teardown_failed or isinstance(close_error, SandboxTeardownError):
        lease.poison()
    else:
        lease.release()
    if close_error is None:
        return
    if isinstance(close_error, RapidOCRSessionCancelled):
        raise OcrCancelled("OCR cancelled") from close_error
    if isinstance(close_error, RapidOCRSessionFailure):
        raise RecipeInvocationHalt(
            "local_session_failed",
            _RAPIDOCR_SESSION_DETAIL,
        ) from close_error
    raise close_error


def tesseract_available() -> tuple[bool, str | None]:
    """Whether the local Tesseract engine can actually run: it needs BOTH the
    system ``tesseract`` binary (brew/apt — NOT a pip package) AND the
    ``pytesseract`` wrapper. Either half alone is not a working engine, so the
    honest catalog/doctor answer is unavailable-with-a-hint that names exactly
    what is missing (the ocr-compare tooltip pattern — never a bare False)."""
    binary = shutil.which("tesseract") is not None
    wrapper = importlib.util.find_spec("pytesseract") is not None
    if binary and wrapper:
        return True, None
    missing: list[str] = []
    if not binary:
        missing.append(
            "the tesseract system binary (macOS: 'brew install tesseract'; "
            "Debian/Ubuntu: 'apt install tesseract-ocr')"
        )
    if not wrapper:
        missing.append(
            "the pytesseract wrapper (repair the base Frisket install; it also "
            "ships the default RapidOCR engine)"
        )
    return False, "Tesseract OCR needs " + " and ".join(missing) + "."


async def ocr_rapidocr(pool, pages, scratch, language=None):
    if pool is None:
        raise RuntimeError("RapidOCR requires its invocation pool")
    return await run_rapidocr_pool(pool, pages)


async def run_rapidocr_pool(pool: RapidOCRProcessPool, pages: list[Path]) -> list[dict]:
    try:
        return await pool.ocr([str(page.absolute()) for page in pages])
    except RapidOCRSessionCancelled as error:
        raise OcrCancelled("OCR cancelled") from error
    except RapidOCRSessionFailure as error:
        raise RecipeInvocationHalt(
            "local_session_failed",
            _RAPIDOCR_SESSION_DETAIL,
        ) from error
    except RapidOCRRowError as error:
        raise RuntimeError(str(error)) from error


async def ocr_tesseract(
    pages: list[Path], scratch: Path, language: str | None = None, *, cancelled=None
) -> list[dict]:
    """Local Tesseract via pytesseract in the sandboxed worker. Fails fast
    with the enablement pointer when the binary or wrapper is absent (the
    honest-unavailable contract — same message the catalog/doctor surface),
    so the row error tells the user how to enable it rather than crashing
    deep in the worker."""
    available, err = tesseract_available()
    if not available:
        raise RuntimeError(err)
    out = scratch / "ocr-result.json"
    result = await run_sandboxed(
        worker_argv("tesseract"),
        policy=SandboxPolicy(
            cpu_seconds=1800,
            wall_seconds=3600,
            memory_mb=4096,
            # PATH so the worker finds the brew/apt tesseract binary
            env_passthrough=["PATH"],
        ),
        stdin_data=json.dumps(
            {
                "paths": [str(p) for p in pages],
                "out": str(out),
                "language": _tesseract_language_code(language),
            }
        ).encode(),
        should_cancel=cancelled,
    )
    if result.cancelled:
        raise OcrCancelled("OCR cancelled")
    if not result.ok or not out.exists():
        raise RuntimeError(
            f"tesseract worker failed: {(result.stderr or result.stdout)[:300]}"
        )
    data = json.loads(out.read_text())
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data["pages"]
