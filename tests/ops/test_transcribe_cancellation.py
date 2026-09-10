"""Local-transcribe cancellation seam.

Tiny substituted workers run through the shipped recipe -> MapRunner path. The
fakes OBSERVE the ``should_cancel`` callable the run threads in: they complete
normally while it reports False and only return a cancelled result after
watching it transition False->True (driven by an EXTERNAL cancel, never
fabricated). Proven for BOTH local engines, plus the pre-materialization guard
and the preview-path fence.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager

import pytest

import frisket.engine.executor.transcription_read as transcription_read
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, Row, SheetRows
from frisket.engine.executor.map_rows_action import (
    build_typed_map_rows_plan,
    run_typed_map_rows_action,
)
from frisket.ai.llm import ModelRouter
from frisket.contracts.transcription_sidecar import TRANSCRIPTION_CONTRACT_VERSION
from frisket.ops.base import OpContext
from frisket.sdk.ops.transcription import parakeet as transcribe_mod
from frisket.engine._workers.parakeet_session import (
    ParakeetRowError,
    ParakeetSessionCancelled,
)
from tests.execution_composition_helpers import open_attempt_authority
from frisket.engine.runner import MapRunner
from frisket.engine.sandbox.shim import SandboxResult
from frisket.engine.store import Project
from runner_test_helpers import run_with_output_claim

_OK_RESULT = {"text": "hello", "segments": []}
_FASTER_OK_STDOUT = json.dumps(
    {
        "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
        "results": [
            {
                **_OK_RESULT,
                "engine": "faster-whisper",
                "language": None,
                "duration": 0.0,
                "model_ids": ["faster-whisper/base"],
                "revision": "runtime-resolved",
                "device": "cpu",
                "dtype": "int8",
                "timings": {"inference_seconds": 0.0},
                "warnings": [],
                "accepted_options": {},
            }
        ],
    }
)
_LOCAL_ENGINES = ["faster_whisper", "parakeet-tdt"]


def _install_parakeet_session(monkeypatch, transcribe) -> None:
    # Local-onnx liveness is artifact-checked at resolution; these tests
    # fake the Parakeet session, so fake the artifact presence probe too.
    monkeypatch.setattr(
        "frisket.execution.definitions.parakeet_artifacts_present",
        lambda: True,
    )
    monkeypatch.setattr(
        "frisket.execution.definitions.parakeet_runtime_present", lambda: True
    )
    """Route legacy worker observations through Parakeet's session boundary."""

    class FakeParakeetSession:
        def __init__(self, *, expected_rows, vad, should_cancel) -> None:
            del expected_rows, vad
            self.should_cancel = should_cancel
            self.teardown_failed = False

        def mark_closed(self) -> None:
            pass

        async def close(self) -> None:
            pass

        async def transcribe(self, path: str) -> dict:
            return await transcribe(path, self.should_cancel)

    monkeypatch.setattr(transcribe_mod, "ParakeetProcessSession", FakeParakeetSession)


def _transcribe_project(tmp_path):
    p = Project.create(tmp_path / "t.frisket", name="t")
    sheet = p.add_sheet("data")
    cols = {"audio": p.add_column(sheet, "audio", type="audio")}
    # A raw local path is trusted on a dev deploy (FRISKET_ALLOW_CODE_RECIPES
    # defaults on); the fake worker never opens it.
    p.add_rows(sheet, [{"audio": str(tmp_path / "clip.wav")}], cols)
    return p, sheet


def _plan(project, sheet, engine="faster_whisper", *, row_ids=None):
    request = ActionRequest(
        action_id="media.transcribe",
        scope=SheetRows(sheet_id=sheet, row_ids=row_ids),
        params={"source": "audio", "engine": engine},
        output_names={"text": "transcript", "segments": "transcript_segments"},
        idempotency_key="transcription-cancellation",
    )
    return build_typed_map_rows_plan(
        project,
        BoundTypedActionRequest.bind(ACTION_REGISTRY.get(request.action_id), request),
    )


def _run_row(p, run_id):
    return p.db.execute(
        "SELECT status, completed_rows FROM runs WHERE id=?", (run_id,)
    ).fetchone()


