from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

from frisket.engine.store.media_blobs import owned_media_metadata_document

import asyncio
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

import frisket.sdk.ops.transcribe_engines as transcribe_mod
from frisket.sdk.ops.transcription import parakeet as parakeet_mod

from frisket.contracts.transcription_sidecar import TRANSCRIPTION_CONTRACT_VERSION
from frisket.engine._workers import faster_whisper_worker
from frisket.ops.base import OpContext
from frisket.sdk.ops.transcribe_engines import (
    segments_with_index,
)
from frisket.sdk.ops.transcription.common import _segment_with_speaker
from frisket.engine.sandbox.shim import SandboxResult
from frisket.engine.store import Project
from helpers import stub_parakeet_run_scope
from frisket.engine.store.evidence import resolve_evidence_viewer


# ---------------------------------------------------------------------------
# faster_whisper_worker: word_timestamps=True actually shapes segment["words"]


class _FakeWord:
    def __init__(self, word: str, start: float, end: float) -> None:
        self.word = word
        self.start = start
        self.end = end


class _FakeSegment:
    def __init__(
        self, start: float, end: float, text: str, words: list[_FakeWord] | None
    ) -> None:
        self.start = start
        self.end = end
        self.text = text
        self.words = words


class _FakeInfo:
    language = "en"
    duration = 1.23


class _FakeWhisperModel:
    calls: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    def transcribe(self, path: str, **kwargs: Any):
        _FakeWhisperModel.calls.append(kwargs)
        segments = [
            _FakeSegment(
                0.0,
                0.6,
                " hi there",
                words=[
                    _FakeWord(" hi", 0.0, 0.3),
                    _FakeWord(" there", 0.3, 0.6),
                ],
            ),
            # a segment with no words (guards the falsy-words branch)
            _FakeSegment(0.6, 1.0, " ok", words=None),
        ]
        return segments, _FakeInfo()


def _run_worker_code(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> dict[str, Any]:
    fake_module = type(sys)("faster_whisper")
    fake_module.WhisperModel = _FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)
    options = dict(payload)
    path = str(options.pop("path"))
    request = {
        "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
        "engine": "faster-whisper",
        "audio_path": path,
        "options": options,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    _FakeWhisperModel.calls.clear()
    buf = io.StringIO()
    with redirect_stdout(buf):
        faster_whisper_worker.main()
    envelope = json.loads(buf.getvalue())
    assert envelope["contract_version"] == TRANSCRIPTION_CONTRACT_VERSION
    return envelope["results"][0]


def test_worker_code_requests_word_timestamps(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_worker_code(
        monkeypatch,
        {"path": "/tmp/a.wav", "context": "Frisket, CTranslate2"},
    )
    assert len(_FakeWhisperModel.calls) == 1
    assert _FakeWhisperModel.calls[0]["word_timestamps"] is True
    assert _FakeWhisperModel.calls[0]["initial_prompt"] == "Frisket, CTranslate2"
    assert result["accepted_options"] == {"context": "Frisket, CTranslate2"}
    assert result["model_ids"]
    assert result["revision"]
    assert result["device"] == "cpu"
    assert result["dtype"] == "int8"


def test_worker_code_shapes_captured_words(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run_worker_code(monkeypatch, {"path": "/tmp/a.wav"})
    assert out["segments"][0]["words"] == [
        {"word": "hi", "start": 0.0, "end": 0.3},
        {"word": "there", "start": 0.3, "end": 0.6},
    ]
    # a segment with no words omits the key entirely (not an empty list)
    assert "words" not in out["segments"][1]


def test_worker_rejects_unversioned_local_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"path": "/tmp/a.wav", "vad": False})),
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        faster_whisper_worker.main()

    envelope = json.loads(buf.getvalue())
    assert envelope["contract_version"] == TRANSCRIPTION_CONTRACT_VERSION
    assert envelope["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# segments_with_index: an additive 'words' key survives the generic copy


def test_segments_with_index_preserves_words() -> None:
    segments = [
        {
            "start": 0.0,
            "end": 0.6,
            "text": "hi there",
            "words": [{"word": "hi", "start": 0.0, "end": 0.3}],
        },
        {"start": 0.6, "end": 1.0, "text": "ok"},
    ]
    normalized = segments_with_index(segments)
    assert normalized[0]["segment_index"] == 0
    assert normalized[0]["words"] == [{"word": "hi", "start": 0.0, "end": 0.3}]
    assert "words" not in normalized[1]


def test_segment_with_speaker_preserves_words_and_speaker_metadata() -> None:
    normalized = _segment_with_speaker(
        {
            "start": 0.001,
            "end": 0.604,
            "text": " hi there ",
            "speaker": "speaker-1",
            "speaker_confidence": "approximate",
            "words": [
                {"word": "hi", "start": 0.001, "end": 0.3},
                {"word": "there", "start": 0.3, "end": 0.604},
            ],
        }
    )

    assert normalized == {
        "start": 0.0,
        "end": 0.6,
        "text": "hi there",
        "speaker": "speaker-1",
        "speaker_confidence": "approximate",
        "words": [
            {"word": "hi", "start": 0.001, "end": 0.3},
            {"word": "there", "start": 0.3, "end": 0.604},
        ],
    }


def test_recipe_execute_threads_words_through_faster_whisper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared transcription engine seam:
    words captured by the sandboxed worker land unchanged on the output
    columns' segment dicts."""

    async def fake_run_sandboxed(
        cmd, *, policy, stdin_data, should_cancel=None, extra_env=None
    ):
        del cmd, policy, should_cancel
        request = json.loads(stdin_data)
        return SandboxResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
                    "results": [
                        {
                            "engine": "faster-whisper",
                            "text": "hi there",
                            "segments": [
                                {
                                    "start": 0.0,
                                    "end": 0.6,
                                    "text": "hi there",
                                    "words": [
                                        {"word": "hi", "start": 0.0, "end": 0.3},
                                        {
                                            "word": "there",
                                            "start": 0.3,
                                            "end": 0.6,
                                        },
                                    ],
                                }
                            ],
                            "language": "en",
                            "duration": 0.6,
                            "model_ids": ["faster-whisper/base"],
                            "revision": "runtime-resolved",
                            "device": "cpu",
                            "dtype": "int8",
                            "timings": {"inference_seconds": 0.1},
                            "warnings": [],
                            "accepted_options": request["options"],
                        }
                    ],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.faster_whisper.run_sandboxed", fake_run_sandboxed
    )
    result = asyncio.run(
        transcribe_mod.run_transcription_engine(
            "faster_whisper", str(tmp_path / "a.wav"), {}, OpContext()
        )
    )
    (segment,) = segments_with_index(result.output["segments"])
    assert segment["segment_index"] == 0
    assert segment["words"] == [
        {"word": "hi", "start": 0.0, "end": 0.3},
        {"word": "there", "start": 0.3, "end": 0.6},
    ]


