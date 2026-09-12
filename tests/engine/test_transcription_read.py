import asyncio
from contextlib import asynccontextmanager, closing

import pytest

from frisket.actions.media_options import TranscriptionOptions
from frisket.actions.types import ColumnRef, Row, RowError
from frisket.engine.executor.transcription_read import AdmittedTranscriber
from frisket.engine.store import Project
from frisket.ops.base import OpContext, RecipeInvocationHalt
from frisket.sdk.ops import transcribe_engines


@pytest.fixture
def audio(tmp_path):
    with closing(Project.create(tmp_path / "audio.frisket", name="audio")) as project:
        sheet = project.add_sheet("recordings")
        column = project.add_column(sheet, "recording", type="audio")
        digest = project.add_blob(
            b"audio fixture", filename="interview.wav", mime="audio/wav"
        )
        value = {"blob": digest, "mime": "audio/wav"}
        row_id = project.add_rows(sheet, [{"recording": value}], {"recording": column})[
            0
        ]
        ctx = OpContext(project=project, extras={"row_id": row_id})
        yield ctx, sheet, row_id, column, value


def bind(reader, audio, *, row=None, sources=None):
    ctx, sheet, row_id, column, value = audio
    row = row or Row({"recording": value})
    return row, reader.bind_row(
        row,
        sheet_id=sheet,
        row_id=row_id,
        ctx=ctx,
        sources=sources
        if sources is not None
        else {
            "recording": {"column_id": column, "column_type": "audio", "value": value}
        },
    )


def stub_engine(monkeypatch):
    calls = []
    output = {
        "text": "Hello world",
        "segments": [{"text": "Hello world", "start": 0, "end": 1}],
        "language": "en",
        "cost": 0,
    }

    async def run(engine, path, spec, ctx, **kwargs):
        from pathlib import Path

        assert Path(path).read_bytes() == b"audio fixture"
        calls.append((engine, spec, kwargs))
        return transcribe_engines.TranscriptionEngineResult(
            output=output, model_calls=({"cost_source": "fixture"},)
        )

    monkeypatch.setattr(transcribe_engines, "run_transcription_engine", run)
    return calls, output


def test_actual_read_is_independent_of_mutable_returned_fields(audio, monkeypatch):
    calls, provider_output = stub_engine(monkeypatch)
    reader = AdmittedTranscriber(audio[0], engine="faster_whisper", options={})

    async def run():
        await reader.start(expected_rows=1)
        try:
            row, bound = bind(reader, audio)
            result = await bound.transcribe(
                row, ColumnRef("recording"), options=TranscriptionOptions()
            )
            assert result.text.root == "Hello world"
            assert result.detected_language.root == "en"
            result.segments[0]["text"] = "author rewrite"
            provider_output["segments"][0]["text"] = "provider dictionary reused"
            fact = reader.calls_by_row[audio[2]][0]
            assert fact["segments"][0]["text"] == "Hello world"
            assert fact["source"]["value"] == audio[4]
            with pytest.raises(RuntimeError, match="one transcription"):
                await bound.transcribe(
                    row, ColumnRef("recording"), options=TranscriptionOptions()
                )
        finally:
            await reader.aclose()

    asyncio.run(run())
    assert len(calls) == 1
    assert reader.accounting_by_row[audio[2]]["model_calls"] == [
        {"cost_source": "fixture"}
    ]


def test_review_close_settles_an_active_transcription(audio, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def run(*args, **kwargs):
            entered.set()
            await release.wait()
            return transcribe_engines.TranscriptionEngineResult(
                output={"text": "text", "segments": [], "language": "en", "cost": 0},
                model_calls=(),
            )

        monkeypatch.setattr(transcribe_engines, "run_transcription_engine", run)
        reader = AdmittedTranscriber(audio[0], engine="faster_whisper", options={})
        await reader.start(expected_rows=1)
        row, bound = bind(reader, audio)
        task = asyncio.create_task(
            bound.transcribe(
                row, ColumnRef("recording"), options=TranscriptionOptions()
            )
        )
        await entered.wait()
        try:
            await reader.aclose()
            assert task.done(), (
                "Transcription continues after invocation resources close"
            )
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["options", "source", "type", "row", "cancelled"])
def test_refuses_changed_authority_before_calling_engine(audio, monkeypatch, change):
    calls, _ = stub_engine(monkeypatch)
    reader = AdmittedTranscriber(audio[0], engine="faster_whisper", options={})

    async def run():
        await reader.start(expected_rows=1)
        try:
            row, bound = bind(reader, audio)
            options = TranscriptionOptions()
            if change == "options":
                options = TranscriptionOptions(language=["en"])
            elif change == "source":
                row.values["recording"]["blob"] = "substituted"
            elif change == "type":
                audio[0].project.db.execute(
                    "UPDATE columns SET type='text' WHERE id=?", (audio[3],)
                )
            elif change == "row":
                row = Row(dict(row.values))
            else:
                audio[0].extras["cancelled"] = lambda: True
            with pytest.raises(
                asyncio.CancelledError if change == "cancelled" else RowError
            ):
                await bound.transcribe(row, ColumnRef("recording"), options=options)
        finally:
            await reader.aclose()

    asyncio.run(run())
    assert calls == []