@pytest.mark.parametrize("engine", _LOCAL_ENGINES)
def test_worker_completes_when_cancel_reports_false(tmp_path, monkeypatch, engine):
    """Control: a fake that OBSERVES should_cancel and only ever sees False must
    NOT produce a cancellation — the row completes and persists normally. This
    is the guard against a fake that fabricates a cancelled outcome."""
    p, sheet = _transcribe_project(tmp_path)
    observed: dict = {"callable": None, "reports": []}

    async def fake(argv, *, policy, stdin_data, should_cancel=None, extra_env=None):
        observed["callable"] = callable(should_cancel)
        observed["reports"].append(should_cancel() if should_cancel else None)
        if should_cancel is not None and should_cancel():
            return SandboxResult(
                returncode=125, stdout="", stderr="cancelled", cancelled=True
            )
        return SandboxResult(returncode=0, stdout=_FASTER_OK_STDOUT, stderr="")

    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.faster_whisper.run_sandboxed", fake
    )
    _install_parakeet_session(
        monkeypatch,
        lambda _path, should_cancel: _parakeet_false_result(observed, should_cancel),
    )
    runner = MapRunner(
        p,
        ModelRouter(cache=None, cache_mode="off"),
        concurrency=1,
        # Transcribe resolves an execution route, and a routed run
        # never dispatches unverified: a test runner builds the
        # SAME attempt authority the real dispatch paths do
        # (engine/executor/actions.py, engine/jobs/runs.py), or the
        # mint refuses before any row runs.
        authority=open_attempt_authority(p),
        allow_action_lifecycle_only_recipes=True,
    )
    # No should_cancel wired: the extras callable reports False throughout.
    plan = _plan(p, sheet, engine)
    result = run_typed_map_rows_action(
        p,
        "test",
        BoundTypedActionRequest.bind(plan.action, plan.request),
        runner.router,
        lambda project, router: runner,
    )

    assert observed["callable"] is True
    assert observed["reports"] == [False]  # the worker saw a live, False signal
    assert result.status == "completed", result.errors
    run = p.db.execute("SELECT status,completed_rows FROM runs").fetchone()
    assert run["status"] == "completed" and run["completed_rows"] == 1


async def _parakeet_false_result(observed: dict, should_cancel) -> dict:
    observed["callable"] = callable(should_cancel)
    observed["reports"].append(should_cancel() if should_cancel else None)
    if should_cancel is not None and should_cancel():
        raise ParakeetSessionCancelled("cancelled")
    return dict(_OK_RESULT)


@pytest.mark.parametrize("engine", _LOCAL_ENGINES)
def test_external_cancel_during_worker_drops_row(tmp_path, monkeypatch, engine):
    """A cancel arrives from OUTSIDE while the worker runs; the fake observes the
    should_cancel False->True transition and returns cancelled. The MapRunner
    fence then drops the row wholesale: zero completed/failed, no persisted
    value/error/cost, status 'cancelled', row still resumable."""
    p, sheet = _transcribe_project(tmp_path)
    state = {"running": False, "cancel": False, "callable": False, "transition": False}

    async def fake(argv, *, policy, stdin_data, should_cancel=None, extra_env=None):
        state["callable"] = callable(should_cancel)
        assert should_cancel is not None and should_cancel() is False
        state["running"] = True
        for _ in range(600):  # poll like the real sandbox supervisor
            if should_cancel():
                state["transition"] = True
                return SandboxResult(
                    returncode=125, stdout="", stderr="cancelled", cancelled=True
                )
            await asyncio.sleep(0.005)
        return SandboxResult(returncode=0, stdout=_FASTER_OK_STDOUT, stderr="")

    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.faster_whisper.run_sandboxed", fake
    )
    _install_parakeet_session(
        monkeypatch,
        lambda _path, should_cancel: _parakeet_cancel_result(state, should_cancel),
    )

    async def go():
        runner = MapRunner(
            p,
            ModelRouter(cache=None, cache_mode="off"),
            concurrency=1,
            # Transcribe resolves an execution route, and a routed run
            # never dispatches unverified: a test runner builds the
            # SAME attempt authority the real dispatch paths do
            # (engine/executor/actions.py, engine/jobs/runs.py), or the
            # mint refuses before any row runs.
            authority=open_attempt_authority(p),
            allow_action_lifecycle_only_recipes=True,
        )
        runner.should_cancel = lambda _run_id: state["cancel"]
        plan = _plan(p, sheet, engine)
        task = asyncio.ensure_future(
            run_with_output_claim(runner, plan.spec_dict(), program=plan.program)
        )
        while not state["running"] and not task.done():
            await asyncio.sleep(0.005)
        state["cancel"] = True  # the user cancels the run mid-worker
        return await task

    prog = asyncio.run(go())

    assert state["callable"] and state["transition"]
    assert prog.cancelled and prog.completed == 0 and prog.failed == 0
    assert prog.cost == 0.0
    row = _run_row(p, prog.run_id)
    assert row["status"] == "cancelled" and row["completed_rows"] == 0
    transcript = next(c for c in p.columns(sheet) if c["name"] == "transcript")
    assert all(v is None for v in p.get_values(sheet, transcript["id"]).values())


async def _parakeet_cancel_result(state: dict, should_cancel) -> dict:
    state["callable"] = callable(should_cancel)
    assert should_cancel is not None and should_cancel() is False
    state["running"] = True
    for _ in range(600):
        if should_cancel():
            state["transition"] = True
            raise ParakeetSessionCancelled("cancelled")
        await asyncio.sleep(0.005)
    return dict(_OK_RESULT)


