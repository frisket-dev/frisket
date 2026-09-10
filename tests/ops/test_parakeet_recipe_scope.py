from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

from frisket.engine._workers.parakeet_artifacts import (
    ParakeetArtifactCancelled,
    ParakeetArtifactUnavailable,
)
from frisket.engine._workers.local_engine_lease import LocalEngineLeaseBusy
from frisket.engine._workers.parakeet_session import (
    ParakeetRowError,
    ParakeetSessionCancelled,
    ParakeetSessionFailure,
)
from frisket.sdk.ops.transcription import parakeet as transcribe_mod
from frisket.ops.base import OpContext, RecipeInvocationHalt
from frisket.actions.media_options import TranscriptionOptions
from frisket.engine.executor.transcription_read import AdmittedTranscriber
from frisket.sdk.ops.transcribe_engines import TranscribeCancelled
from frisket.engine.sandbox.shim import SandboxTeardownError


class _FakeLease:
    def __init__(self) -> None:
        self.released = 0
        self.poisoned = 0

    def release(self) -> None:
        self.released += 1

    def poison(self) -> None:
        self.poisoned += 1


@asynccontextmanager
async def _scope(spec, ctx, *, expected_rows):
    engine = spec["engine"]
    options = TranscriptionOptions.model_validate(
        {k: v for k, v in spec.items() if k != "engine"}
    )
    reader = AdmittedTranscriber(ctx, engine=engine, options=options.normalize(engine))
    try:
        await reader.start(expected_rows=expected_rows)
        yield reader
    finally:
        await reader.aclose()


class _FakeSession:
    instances: list["_FakeSession"] = []
    close_error: BaseException | None = None

    def __init__(self, *, expected_rows: int, vad: bool, should_cancel) -> None:
        self.expected_rows = expected_rows
        self.vad = vad
        self.should_cancel = should_cancel
        self.teardown_failed = isinstance(self.close_error, SandboxTeardownError)
        self.marked_closed = False
        self.close_calls = 0
        self.paths: list[str] = []
        self.instances.append(self)

    def mark_closed(self) -> None:
        self.marked_closed = True

    async def close(self) -> None:
        self.close_calls += 1
        assert self.marked_closed is True
        assert transcribe_mod._ACTIVE_RUN_SCOPED_SESSION.get() is None
        if self.close_error is not None:
            raise self.close_error

    async def transcribe(self, path: str) -> dict[str, Any]:
        self.paths.append(path)
        return {"text": path, "segments": [], "language": "en"}


@pytest.fixture(autouse=True)
def _reset_session_state() -> None:
    _FakeSession.instances = []
    _FakeSession.close_error = None
    token = transcribe_mod._ACTIVE_RUN_SCOPED_SESSION.set(None)
    yield
    transcribe_mod._ACTIVE_RUN_SCOPED_SESSION.reset(token)


def test_non_parakeet_and_precancelled_scopes_allocate_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(engine: str):
        raise AssertionError("lease must not be acquired")

    monkeypatch.setattr(transcribe_mod, "acquire_local_engine_lease", forbidden)

    async def run() -> None:
        async with _scope({"engine": "faster_whisper"}, OpContext(), expected_rows=2):
            assert transcribe_mod._ACTIVE_RUN_SCOPED_SESSION.get() is None
        async with _scope(
            {"engine": "parakeet-tdt"},
            OpContext(extras={"cancelled": lambda: True}),
            expected_rows=2,
        ):
            assert transcribe_mod._ACTIVE_RUN_SCOPED_SESSION.get() is None

    asyncio.run(run())