def test_parakeet_default_vad_true_stays_segment_level_no_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parakeet's default (vad=true) path never produces a 'words' key --
    this slice does not flip the VAD default to gain word timing (would give
    up hallucination suppression, a product trade-off out of scope here)."""

    class FakeParakeetSession:
        def __init__(self, *, expected_rows, vad, should_cancel) -> None:
            assert expected_rows == 1
            assert vad is True
            assert should_cancel is None
            self.teardown_failed = False

        def mark_closed(self) -> None:
            pass

        async def close(self) -> None:
            pass

        async def transcribe(self, path: str) -> dict[str, Any]:
            assert path == str(tmp_path / "a.wav")
            return {
                "text": "hi there",
                "segments": [{"start": 0.0, "end": 0.6, "text": "hi there"}],
                "language": "en",
            }

    monkeypatch.setattr(parakeet_mod, "ParakeetProcessSession", FakeParakeetSession)
    result = asyncio.run(
        transcribe_mod.run_transcription_engine(
            "parakeet-tdt", str(tmp_path / "a.wav"), {}, OpContext()
        )
    )
    (segment,) = segments_with_index(result.output["segments"])
    assert "words" not in segment


# ---------------------------------------------------------------------------
# evidence writer: captured words land ms-normalized in selector.temporal.words


def test_evidence_writer_stores_ms_normalized_words_in_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store.media_blobs import media_cell
    from frisket.engine.store.evidence import list_cell_evidence

    project = Project.create(tmp_path / "word-capture-evidence.frisket")
    try:
        sheet_id = project.add_sheet("Episodes")
        cols = {
            "title": project.add_column(sheet_id, "title", type="text"),
            "media": project.add_column(sheet_id, "media", type="audio"),
        }
        blob = project.add_blob(
            b"RIFF0000WAVEfmt words",
            filename="a.wav",
            mime="audio/wav",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 1.0, "kind": "audio"}
            ),
        )
        row_id = project.add_rows(
            sheet_id,
            [
                {
                    "title": "Episode A",
                    "media": media_cell(blob, mime="audio/wav", filename="a.wav"),
                }
            ],
            cols,
        )[0]

        async def fake_faster_whisper(self, path, spec, *, should_cancel=None):
            del self, spec, path
            return {
                "text": "hi there",
                "segments": [
                    {
                        "start": 0.0,
                        "end": 0.6,
                        "text": "hi there",
                        "words": [
                            {"word": "hi", "start": 0.0, "end": 0.3},
                            {"word": "there", "start": 0.3, "end": 0.6},
                        ],
                    }
                ],
                "language": "en",
                "duration": 0.6,
            }

        monkeypatch.setattr(
            transcribe_engines.FasterWhisperAdapter, "transcribe", fake_faster_whisper
        )
        result = run_action_spec(
            project,
            {
                "action_id": "media.transcribe",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": [row_id],
                },
                "output_names": {
                    "text": "transcript",
                    "segments": "transcript_segments",
                },
                "params": {
                    "source": "media",
                    "engine": "faster_whisper",
                },
                "idempotency_key": "media_transcribe@sha256:word-capture",
            },
            project_id="project-word-capture-evidence",
        )
        assert result.status == "completed", result.errors

        transcript_column_id = next(
            c["id"] for c in project.columns(sheet_id) if c["name"] == "transcript"
        )
        cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=transcript_column_id,
            project_id="project-word-capture-evidence",
        )
        (link,) = cell_evidence["links"]
        viewer = resolve_evidence_viewer(
            project, link["stable_id"], project_id="project-word-capture-evidence"
        )
        (artifact,) = viewer["artifacts"]
        (span,) = artifact["spans"]
        assert span["selector"]["temporal"]["words"] == [
            {"word": "hi", "start_ms": 0, "end_ms": 300},
            {"word": "there", "start_ms": 300, "end_ms": 600},
        ]
        assert span["selector"]["temporal"]["segment_index"] == 0
    finally:
        project.close()


def test_evidence_writer_omits_words_when_segment_carries_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store.media_blobs import media_cell
    from frisket.engine.store.evidence import list_cell_evidence

    project = Project.create(tmp_path / "word-capture-absent.frisket")
    try:
        sheet_id = project.add_sheet("Episodes")
        cols = {
            "title": project.add_column(sheet_id, "title", type="text"),
            "media": project.add_column(sheet_id, "media", type="audio"),
        }
        blob = project.add_blob(
            b"RIFF0000WAVEfmt nowords",
            filename="a.wav",
            mime="audio/wav",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 1.0, "kind": "audio"}
            ),
        )
        row_id = project.add_rows(
            sheet_id,
            [
                {
                    "title": "Episode A",
                    "media": media_cell(blob, mime="audio/wav", filename="a.wav"),
                }
            ],
            cols,
        )[0]

        async def fake_parakeet(self, path, spec, *, should_cancel=None):
            del self, spec, path
            return {
                "text": "hi there",
                "segments": [{"start": 0.0, "end": 0.6, "text": "hi there"}],
                "language": "en",
            }

        monkeypatch.setattr(
            transcribe_engines.ParakeetAdapter, "transcribe", fake_parakeet
        )
        stub_parakeet_run_scope(monkeypatch)
        # Local-onnx liveness is artifact-checked at resolution
        # (no_live_target refusal otherwise); this test fakes the engine, so
        # fake the artifact presence probe too.
        monkeypatch.setattr(
            "frisket.execution.definitions.parakeet_artifacts_present",
            lambda: True,
        )
        monkeypatch.setattr(
            "frisket.execution.definitions.parakeet_runtime_present", lambda: True
        )
        result = run_action_spec(
            project,
            {
                "action_id": "media.transcribe",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": [row_id],
                },
                "output_names": {
                    "text": "transcript",
                    "segments": "transcript_segments",
                },
                "params": {
                    "source": "media",
                    "engine": "parakeet-tdt",
                },
                "idempotency_key": "media_transcribe@sha256:word-capture-absent",
            },
            project_id="project-word-capture-absent",
        )
        assert result.status == "completed", result.errors

        transcript_column_id = next(
            c["id"] for c in project.columns(sheet_id) if c["name"] == "transcript"
        )
        cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=transcript_column_id,
            project_id="project-word-capture-absent",
        )
        (link,) = cell_evidence["links"]
        viewer = resolve_evidence_viewer(
            project, link["stable_id"], project_id="project-word-capture-absent"
        )
        (artifact,) = viewer["artifacts"]
        (span,) = artifact["spans"]
        assert "words" not in span["selector"]["temporal"]
    finally:
        project.close()
