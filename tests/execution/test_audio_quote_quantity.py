"""ASR estimates measure one admitted source per row, never guess a selection."""

from dataclasses import dataclass

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import (
    MediaBlobStore,
    media_cell,
    owned_media_metadata_document,
)
from frisket.execution.promise_compiler import (
    OperatorBorneZeroCost,
    PricedCostBasis,
    UnpriceableCost,
)
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.execution.resolve_for_action import (
    _transcribe_row_quantities_seconds,
    resolve_for_action,
)
from frisket.execution.resolver import ResolvedExecution
from frisket.ops.base import Recipe
from frisket.sdk.ops.transcribe_engines import total_audio_seconds


@dataclass
class _AudioProgram(Recipe):
    name: str = "example.audio_reader"
    consumes_resolution = True
    execution_capability = "transcribe"
    cost_class = "metered"


@pytest.fixture
def audio(tmp_path):
    project = Project.create(tmp_path / "audio.frisket")
    sheet = project.add_sheet("Sources")
    columns = {name: project.add_column(sheet, name, "json") for name in ("a", "b")}
    try:
        yield project, sheet, columns
    finally:
        project.close()


def _media(project, duration, *, acquired=None):
    digest = project.add_blob(
        repr((duration, acquired)).encode(),
        filename="audio.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": duration},
            acquisition={"duration_seconds": acquired} if acquired is not None else {},
        ),
    )
    return media_cell(digest, filename="audio.wav", mime="audio/wav")


def _spec(audio, rows, *, engine="openai/whisper-1"):
    project, sheet, columns = audio
    selected = project.add_rows(sheet, rows, columns)
    return {
        "action_kind": "example.audio_reader",
        "sheet_id": sheet,
        "input_columns": list(columns),
        "row_ids": selected,
        "engine": engine,
    }


def _resolve(project, spec):
    return resolve_for_action(
        project,
        spec,
        _AudioProgram(),
        composition=open_execution_composition(
            project, ModelRouter(), ExecutionCompositionContext.direct()
        ),
    )


def test_exact_selected_durations_preserve_row_allocation(audio, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-audio-quote-key")
    project, sheet, columns = audio
    spec = _spec(audio, [{"a": _media(project, 2.5)}, {"b": _media(project, 3)}, {}])
    project.add_rows(sheet, [{"a": _media(project, 99)}], columns)
    assert _transcribe_row_quantities_seconds(project, spec, _AudioProgram()) == dict(
        zip(spec["row_ids"], [2.5, 3, 0])
    )
    resolved = _resolve(project, spec)
    assert isinstance(resolved, ResolvedExecution)
    assert isinstance(resolved.cost_basis, PricedCostBasis)
    assert resolved.cost_basis.estimated_quantity == "5.5"


@pytest.mark.parametrize("other", ["audio", "path"])
@pytest.mark.parametrize("reverse", [False, True])
def test_multiple_sources_make_quote_unknown(audio, monkeypatch, other, reverse):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-audio-quote-key")
    project = audio[0]
    second = _media(project, 8) if other == "audio" else "/tmp/unmeasured.wav"
    spec = _spec(audio, [{"a": _media(project, 2.5), "b": second}])
    if reverse:
        spec["input_columns"].reverse()
    resolved = _resolve(project, spec)
    assert isinstance(resolved, ResolvedExecution)
    assert isinstance(resolved.cost_basis, UnpriceableCost)
    assert any(
        p.field == "cost" and p.op == "unbounded" for p in resolved.promise_set.promises
    )


@pytest.mark.parametrize(
    "value", ["/tmp/audio.wav", False, 0, {"blob": "0" * 64}, {"duration_seconds": 100}]
)
def test_unmeasured_values_are_unknown_not_zero(audio, value):
    assert total_audio_seconds([{"a": value}], audio[0]) is None


@pytest.mark.parametrize(
    "duration", [None, True, -1, "NaN", "Infinity", "not-a-duration"]
)
def test_invalid_metadata_cannot_form_a_quote(audio, duration):
    media = _media(audio[0], duration)
    assert total_audio_seconds([{"a": media}], audio[0]) is None


def test_blank_rows_zero_and_acquisition_duration_preserved(audio):
    project = audio[0]
    assert total_audio_seconds([{}, {"a": None, "b": " "}, {"a": []}], project) == 0
    assert (
        total_audio_seconds([{"a": _media(project, 3, acquired=4.5)}], project) == 4.5
    )
    assert total_audio_seconds([{"a": _media(project, 0)}], project) == 0


def test_local_free_route_does_not_read_duration_metadata(audio, monkeypatch):
    spec = _spec(
        audio, [{"a": "/tmp/audio.wav", "b": "other source"}], engine="faster_whisper"
    )

    def forbidden(*args, **kwargs):
        pytest.fail("A free local route must not measure audio for pricing")

    monkeypatch.setattr(MediaBlobStore, "display_metadata", forbidden)
    resolved = _resolve(audio[0], spec)
    assert isinstance(resolved, ResolvedExecution)
    assert isinstance(resolved.cost_basis, OperatorBorneZeroCost)