def test_scope_installs_one_inherited_session_then_resets_before_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _FakeLease()
    monkeypatch.setattr(
        transcribe_mod,
        "acquire_local_engine_lease",
        lambda engine: lease,
    )
    monkeypatch.setattr(transcribe_mod, "ParakeetProcessSession", _FakeSession)

    def cancel() -> bool:
        return False

    async def run() -> None:
        async with _scope(
            {"engine": "parakeet-tdt", "vad": False},
            OpContext(extras={"cancelled": cancel}),
            expected_rows=3,
        ):
            current = transcribe_mod._ACTIVE_RUN_SCOPED_SESSION.get()
            assert current is _FakeSession.instances[0]
            first = await asyncio.create_task(
                transcribe_engines.ParakeetAdapter().transcribe("one.wav", {})
            )
            second = await transcribe_engines.ParakeetAdapter().transcribe(
                "two.wav", {}
            )
            assert first["text"].endswith("one.wav")
            assert second["text"].endswith("two.wav")
        assert transcribe_mod._ACTIVE_RUN_SCOPED_SESSION.get() is None

    asyncio.run(run())
    session = _FakeSession.instances[0]
    assert (session.expected_rows, session.vad, session.should_cancel) == (
        3,
        False,
        cancel,
    )
    assert len(session.paths) == 2
    assert session.close_calls == 1
    assert lease.released == 1
    assert lease.poisoned == 0


def test_scope_resets_and_closes_after_body_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _FakeLease()
    monkeypatch.setattr(
        transcribe_mod,
        "acquire_local_engine_lease",
        lambda engine: lease,
    )
    monkeypatch.setattr(transcribe_mod, "ParakeetProcessSession", _FakeSession)

    async def run() -> None:
        with pytest.raises(ValueError, match="body failed"):
            async with _scope({"engine": "parakeet-tdt"}, OpContext(), expected_rows=1):
                raise ValueError("body failed")

    asyncio.run(run())
    assert _FakeSession.instances[0].close_calls == 1
    assert lease.released == 1


def test_teardown_failure_poisons_admission_and_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _FakeLease()
    _FakeSession.close_error = SandboxTeardownError("tree remained live")
    monkeypatch.setattr(
        transcribe_mod,
        "acquire_local_engine_lease",
        lambda engine: lease,
    )
    monkeypatch.setattr(transcribe_mod, "ParakeetProcessSession", _FakeSession)

    async def run() -> None:
        with pytest.raises(SandboxTeardownError):
            async with _scope({"engine": "parakeet-tdt"}, OpContext(), expected_rows=1):
                pass

    asyncio.run(run())
    assert lease.poisoned == 1
    assert lease.released == 0


def test_direct_call_uses_one_request_session_and_releases_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _FakeLease()
    monkeypatch.setattr(
        transcribe_mod,
        "acquire_local_engine_lease",
        lambda engine: lease,
    )
    monkeypatch.setattr(transcribe_mod, "ParakeetProcessSession", _FakeSession)

    result = asyncio.run(
        transcribe_engines.ParakeetAdapter().transcribe("direct.wav", {})
    )

    assert result["text"].endswith("direct.wav")
    session = _FakeSession.instances[0]
    assert session.expected_rows == 1
    assert session.close_calls == 1
    assert lease.released == 1


def test_busy_lease_is_a_typed_resumable_halt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def busy(engine: str):
        raise LocalEngineLeaseBusy("busy")

    monkeypatch.setattr(transcribe_mod, "acquire_local_engine_lease", busy)
    with pytest.raises(RecipeInvocationHalt) as caught:
        asyncio.run(transcribe_engines.ParakeetAdapter().transcribe("direct.wav", {}))
    assert caught.value.code == "local_engine_busy"


@pytest.mark.parametrize(
    ("error", "expected", "code"),
    [
        (ParakeetArtifactCancelled("cancel"), TranscribeCancelled, None),
        (ParakeetSessionCancelled("cancel"), TranscribeCancelled, None),
        (
            ParakeetArtifactUnavailable("missing"),
            RecipeInvocationHalt,
            "local_artifact_unavailable",
        ),
        (
            ParakeetSessionFailure("failed"),
            RecipeInvocationHalt,
            "local_session_failed",
        ),
        (ParakeetRowError("bad audio"), RuntimeError, None),
    ],
)
def test_session_errors_map_at_the_recipe_boundary(
    error: BaseException, expected: type[BaseException], code: str | None
) -> None:
    class FailingSession:
        async def transcribe(self, path):
            raise error

    with pytest.raises(expected) as caught:
        asyncio.run(
            transcribe_engines.ParakeetAdapter()._transcribe_session(
                FailingSession(), "audio.wav"
            )
        )
    if code is not None:
        assert isinstance(caught.value, RecipeInvocationHalt)
        assert caught.value.code == code
