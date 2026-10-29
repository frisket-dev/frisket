"""Allowlisted durable engine setup operations."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from frisket.contracts.classification import LOCAL_CLASSIFIERS
from frisket.engine._workers.embeddinggemma_model import (
    SETUP_REF as EMBEDDINGGEMMA_SETUP_REF,
)
from frisket.engine._workers.indra_ocr_model import SETUP_REF as INDRA_OCR_SETUP_REF
from frisket.engine.jobs import model_pull_store

PARAKEET_TDT_SETUP_REF = "engine-setup:parakeet-tdt.local-onnx@1"
FASTER_WHISPER_SETUP_REF = "engine-setup:faster-whisper.local@1"
RAPIDOCR_SETUP_REF = "engine-setup:rapidocr.local@1"
DOCLING_SETUP_REF = "engine-setup:docling.local@1"
CHROMIUM_SETUP_REF = "engine-setup:chromium.local@1"

_SETUP_NAMES = {
    CHROMIUM_SETUP_REF: "Browser tools (Chromium)",
    PARAKEET_TDT_SETUP_REF: "Parakeet TDT (local ONNX)",
    FASTER_WHISPER_SETUP_REF: "Whisper base (local)",
    RAPIDOCR_SETUP_REF: "RapidOCR",
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


@dataclass(frozen=True)
class LocalEngineSetupRequirement:
    """Passive local setup fact consumed by search-preparation planners.

    ``artifact_refs`` are immutable manifest identities suitable for a plan
    hash. ``download_bytes_if_missing`` is the current missing-byte disclosure
    and becomes zero when every weight artifact is cached; its precision is
    described by ``download_size_basis``.
    """

    setup_ref: str
    capability: str
    display_name: str
    runtime_ready: bool
    weights_ready: bool
    download_bytes_if_missing: int
    license: str
    source_url: str
    artifact_refs: tuple[str, ...]
    download_size_basis: str
    runtime_installable: bool = False
    runtime_remediation: str | None = None


def local_search_setup_requirements() -> tuple[LocalEngineSetupRequirement, ...]:
    """Return cache/runtime facts for local searchable-content engines.

    The probe performs no installation, cache mutation, subprocess launch, or
    network access. Callers choose which capabilities their imported media need.
    """

    from frisket.ai.models import artifact_manifest
    from frisket.engine._workers import embeddinggemma_artifacts, parakeet_artifacts
    from frisket.execution.definitions import parakeet_runtime_present
    from frisket.ops.ocr_engines_local import (
        rapidocr_available,
        rapidocr_setup_ready,
    )
    from frisket.runtime.embeddinggemma_install import is_installed

    rapid = artifact_manifest.rapidocr_default_model_artifact()
    parakeet = tuple(
        artifact
        for artifact in (
            artifact_manifest.parakeet_model_artifact(),
            artifact_manifest.parakeet_vad_artifact(),
        )
        if artifact is not None
    )
    embedding = embeddinggemma_artifacts.embeddinggemma_artifact()
    if rapid is None or rapid.total_size is None or len(parakeet) != 2:
        raise RuntimeError("local search setup manifest is incomplete")
    embedding_bytes = embedding.approx_size_bytes
    if embedding_bytes is None:
        raise RuntimeError("local search setup sizes are missing")
    rapid_runtime, _rapid_error = rapidocr_available()
    parakeet_runtime = parakeet_runtime_present()
    embedding_runtime = is_installed()
    try:
        embedding_weights_ready = (
            embeddinggemma_artifacts.cached_embeddinggemma_path() is not None
        )
    except (OSError, RuntimeError):
        embedding_weights_ready = False
    rapid_weights_ready = rapidocr_setup_ready()
    try:
        parakeet_bytes = parakeet_artifacts.parakeet_missing_download_bytes()
    except parakeet_artifacts.ParakeetArtifactUnavailable as exc:
        raise RuntimeError("local search setup cache facts are unavailable") from exc
    return (
        LocalEngineSetupRequirement(
            setup_ref=RAPIDOCR_SETUP_REF,
            capability="ocr",
            display_name=_SETUP_NAMES[RAPIDOCR_SETUP_REF],
            runtime_ready=rapid_runtime,
            weights_ready=rapid_weights_ready,
            download_bytes_if_missing=0 if rapid_weights_ready else rapid.total_size,
            license=rapid.license,
            source_url=rapid.source_url,
            artifact_refs=(rapid.ref,),
            download_size_basis="exact_checksum",
            runtime_remediation=(
                None
                if rapid_runtime
                else "Repair the Frisket base install and restart."
            ),
        ),
        LocalEngineSetupRequirement(
            setup_ref=PARAKEET_TDT_SETUP_REF,
            capability="transcription",
            display_name=_SETUP_NAMES[PARAKEET_TDT_SETUP_REF],
            runtime_ready=parakeet_runtime,
            weights_ready=parakeet_bytes == 0,
            download_bytes_if_missing=parakeet_bytes,
            license=", ".join(sorted({artifact.license for artifact in parakeet})),
            source_url=parakeet[0].source_url,
            artifact_refs=tuple(artifact.ref for artifact in parakeet),
            download_size_basis="manifest_estimate",
            runtime_installable=True,
            runtime_remediation=(
                None
                if parakeet_runtime
                else "The explicit setup also installs local transcription components."
            ),
        ),
        LocalEngineSetupRequirement(
            setup_ref=EMBEDDINGGEMMA_SETUP_REF,
            capability="semantic_search",
            display_name=_SETUP_NAMES[EMBEDDINGGEMMA_SETUP_REF],
            runtime_ready=embedding_runtime,
            weights_ready=embedding_weights_ready,
            download_bytes_if_missing=0 if embedding_weights_ready else embedding_bytes,
            license=embedding.license,
            source_url=embedding.source_url,
            artifact_refs=(embedding.ref,),
            download_size_basis="manifest_estimate",
            runtime_installable=True,
            runtime_remediation=(
                None
                if embedding_runtime
                else "The explicit setup also installs additional runtime components."
            ),
        ),
    )


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
    if row.model_ref == CHROMIUM_SETUP_REF:
        return _run_chromium_setup(
            engine=engine,
            pull_id=pull_id,
            should_cancel=should_cancel,
            is_final_attempt=is_final_attempt,
        )
    if row.model_ref == FASTER_WHISPER_SETUP_REF:
        return _run_whisper_setup(
            engine=engine,
            pull_id=pull_id,
            should_cancel=should_cancel,
            is_final_attempt=is_final_attempt,
        )
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
    if row.model_ref == RAPIDOCR_SETUP_REF:
        return _run_rapidocr_setup(
            engine=engine,
            pull_id=pull_id,
            should_cancel=should_cancel,
            is_final_attempt=is_final_attempt,
        )
    if row.model_ref != PARAKEET_TDT_SETUP_REF:
        raise LookupError(f"unsupported engine setup ref {row.model_ref!r}")

    from frisket.engine._workers.parakeet_artifacts import (
        ParakeetArtifactCancelled,
        ParakeetArtifactUnavailable,
        resolve_parakeet_artifacts,
    )
    from frisket.engine.jobs.model_pull import _fail

    from frisket.runtime.model_install import ModelInstallCancelled

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
        _install_speech_runtime(
            "parakeet-tdt",
            engine=engine,
            pull_id=pull_id,
            should_cancel=should_cancel,
        )
        model_pull_store.update_progress(
            engine,
            pull_id,
            phase="Downloading Parakeet model",
            total_bytes=None,
            completed_bytes=0,
        )
        asyncio.run(
            resolve_parakeet_artifacts(
                vad=True,
                should_cancel=should_cancel,
                allow_download=True,
            )
        )
    except (ParakeetArtifactCancelled, ModelInstallCancelled):
        model_pull_store.mark_cancelled(engine, pull_id)
        return {"status": "cancelled"}
    except (
        ParakeetArtifactUnavailable,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        raise _fail(
            engine,
            pull_id,
            error_code="engine_setup_unavailable",
            message="Could not set up Parakeet. Check your connection and available disk space, then retry.",
            terminal=False,
            is_final_attempt=is_final_attempt,
        ) from exc
    if should_cancel():
        model_pull_store.mark_cancelled(engine, pull_id)
        return {"status": "cancelled"}
    model_pull_store.mark_done(engine, pull_id)
    return {"status": "done"}


def _install_speech_runtime(
    engine_id: str, *, engine, pull_id: int, should_cancel: Callable[[], bool]
) -> None:
    from frisket.runtime.speech_install import install_speech, runtime_present

    if not runtime_present(engine_id):
        model_pull_store.update_progress(
            engine,
            pull_id,
            phase="Installing transcription components",
            total_bytes=None,
            completed_bytes=0,
        )
        install_speech(should_cancel=should_cancel, progress=lambda _: None)


def _run_whisper_setup(
    *, engine, pull_id: int, should_cancel: Callable[[], bool], is_final_attempt: bool
) -> dict[str, object]:
    from frisket.ai.models.artifact_manifest import whisper_base_artifact
    from frisket.engine._workers.parakeet_artifacts import cached_whisper_path

    def install(*, should_cancel, progress):
        _install_speech_runtime(
            "faster_whisper",
            engine=engine,
            pull_id=pull_id,
            should_cancel=should_cancel,
        )

    return _run_pinned_setup(
        engine=engine,
        pull_id=pull_id,
        should_cancel=should_cancel,
        is_final_attempt=is_final_attempt,
        label="Whisper base",
        install=install,
        artifact=whisper_base_artifact,
        cached_path=cached_whisper_path,
    )


def _run_rapidocr_setup(
    *, engine, pull_id: int, should_cancel: Callable[[], bool], is_final_attempt: bool
) -> dict[str, object]:
    from frisket.ai.models.artifact_manifest import rapidocr_default_model_artifact
    from frisket.engine.jobs.model_pull import _fail
    from frisket.ops.ocr_engines_local import (
        OcrCancelled,
        provision_rapidocr_default_models,
    )

    artifact = rapidocr_default_model_artifact()
    if artifact is None or artifact.total_size is None:
        raise _fail(
            engine,
            pull_id,
            error_code="engine_setup_unavailable",
            message="The pinned RapidOCR setup manifest is unavailable.",
            terminal=True,
            is_final_attempt=is_final_attempt,
        )
    model_pull_store.set_artifact_metadata(
        engine,
        pull_id,
        artifact_kind="engine_setup",
        artifact_source_url=artifact.source_url,
        artifact_license=artifact.license,
        artifact_manifest_version=artifact.manifest_version,
    )

    def progress(completed: int, total: int) -> None:
        model_pull_store.update_progress(
            engine,
            pull_id,
            phase="provisioning",
            total_bytes=total,
            completed_bytes=completed,
        )

    try:
        provision_rapidocr_default_models(
            should_cancel=should_cancel,
            progress=progress,
        )
    except OcrCancelled:
        model_pull_store.mark_cancelled(engine, pull_id)
        return {"status": "cancelled"}
    except (OSError, RuntimeError) as exc:
        raise _fail(
            engine,
            pull_id,
            error_code="engine_setup_unavailable",
            message="Could not provision the pinned RapidOCR model. Check the connection and retry.",
            terminal=False,
            is_final_attempt=is_final_attempt,
        ) from exc
    model_pull_store.mark_done(
        engine, pull_id, resolved_digest=artifact.composite_digest
    )
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


def _run_chromium_setup(
    *, engine, pull_id: int, should_cancel: Callable[[], bool], is_final_attempt: bool
) -> dict[str, object]:
    from frisket.engine.jobs.model_pull import _fail
    from frisket.runtime.chromium import install_chromium
    from frisket.runtime.model_install import ModelInstallCancelled

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
        install_chromium(should_cancel=should_cancel, progress=lambda _: None)
    except ModelInstallCancelled:
        model_pull_store.mark_cancelled(engine, pull_id)
        return {"status": "cancelled"}
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise _fail(
            engine,
            pull_id,
            error_code="browser_setup_failed",
            message="Browser tools could not be installed. Check your connection and available disk space, then retry setup.",
            terminal=True,
            is_final_attempt=is_final_attempt,
        ) from exc
    model_pull_store.mark_done(engine, pull_id)
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
    "RAPIDOCR_SETUP_REF",
    "LocalEngineSetupRequirement",
    "is_engine_setup_ref",
    "local_search_setup_requirements",
    "run_engine_setup",
]
