"""Allowlisted durable engine setup operations."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Callable

from frisket.engine.jobs import model_pull_store

PARAKEET_TDT_SETUP_REF = "engine-setup:parakeet-tdt.local-onnx@1"
DOCLING_SETUP_REF = "engine-setup:docling.local@1"


def is_engine_setup_ref(ref: str) -> bool:
    return ref in {PARAKEET_TDT_SETUP_REF, DOCLING_SETUP_REF}


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
    if row.model_ref == DOCLING_SETUP_REF:
        return _run_docling_setup(
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
    "PARAKEET_TDT_SETUP_REF",
    "is_engine_setup_ref",
    "run_engine_setup",
]
