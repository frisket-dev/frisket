"""Parakeet process sessions, their local lease, and adapter."""

from __future__ import annotations

import contextvars
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from frisket.ai.models.metadata import ModelCallMeta
from frisket.contracts.action import project_transcription_engine_options
from frisket.engine._workers.local_engine_lease import (
    LocalEngineLease,
    LocalEngineLeaseBusy,
    acquire_local_engine_lease,
)
from frisket.engine._workers.parakeet_artifacts import (
    PARAKEET_MODEL,
    ParakeetArtifactCancelled,
    ParakeetArtifactUnavailable,
)
from frisket.engine._workers.parakeet_session import (
    ParakeetProcessSession,
    ParakeetRowError,
    ParakeetSessionCancelled,
    ParakeetSessionFailure,
)
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.ops.base import RecipeInvocationHalt

from .common import TranscribeCancelled, input_units

_ACTIVE_RUN_SCOPED_SESSION: contextvars.ContextVar[ParakeetProcessSession | None] = (
    contextvars.ContextVar("frisket_transcribe_run_scoped_session", default=None)
)
_LOCAL_ENGINE_BUSY_DETAIL = (
    "another local Parakeet session is still running; retry after it closes"
)


async def _close_session(
    session: ParakeetProcessSession, lease: LocalEngineLease
) -> None:
    session.mark_closed()
    close_error: BaseException | None = None
    try:
        await session.close()
    except BaseException as error:
        close_error = error
    if session.teardown_failed or isinstance(close_error, SandboxTeardownError):
        lease.poison()
    else:
        lease.release()
    if close_error is not None:
        raise close_error


class ParakeetAdapter:
    async def transcribe(
        self, path: str, spec: dict, *, should_cancel: Callable[[], bool] | None = None
    ) -> dict:
        projected = project_transcription_engine_options("parakeet-tdt", spec)
        path = os.path.abspath(path)
        active_session = _ACTIVE_RUN_SCOPED_SESSION.get()
        if active_session is not None:
            return await self._transcribe_session(active_session, path)
        try:
            lease = acquire_local_engine_lease("parakeet-tdt")
        except LocalEngineLeaseBusy as error:
            raise RecipeInvocationHalt(
                "local_engine_busy", _LOCAL_ENGINE_BUSY_DETAIL
            ) from error
        try:
            session = ParakeetProcessSession(
                expected_rows=1,
                vad=bool(projected.get("vad", True)),
                should_cancel=should_cancel,
            )
        except BaseException:
            lease.release()
            raise
        try:
            return await self._transcribe_session(session, path)
        finally:
            await _close_session(session, lease)

    @staticmethod
    async def _transcribe_session(
        session: ParakeetProcessSession, path: str
    ) -> dict[str, Any]:
        try:
            return await session.transcribe(path)
        except (ParakeetArtifactCancelled, ParakeetSessionCancelled) as error:
            raise TranscribeCancelled("transcription cancelled") from error
        except ParakeetArtifactUnavailable as error:
            raise RecipeInvocationHalt(
                "local_artifact_unavailable",
                "the pinned local Parakeet artifacts are unavailable; retry after checking the persistent model cache and network",
            ) from error
        except ParakeetSessionFailure as error:
            raise RecipeInvocationHalt(
                "local_session_failed",
                "the local Parakeet process stopped; completed rows were preserved and the run can be resumed",
            ) from error
        except ParakeetRowError as error:
            raise RuntimeError(str(error)) from error

    def model_calls(
        self, engine: str, path: str, spec: dict[str, Any], out: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return [
            ModelCallMeta.local(
                capability="transcribe",
                engine=engine,
                provider="local-onnx",
                model_ids=[PARAKEET_MODEL],
                units=input_units(path, out),
            ).as_dict()
        ]


@asynccontextmanager
async def execution_scope(
    *,
    enabled: bool,
    spec: dict[str, Any],
    expected_rows: int,
    should_cancel: Callable[[], bool] | None,
) -> AsyncIterator[None]:
    """Own a run-scoped Parakeet session and its lease when route-selected."""
    if not enabled:
        yield
        return
    if should_cancel is not None and should_cancel():
        yield
        return
    projected = project_transcription_engine_options("parakeet-tdt", spec)
    try:
        lease = acquire_local_engine_lease("parakeet-tdt")
    except LocalEngineLeaseBusy as error:
        raise RecipeInvocationHalt(
            "local_engine_busy", _LOCAL_ENGINE_BUSY_DETAIL
        ) from error
    try:
        session = ParakeetProcessSession(
            expected_rows=expected_rows,
            vad=bool(projected.get("vad", True)),
            should_cancel=should_cancel,
        )
    except BaseException:
        lease.release()
        raise
    token = _ACTIVE_RUN_SCOPED_SESSION.set(session)
    try:
        yield
    finally:
        _ACTIVE_RUN_SCOPED_SESSION.reset(token)
        await _close_session(session, lease)