def test_gateway_requires_route_before_starting_resources(audio, monkeypatch):
    called = []

    @asynccontextmanager
    async def scope(*args, **kwargs):
        called.append(True)
        yield

    monkeypatch.setattr(transcribe_engines, "transcription_execution_scope", scope)
    reader = AdmittedTranscriber(audio[0], engine="whisper-turbo", options={})
    with pytest.raises(RecipeInvocationHalt, match="admitted route"):
        asyncio.run(reader.start(expected_rows=1))
    assert not called


def test_invocation_scope_is_shared_and_closed_once(audio, monkeypatch):
    events = []

    @asynccontextmanager
    async def scope(spec, ctx, *, expected_rows):
        events.append(("start", expected_rows, spec["engine"]))
        try:
            yield
        finally:
            events.append("close")

    monkeypatch.setattr(transcribe_engines, "transcription_execution_scope", scope)
    reader = AdmittedTranscriber(audio[0], engine="faster_whisper", options={})

    async def run():
        await reader.start(expected_rows=3)
        bind(reader, audio)
        bind(reader, audio)
        await reader.aclose()
        await reader.aclose()
        with pytest.raises(RuntimeError, match="active invocation"):
            bind(reader, audio)

    asyncio.run(run())
    assert events == [("start", 3, "faster_whisper"), "close"]


def test_typed_execution_publishes_and_replays_timestamped_transcript(
    audio, monkeypatch
):
    from frisket.actions.core import RegisteredAction
    from frisket.actions.media import TRANSCRIBE
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store.receipts import ReceiptStore

    registered = RegisteredAction("media.transcribe", TRANSCRIBE)
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {
            **ACTION_REGISTRY._actions,
            "media.transcribe": registered,
        },
    )
    calls = []

    async def run(*args, **kwargs):
        calls.append(True)
        return transcribe_engines.TranscriptionEngineResult(
            output={
                "text": "Hello world",
                "segments": [{"text": "Hello world", "start": 0, "end": 1}],
                "language": "en",
                "cost": 0,
            },
            model_calls=(),
        )

    monkeypatch.setattr(transcribe_engines, "run_transcription_engine", run)
    ctx, sheet, row_id, _column, _value = audio
    body = {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"source": "recording", "engine": "faster_whisper"},
        "output_names": {
            "text": "interview_text",
            "segments": "timings",
            "detected_language": "language",
        },
        "idempotency_key": "typed-transcribe-test",
    }
    result = run_action_spec(ctx.project, body, project_id="audio")
    if result.status == "needs_confirmation":
        body["confirmation"] = result.errors[0].details["promise_set_hash"]
        result = run_action_spec(ctx.project, body, project_id="audio")
    assert result.status == "completed", "\n".join(
        error.message for error in result.errors
    )
    text_column = next(
        output.column_id for output in result.outputs if output.name == "interview_text"
    )
    assert ctx.project.get_values(sheet, text_column, row_ids=[row_id]) == {
        row_id: "Hello world"
    }
    assert (
        ctx.project.db.execute(
            "SELECT type FROM columns WHERE id=?", (text_column,)
        ).fetchone()[0]
        == "timestamped_transcript"
    )
    receipt = ReceiptStore(ctx.project).parsed_by_id(result.receipt_id)
    assert any(
        item.ref.get("kind") == "media_transcribe_temporal_evidence_link"
        for item in receipt.evidence
    )
    replay = run_action_spec(ctx.project, body, project_id="audio")
    assert replay.status == "completed", replay.errors
    assert replay.receipt_id == result.receipt_id
    assert calls == [True]
