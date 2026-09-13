"""Allowlisted durable engine setup operations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from frisket.engine.jobs import model_pull_store

PARAKEET_TDT_SETUP_REF = "engine-setup:parakeet-tdt.local-onnx@1"


def is_engine_setup_ref(ref: str) -> bool:
    return ref == PARAKEET_TDT_SETUP_REF


def run_engine_setup(
    *,
    engine,
    pull_id: int,
    should_cancel: Callable[[], bool],
    is_final_attempt: bool,
) -> dict[str, object]:
    """Provision the sole supported engine bundle through its existing worker.

    ``resolve_parakeet_artifacts`` verifies each pinned cache component before
    it starts its supervised downloader, so a retry downloads only missing or
    invalid components.
    """
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


__all__ = ["PARAKEET_TDT_SETUP_REF", "is_engine_setup_ref", "run_engine_setup"]