@pytest.mark.parametrize("engine", _LOCAL_ENGINES)
def test_worker_timeout_stays_a_row_failure_not_cancellation(
    tmp_path, monkeypatch, engine
):
    p, sheet = _transcribe_project(tmp_path)

    async def fake(argv, *, policy, stdin_data, should_cancel=None, extra_env=None):
        # A wall timeout is NOT a cancel: the run's cancel state stays clear.
        return SandboxResult(
            returncode=124, stdout="", stderr="wall timeout", timed_out=True
        )

    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.faster_whisper.run_sandboxed", fake
    )
    _install_parakeet_session(
        monkeypatch,
        lambda _path, _should_cancel: _parakeet_timeout_result(),
    )
    runner = MapRunner(
        p,
        ModelRouter(cache=None, cache_mode="off"),
        concurrency=1,
        # Transcribe resolves an execution route, and a routed run
        # never dispatches unverified: a test runner builds the
        # SAME attempt authority the real dispatch paths do
        # (engine/executor/actions.py, engine/jobs/runs.py), or the
        # mint refuses before any row runs.
        authority=open_attempt_authority(p),
        allow_action_lifecycle_only_recipes=True,
    )
    plan = _plan(p, sheet, engine)
    prog = asyncio.run(
        run_with_output_claim(runner, plan.spec_dict(), program=plan.program)
    )

    # A timeout runs the fence's fall-through path: the failed row is accepted
    # (failed + completed advance), the run is NOT cancelled, and the failure
    # was persisted (completed_rows counts it).
    assert not prog.cancelled
    assert prog.failed == 1 and prog.completed == 1
    row = _run_row(p, prog.run_id)
    assert row["status"] == "completed"  # fully attempted, all rows failed
    assert row["completed_rows"] == 1


async def _parakeet_timeout_result() -> dict:
    raise ParakeetRowError("wall timeout")


def test_execute_raises_before_materialization_when_already_cancelled(
    tmp_path, monkeypatch
):
    """Invariant 1 at the transcribe seam: an already-latched cancel raises
    TranscribeCancelled before media is materialized AND before any sandbox
    launch — neither the (expensive) materializer nor the worker runs."""
    materialized = {"count": 0}
    launched = {"count": 0}

    @contextmanager
    def tracking_materialize(media, ctx, *, op="this op"):
        materialized["count"] += 1
        yield media

    async def fake(*args, **kwargs):
        launched["count"] += 1
        return SandboxResult(returncode=0, stdout=_FASTER_OK_STDOUT, stderr="")

    monkeypatch.setattr(
        transcription_read, "materialize_media_path", tracking_materialize
    )
    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.faster_whisper.run_sandboxed", fake
    )

    ctx = OpContext(extras={"cancelled": lambda: True})

    async def go():
        reader = transcription_read.AdmittedTranscriber(
            ctx, engine="faster_whisper", options={"vad": True}
        )
        try:
            await reader.start(expected_rows=1)
            reader.bind_row(
                Row({"audio": "unused.wav"}), sheet_id=1, row_id=1, sources={}, ctx=ctx
            )
        finally:
            await reader.aclose()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(go())
    assert materialized["count"] == 0, "media must not be materialized when cancelled"
    assert launched["count"] == 0, "no sandbox worker may launch when cancelled"


def test_preview_external_cancel_persists_no_row_values(tmp_path, monkeypatch):
    """Preview-path fence mirror of the durable case: an external cancel during
    the preview row (a threading.Event) drops the row — the in-memory result
    carries no value for it and progress never counts it."""
    p, sheet = _transcribe_project(tmp_path)
    state = {"running": False, "transition": False}
    cancel_event = threading.Event()

    async def fake(argv, *, policy, stdin_data, should_cancel=None, extra_env=None):
        assert should_cancel is not None and should_cancel() is False
        state["running"] = True
        for _ in range(600):
            if should_cancel():
                state["transition"] = True
                return SandboxResult(
                    returncode=125, stdout="", stderr="cancelled", cancelled=True
                )
            await asyncio.sleep(0.005)
        return SandboxResult(returncode=0, stdout=_FASTER_OK_STDOUT, stderr="")

    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.faster_whisper.run_sandboxed", fake
    )
    progress_calls: list[tuple[int, int]] = []

    # preview() requires an explicit, non-empty row_ids sample.
    plan = _plan(p, sheet, row_ids=list(p.visible_row_ids(sheet)))

    async def go():
        runner = MapRunner(
            p,
            ModelRouter(cache=None, cache_mode="off"),
            concurrency=1,
            # Transcribe resolves an execution route, and a routed run
            # never dispatches unverified: a test runner builds the
            # SAME attempt authority the real dispatch paths do
            # (engine/executor/actions.py, engine/jobs/runs.py), or the
            # mint refuses before any row runs.
            authority=open_attempt_authority(p),
            allow_action_lifecycle_only_recipes=True,
        )
        task = asyncio.ensure_future(
            runner.preview(
                plan.spec_dict(),
                program=plan.program,
                cancel_event=cancel_event,
                progress_cb=lambda done, total: progress_calls.append((done, total)),
            )
        )
        while not state["running"] and not task.done():
            await asyncio.sleep(0.005)
        cancel_event.set()  # external cancel during the preview row
        return await task

    result = asyncio.run(go())

    assert state["transition"]
    assert result.values == {}, "a cancelled preview row must persist no values"
    assert progress_calls == [], "a dropped row must not advance progress"
