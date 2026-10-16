"""Allowlisted durable engine setup operations."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Callable

from frisket.contracts.classification import LOCAL_CLASSIFIERS
from frisket.engine._workers.embeddinggemma_model import (
    SETUP_REF as EMBEDDINGGEMMA_SETUP_REF,
)
from frisket.engine._workers.indra_ocr_model import SETUP_REF as INDRA_OCR_SETUP_REF
from frisket.engine.jobs import model_pull_store

PARAKEET_TDT_SETUP_REF = "engine-setup:parakeet-tdt.local-onnx@1"
DOCLING_SETUP_REF = "engine-setup:docling.local@1"

_SETUP_NAMES = {
    PARAKEET_TDT_SETUP_REF: "Parakeet TDT (local ONNX)",
    DOCLING_SETUP_REF: "Docling",
    INDRA_OCR_SETUP_REF: "Indra OCR",
    EMBEDDINGGEMMA_SETUP_REF: "EmbeddingGemma 2",
    **{
        spec.setup_ref: {"gliclass": "GLiClass", "jeff": "Jeff"}.get(
            engine_id, engine_id
        )
        for engine_id, spec in LOCAL_CLASSIFIERS.items()
    },
}


def engine_setup_display_name(ref: str) -> str | None:
    return _SETUP_NAMES.get(ref)


def is_engine_setup_ref(ref: str) -> bool:
    return ref in _SETUP_NAMES


def run_engine_setup(
    *,
    engine,
    pull_id: int,
    should_cancel: Callable[[], bool],
    is_final_attempt: bool,
) -> dict[str, object]:
    """Provision an allowlisted engine bundle through its owned installer.

    ``resolve_parakeet_artifacts`` verifies each pinned cache component before
    it starts its supervised downloader, so a retry downloads only missing or
    invalid components.
    """
    row = model_pull_store.get(engine, pull_id)
    if row is None:
        raise LookupError(f"engine setup row {pull_id} does not exist")
    for engine_id, spec in LOCAL_CLASSIFIERS.items():
        if row.model_ref == spec.setup_ref:
            return _run_classifier_setup(
                engine_id=engine_id,
                engine=engine,
                pull_id=pull_id,
                should_cancel=should_cancel,
                is_final_attempt=is_final_attempt,
            )
    if row.model_ref == DOCLING_SETUP_REF:
        return _run_docling_setup(
            engine=engine,
            pull_id=pull_id,
            should_cancel=should_cancel,
            is_final_attempt=is_final_attempt,
        )
    if row.model_ref == EMBEDDINGGEMMA_SETUP_REF:
        return _run_embeddinggemma_setup(
            engine=engine,
            pull_id=pull_id,
            should_cancel=should_cancel,
            is_final_attempt=is_final_attempt,
        )
    if row.model_ref == INDRA_OCR_SETUP_REF:
        return _run_indra_ocr_setup(
            engine=engine,
            pull_id=pull_id,
            should_cancel=should_cancel,
            is_final_attempt=is_final_attempt,
        )

    from frisket.engine._workers.parakeet_artifacts import (
        ParakeetArtifactCancelled,
        ParakeetArtifactUnavailable,
        resolve_parakeet_artifacts,
    )
    from frisket.engine.jobs.model_pull import _fail

    if should_cancel():
        model_pull_store.mark_cancelled(engine, pull_id)
        return {"status": "cancelled"}
    model_pull_store.set_artifact_metadata(
        engine,
        pull_id,
        artifact_kind="engine_setup",
        artifact_source_url=None,
        artifact_license=None,
        artifact_manifest_version=None,
    )
    model_pull_store.update_progress(
        engine, pull_id, phase="provisioning", total_bytes=None, completed_bytes=0
    )
    try:
        asyncio.run(resolve_parakeet_artifacts(vad=True, should_cancel=should_cancel))
    except ParakeetArtifactCancelled:
        model_pull_store.mark_cancelled(engine, pull_id)
        return {"status": "cancelled"}
    except ParakeetArtifactUnavailable as exc:
        raise _fail(
            engine,
            pull_id,
            error_code="engine_setup_unavailable",
            message="could not provision the pinned Parakeet engine artifacts",
            terminal=False,
            is_final_attempt=is_final_attempt,
        ) from exc
    if should_cancel():
        model_pull_store.mark_cancelled(engine, pull_id)
        return {"status": "cancelled"}
    model_pull_store.mark_done(engine, pull_id)
    return {"status": "done"}


def _run_classifier_setup(
    *,
    engine_id: str,
    engine,
    pull_id: int,
    should_cancel: Callable[[], bool],
    is_final_attempt: bool,
) -> dict[str, object]:
    from frisket.engine._workers.classifier_artifacts import (
        cached_classifier_path,
        classifier_artifact,
    )
    from frisket.runtime.classifier_install import install_classifiers

    return _run_pinned_setup(
        engine=engine,
        pull_id=pull_id,
        should_cancel=should_cancel,
        is_final_attempt=is_final_attempt,
        label="local classifier",
        install=install_classifiers,
        artifact=lambda: classifier_artifact(engine_id),
        cached_path=lambda: cached_classifier_path(engine_id),
    )


def _run_indra_ocr_setup(
    *, engine, pull_id: int, should_cancel: Callable[[], bool], is_final_attempt: bool
) -> dict[str, object]:
    from frisket.engine._workers.indra_ocr_artifacts import (
        cached_indra_ocr_path,
        indra_ocr_artifact,
    )
    from frisket.runtime.indra_ocr_install import install_indra_ocr

    return _run_pinned_setup(
        engine=engine,
        pull_id=pull_id,
        should_cancel=should_cancel,
        is_final_attempt=is_final_attempt,
        label="Indra OCR",
        install=install_indra_ocr,
        artifact=indra_ocr_artifact,
        cached_path=cached_indra_ocr_path,
    )


def _run_embeddinggemma_setup(
    *, engine, pull_id: int, should_cancel: Callable[[], bool], is_final_attempt: bool
) -> dict[str, object]:
    from frisket.engine._workers.embeddinggemma_artifacts import (
        cached_embeddinggemma_path,
        embeddinggemma_artifact,
    )
    from frisket.runtime.embeddinggemma_install import install_embeddinggemma

    return _run_pinned_setup(
        engine=engine,
        pull_id=pull_id,
        should_cancel=should_cancel,
        is_final_attempt=is_final_attempt,
        label="multimodal search",
        install=install_embeddinggemma,
        artifact=embeddinggemma_artifact,
        cached_path=cached_embeddinggemma_path,
    )


def _run_pinned_setup(
    *,
    engine,
    pull_id: int,
    should_cancel: Callable[[], bool],
    is_final_attempt: bool,
    label: str,
    install: Callable,
    artifact: Callable,
    cached_path: Callable,
) -> dict[str, object]:
    """Complete one private runtime and its pinned snapshot before publishing ready."""
    from frisket.engine.jobs.artifact_pull import _run_hf_snapshot_pull
    from frisket.engine.jobs.model_pull import _fail
    from frisket.runtime.model_install import ModelInstallCancelled

    try:
        if should_cancel():
            raise ModelInstallCancelled
        model_pull_store.update_progress(
            engine,
            pull_id,
            phase="provisioning",
            total_bytes=None,
            completed_bytes=None,
        )
        install(should_cancel=should_cancel, progress=lambda _: None)
        pinned = artifact()
    except ModelInstallCancelled:
        model_pull_store.mark_cancelled(engine, pull_id)
        return {"status": "cancelled"}
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise _fail(
            engine,
            pull_id,
            error_code="engine_setup_unavailable",
            message=f"Could not set up {label}. Check your connection and available disk space, then retry.",
            terminal=False,
            is_final_attempt=is_final_attempt,
        ) from exc
    # The downloader owns its persisted failure code and retry state. Do not
    # relabel a network failure as a runtime installation failure.
    result = _run_hf_snapshot_pull(
        engine,
        pull_id,
        pinned,
        should_cancel=should_cancel,
        is_final_attempt=is_final_attempt,
        mark_done=False,
    )
    if result.get("status") == "cancelled" or should_cancel():
        model_pull_store.mark_cancelled(engine, pull_id)
        return {"status": "cancelled"}
    if cached_path() is None:
        raise _fail(
            engine,
            pull_id,
            error_code="engine_setup_unavailable",
            message=f"The {label} download is incomplete. Please retry setup.",
            terminal=False,
            is_final_attempt=is_final_attempt,
        )
    model_pull_store.mark_done(engine, pull_id, resolved_digest=pinned.composite_digest)
    return {"status": "done"}


def _run_docling_setup(
    *, engine, pull_id: int, should_cancel: Callable[[], bool], is_final_attempt: bool
) -> dict[str, object]:
    from frisket.engine.jobs.model_pull import _fail
    from frisket.runtime.model_install import ModelInstallCancelled, install_docling
    from frisket.runtime.model_server import (
        LOCAL_MODELS_TOKEN_ENV,
        LOCAL_MODELS_URL_ENV,
        wait_until_ready,
    )

    if should_cancel():
        model_pull_store.mark_cancelled(engine, pull_id)
        return {"status": "cancelled"}
    model_pull_store.set_artifact_metadata(
        engine,
        pull_id,
        artifact_kind="engine_setup",
        artifact_source_url=None,
        artifact_license=None,
        artifact_manifest_version=None,
    )
    model_pull_store.update_progress(
        engine, pull_id, phase="provisioning", total_bytes=None, completed_bytes=0
    )
    installed = False
    try:
        install_docling(should_cancel=should_cancel, progress=lambda _message: None)
        installed = True
        url = os.environ.get(LOCAL_MODELS_URL_ENV)
        token = os.environ.get(LOCAL_MODELS_TOKEN_ENV)
        if not url or not token:
            raise RuntimeError("managed model server is not configured")
        if not wait_until_ready(url, token, stopped=should_cancel):
            if should_cancel():
                raise ModelInstallCancelled
            raise RuntimeError("installed model server did not become ready")
    except ModelInstallCancelled:
        model_pull_store.mark_cancelled(engine, pull_id)
        return {"status": "cancelled"}
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise _fail(
            engine,
            pull_id,
            error_code=(
                "model_server_start_failed" if installed else "engine_setup_unavailable"
            ),
            message=(
                "Docling is installed, but its local server did not start. "
                "Restart Frisket and retry; check the local server log if it persists."
                if installed
                else "could not install the native Docling model server"
            ),
            terminal=False,
            is_final_attempt=is_final_attempt,
        ) from exc
    model_pull_store.mark_done(engine, pull_id)
    return {"status": "done"}


__all__ = [
    "DOCLING_SETUP_REF",
    "INDRA_OCR_SETUP_REF",
    "PARAKEET_TDT_SETUP_REF",
    "is_engine_setup_ref",
    "run_engine_setup",
]
