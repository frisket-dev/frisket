from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.contracts.action import (
    ActionResult,
    Receipt,
)
from frisket.contracts.action_validation import validate_action_spec
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import root_action_catalog, validate_root_action
from frisket.engine.executor.action_specs import PlacementPolicy, execution_spec_for
from frisket.engine.jobs import RUN_PROJECT_KIND
from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import resolve_timeline
from frisket.engine.store.evidence import (
    TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
    record_evidence_link,
    record_source_span,
)
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results
from frisket.features.topic_segmentation.contracts import (
    BetweenUnits,
    BoundaryCandidate,
    EngineDefinition,
    PreflightResult,
    SegmentationResult,
    TOPIC_ANALYSIS_SIDECAR_COLUMN,
    TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION,
)
from http_test_helpers import drain_queue


def _project(client: TestClient, name: str) -> tuple[str, Project]:
    project_id = client.post("/api/projects", json={"name": name}).json()["id"]
    return project_id, client.app.state.workspace.get(project_id)


def _queue_action(
    client: TestClient,
    project_id: str,
    action: dict[str, Any],
) -> ActionResult:
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=action,
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None
    return result


def _column(project: Project, sheet_id: int, name: str) -> Any:
    row = project.db.execute(
        "SELECT * FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
        (sheet_id, name),
    ).fetchone()
    assert row is not None
    return row


def _run_rows(project: Project, run_id: int, column_id: int) -> list[Any]:
    return project.db.execute(
        "SELECT row_id, value, error, error_code FROM results "
        "WHERE run_id=? AND column_id=? ORDER BY row_id",
        (run_id, column_id),
    ).fetchall()


def test_finder_catalog_types_and_retry_placement() -> None:
    from frisket.server.action_catalog_hints import (
        action_catalog_payload_with_launcher_hints,
    )

    entries = {entry.kind: entry for entry in root_action_catalog().actions}
    served = {
        entry["kind"]: entry
        for entry in action_catalog_payload_with_launcher_hints({})["actions"]
    }
    for kind, source_type, output_type, key in (
        (
            "map.find_topic_sections",
            "timestamped_transcript",
            "timeline_ranges",
            "sections",
        ),
        ("map.find_visual_cuts", "video", "timeline_points", "cuts"),
    ):
        entry = entries[kind]
        assert ACTION_REGISTRY.get(kind).action_id == kind
        assert entry.execution_mode == "per_row"
        assert entry.async_mode == "queued"
        assert entry.cost_policy.kind == "none"
        assert entry.ui_hints["semantic_controls"]["source"] == "column"
        assert entry.ui_hints["source_requirements"][0]["accepted_column_types"] == [
            source_type
        ]
        assert entry.ui_hints["logical_outputs"] == [
            {"key": key, "column_type": output_type}
        ]
        assert (
            execution_spec_for(kind).lifecycle.placement
            is PlacementPolicy.QUEUED_PROJECT_RUN
        )
    assert (
        entries["map.find_topic_sections"].ui_hints["semantic_controls"]["engine"]
        == "engine"
    )
    engines = entries["map.find_topic_sections"].ui_hints["engines"]
    assert [engine["id"] for engine in engines] == ["deep_tiling", "texttiling"]
    assert all("available" not in engine for engine in engines)
    assert all(
        "available" in engine
        for engine in served["map.find_topic_sections"]["ui_hints"]["engines"]
    )


@pytest.mark.parametrize(
    ("runtime_state", "available", "error_fragment"),
    [
        ("installed", True, None),
        ("unavailable", False, "bundled FastEmbed runtime"),
        ("disabled", False, "FRISKET_DISABLE_LOCAL_EMBED"),
    ],
)
def test_topic_engine_runtime_availability_is_only_a_served_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_state: str,
    available: bool,
    error_fragment: str | None,
) -> None:
    import frisket.features.topic_segmentation.deep_tiling as deep_tiling
    from frisket.actions.system import root_action_catalog_payload
    from frisket.server.action_catalog_hints import (
        action_catalog_payload_with_launcher_hints,
        project_action_catalog_payload_with_launcher_hints,
    )

    static_before = json.dumps(
        root_action_catalog_payload(), sort_keys=True, separators=(",", ":")
    )
    real_find_spec = deep_tiling.importlib.util.find_spec
    marker = object()

    def controlled_find_spec(name: str, *args, **kwargs):
        if name == "fastembed":
            return marker if runtime_state == "installed" else None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(deep_tiling.importlib.util, "find_spec", controlled_find_spec)
    if runtime_state == "disabled":
        monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    else:
        monkeypatch.delenv("FRISKET_DISABLE_LOCAL_EMBED", raising=False)

    assert (
        json.dumps(root_action_catalog_payload(), sort_keys=True, separators=(",", ":"))
        == static_before
    )

    global_payload = action_catalog_payload_with_launcher_hints({})
    project = Project.create(tmp_path / "catalog.frisket")
    try:
        project_payload = project_action_catalog_payload_with_launcher_hints(
            project, sidecar_capabilities={}
        )
    finally:
        project.close()

    for payload in (global_payload, project_payload):
        topic_entry = next(
            entry
            for entry in payload["actions"]
            if entry["kind"] == "map.find_topic_sections"
        )
        deep_tiling_hint = next(
            engine
            for engine in topic_entry["ui_hints"]["engines"]
            if engine["id"] == "deep_tiling"
        )
        assert deep_tiling_hint["available"] is available
        if error_fragment is None:
            assert deep_tiling_hint["error"] is None
        else:
            assert error_fragment in deep_tiling_hint["error"]


def test_topic_finder_validation_rejects_unknown_engine_and_settings() -> None:
    for params in (
        {"source": "transcript", "engine": "unknown"},
        {"source": "transcript", "settings": {"unknown": True}},
    ):
        validation = validate_root_action(
            {
                "action_id": "map.find_topic_sections",
                "scope": {"kind": "sheet_rows", "sheet_id": 1},
                "params": params,
                "idempotency_key": "bad-topic",
            }
        )
        assert not validation.ok


def test_visual_finder_queues_multi_row_map_and_keeps_row_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id, project = _project(client, "Visual finder")
    sheet_id = project.add_sheet("Videos")
    video_column_id = project.add_column(sheet_id, "video", "video")
    blob_hashes = [
        project.add_blob(
            payload,
            filename=f"source-{index}.mp4",
            mime="video/mp4",
            metadata=owned_media_metadata_document(probe={"duration_seconds": 4.0}),
        )
        for index, payload in enumerate((b"good-video", b"bad-video"), start=1)
    ]
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "video": {
                    "blob": blob_hash,
                    "filename": f"source-{index}.mp4",
                    "mime": "video/mp4",
                }
            }
            for index, blob_hash in enumerate(blob_hashes, start=1)
        ],
        {"video": video_column_id},
    )
    expected_timeline = resolve_timeline(
        project,
        sheet_id=sheet_id,
        row_id=row_ids[0],
        column_id=video_column_id,
    ).anchor.wire_value()
    calls: list[bytes] = []

    def fake_visual_cuts(path: Path, *, timeline: dict[str, Any]):
        payload = Path(path).read_bytes()
        calls.append(payload)
        if payload == b"bad-video":
            raise ValueError("fixture visual detector failure")
        return {
            "schema_version": "frisket.timeline_points.v1",
            "timeline": timeline,
            "items": [
                {
                    "id": "visual-cut-0000000010",
                    "at_ms": 1_000,
                    "metadata": {
                        "detector": "pyscenedetect.content",
                        "frame": 10,
                    },
                }
            ],
        }

    monkeypatch.setattr(
        "frisket.engine.executor.visual_cuts_read.visual_cuts_value",
        fake_visual_cuts,
    )
    result = _queue_action(
        client,
        project_id,
        {
            "action_id": "map.find_visual_cuts",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": row_ids,
            },
            "params": {"source": "video"},
            "output_names": {"cuts": "Visual cuts"},
            "idempotency_key": "visual-finder@sha256:test",
        },
    )

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.max_attempts == 1
    assert job.payload["spec"]["action_kind"] == "map.find_visual_cuts"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["v1_action"]["params"] == {"source": "video"}
    assert job.payload["v1_action"]["output_names"] == {"cuts": "Visual cuts"}
    assert job.payload["spec"]["row_ids"] == row_ids
    drain_queue(client)

    assert sorted(calls) == [b"bad-video", b"good-video"]
    run = project.db.execute(
        "SELECT * FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert (run["status"], run["total_rows"], run["completed_rows"]) == (
        "completed",
        2,
        2,
    )
    assert run["failed_rows"] == 1

    output_column = _column(project, sheet_id, "Visual cuts")
    assert output_column["type"] == "timeline_points"
    values = project.get_values(
        sheet_id,
        int(output_column["id"]),
        row_ids=row_ids,
    )
    assert values[row_ids[0]]["schema_version"] == "frisket.timeline_points.v1"
    assert values[row_ids[0]]["timeline"] == expected_timeline
    assert values[row_ids[0]]["items"][0]["metadata"]["detector"] == (
        "pyscenedetect.content"
    )
    assert values[row_ids[1]] is None
    rows = _run_rows(project, int(result.run_id), int(output_column["id"]))
    assert rows[0]["error"] is None
    assert "fixture visual detector failure" in rows[1]["error"]
    assert rows[1]["error_code"] == "model_error"

    receipt = Receipt.model_validate(
        json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()["body"]
        )
    )
    evidence = {item.ref["kind"]: item.ref for item in receipt.evidence}
    counts = evidence["map_rows_run_counts"]
    assert counts["failed_row_ids"] == [row_ids[1]]
    assert (counts["total_rows"], counts["completed_rows"], counts["failed_rows"]) == (
        2,
        2,
        1,
    )
    output_ref = receipt.outputs[0].ref
    assert output_ref["type"] == "timeline_points"
    assert output_ref["source_columns"] == ["video"]
    assert output_ref["analysis"] == "map.find_visual_cuts"

    replay_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=job.payload["v1_action"],
    )
    assert replay_response.status_code == 200, replay_response.text
    replay = ActionResult.model_validate(replay_response.json())
    assert replay.receipt_id == result.receipt_id
    assert replay.status == "partial"
    assert sorted(calls) == [b"bad-video", b"good-video"]


def test_visual_finder_has_one_strict_typed_request_contract() -> None:
    request = {
        "action_id": "map.find_visual_cuts",
        "scope": {"kind": "sheet_rows", "sheet_id": 1, "row_ids": [1]},
        "params": {"source": "video"},
        "output_names": {"cuts": "Scene boundaries"},
        "idempotency_key": "visual-cuts-contract",
    }
    valid = validate_root_action(request)
    assert valid.ok
    assert valid.action.output_names == {"cuts": "Scene boundaries"}
    for invalid in (
        {**request, "params": {"source": "video", "threshold": 27}},
        {**request, "params": {"source": "video", "input_column": "video"}},
        {**request, "params": {"source": 1}},
        {**request, "scope": {"kind": "project"}},
        {**request, "output_names": {"unknown": "Scene boundaries"}},
    ):
        assert not validate_root_action(invalid).ok
    assert not validate_action_spec(
        {
            "schema_version": "frisket.action.v2",
            "kind": "map.find_visual_cuts",
            "capabilities": ["project:write"],
            "params": {"sheet_id": 1, "input_column": "video"},
            "idempotency_key": "retired-visual-cuts-contract",
        }
    ).ok


def test_topic_finder_accepts_fresh_transcript_evidence_with_clamped_overrun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.engine.executor.temporal_transcripts import (
        resolve_timestamped_transcript,
    )

    client = TestClient(create_app(tmp_path / "workspace"))
    project_id, project = _project(client, "Overrun transcript")
    sheet_id = project.add_sheet("Audio")
    audio_column_id = project.add_column(sheet_id, "audio", "audio")
    blob_hash = project.add_blob(
        b"fixture-audio",
        filename="fixture.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(probe={"duration_seconds": 4.0}),
    )
    row_id = project.add_rows(
        sheet_id,
        [
            {
                "audio": {
                    "blob": blob_hash,
                    "filename": "fixture.wav",
                    "mime": "audio/wav",
                }
            }
        ],
        {"audio": audio_column_id},
    )[0]

    async def fake_faster_whisper(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        del self, path, spec, should_cancel
        return {
            "text": "First segment. Second segment. Tail.",
            "language": "en",
            "duration": 4.0,
            "segments": [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "text": "First segment.",
                    "words": [
                        {"word": "First", "start": 0.0, "end": 1.0},
                        {"word": "segment.", "start": 1.0, "end": 2.0},
                    ],
                },
                {
                    "start": 2.0,
                    "end": 4.01,
                    "text": "Second segment.",
                    "words": [
                        {"word": "Second", "start": 2.0, "end": 3.0},
                        {"word": "segment.", "start": 3.7, "end": 4.01},
                    ],
                },
                {
                    "start": 4.01,
                    "end": 4.02,
                    "text": "Tail.",
                    "words": [{"word": "Tail.", "start": 4.01, "end": 4.02}],
                },
            ],
        }

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", fake_faster_whisper
    )
    transcribe_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": "media.transcribe",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": [row_id]},
            "output_names": {"text": "transcript", "segments": "transcript_segments"},
            "params": {
                "source": "audio",
                "engine": "faster_whisper",
            },
            "idempotency_key": "transcribe-overrun@sha256:test",
        },
    )
    assert transcribe_response.status_code == 200, transcribe_response.text
    transcribe_result = ActionResult.model_validate(transcribe_response.json())
    assert transcribe_result.status == "queued"
    assert transcribe_result.run_id is not None
    assert transcribe_result.job_id is not None
    drain_queue(client)
    transcribe_run = project.db.execute(
        "SELECT status, completed_rows, failed_rows FROM runs WHERE id=?",
        (transcribe_result.run_id,),
    ).fetchone()
    assert transcribe_run is not None
    assert tuple(transcribe_run) == ("completed", 1, 0)

    transcript_column = _column(project, sheet_id, "transcript")
    _values, current_refs = project.get_values_with_refs(
        sheet_id, int(transcript_column["id"]), row_ids=[row_id]
    )
    evidence_link = project.db.execute(
        "SELECT * FROM evidence_links WHERE row_id=? AND column_id=? "
        "AND link_role='media_transcribe_temporal'",
        (row_id, int(transcript_column["id"])),
    ).fetchone()
    assert evidence_link is not None
    assert json.loads(evidence_link["subject_ref_json"]) == current_refs[row_id]
    resolved = resolve_timestamped_transcript(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=int(transcript_column["id"]),
    )
    assert resolved is not None
    assert resolved.evidence_link_id == int(evidence_link["id"])
    assert resolved.transcript_run_id == transcribe_result.run_id
    evidence_spans = project.db.execute(
        "SELECT els.rank, sp.start_ms, sp.end_ms, sp.selector_json "
        "FROM evidence_link_spans els JOIN source_spans sp ON sp.id=els.span_id "
        "WHERE els.link_id=? ORDER BY els.rank",
        (evidence_link["id"],),
    ).fetchall()
    assert [
        (row["rank"], row["start_ms"], row["end_ms"]) for row in evidence_spans
    ] == [
        (0, 0, 2_000),
        (1, 2_000, 4_000),
    ]
    for expected_index, span in enumerate(evidence_spans):
        selector = json.loads(span["selector_json"])
        assert selector["segment_index"] == expected_index
        for word in selector.get("words", []):
            assert (
                span["start_ms"] <= word["start_ms"] < word["end_ms"] <= span["end_ms"]
            )
    assert json.loads(evidence_spans[-1]["selector_json"])["words"][-1] == {
        "word": "segment.",
        "start_ms": 3_700,
        "end_ms": 4_000,
    }

    class _OverrunTopicSegmenter(_ExactTopicSegmenter):
        def preflight(self, snapshot, settings):
            assert snapshot.source_kind == "timestamped_transcript"
            assert snapshot.units[0].speaker is None
            assert settings == {"detail": "more"}
            return PreflightResult.passed()

    engine = _OverrunTopicSegmenter()

    def exact_getter(engine_id: str):
        assert engine_id == "texttiling"
        return engine

    monkeypatch.setattr(
        "frisket.features.topic_segmentation.engines.get_segmenter",
        exact_getter,
    )
    finder_result = _queue_action(
        client,
        project_id,
        {
            "action_id": "map.find_topic_sections",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": [row_id]},
            "params": {
                "source": "transcript",
                "engine": "texttiling",
                "settings": {"detail": "more"},
            },
            "output_names": {"sections": "Topic sections"},
            "idempotency_key": "topic-overrun@sha256:test",
        },
    )
    drain_queue(client)
    finder_run = project.db.execute(
        "SELECT status, completed_rows, failed_rows FROM runs WHERE id=?",
        (finder_result.run_id,),
    ).fetchone()
    assert finder_run is not None
    assert tuple(finder_run) == ("completed", 1, 0)
    assert engine.segment_calls == 1
    output_column = _column(project, sheet_id, "Topic sections")
    assert (
        project.get_values(sheet_id, int(output_column["id"]), row_ids=[row_id])[row_id]
        is not None
    )


class _ExactTopicSegmenter:
    definition = EngineDefinition(
        id="texttiling",
        version="fixture-1",
        label="Exact fixture engine",
        description="Returns one boundary between the first two units.",
    )

    def __init__(self) -> None:
        self.segment_calls = 0

    def validate_settings(self, settings):
        assert settings == {"detail": "more"}
        return {"detail": "more"}

    def preflight(self, snapshot, settings):
        assert snapshot.source_kind == "timestamped_transcript"
        assert snapshot.units[0].speaker == "S1"
        assert settings == {"detail": "more"}
        return PreflightResult.passed()

    def segment(self, snapshot, settings, context):
        context.raise_if_cancelled()
        self.segment_calls += 1
        return SegmentationResult(
            engine_id="texttiling",
            engine_version="fixture-1",
            boundaries=(
                BoundaryCandidate(
                    id="topic-boundary-1",
                    locator=BetweenUnits(
                        left_unit_id=snapshot.units[0].id,
                        right_unit_id=snapshot.units[1].id,
                    ),
                    strength=0.9,
                    label="Subject changes",
                    diagnostics={"fixture": True},
                ),
            ),
            resolved_settings={"detail": "more"},
            diagnostics={"algorithm": "fixture"},
        )


def _seed_timestamped_transcripts(
    project: Project,
) -> tuple[int, list[int], int, dict[str, Any]]:
    sheet_id = project.add_sheet("Interviews")
    video_column_id = project.add_column(sheet_id, "video", "video")
    blob_hashes = [
        project.add_blob(
            f"video-{index}".encode(),
            filename=f"video-{index}.mp4",
            mime="video/mp4",
            metadata=owned_media_metadata_document(probe={"duration_seconds": 4.0}),
        )
        for index in (1, 2)
    ]
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "video": {
                    "blob": blob_hash,
                    "filename": f"video-{index}.mp4",
                    "mime": "video/mp4",
                }
            }
            for index, blob_hash in enumerate(blob_hashes, start=1)
        ],
        {"video": video_column_id},
    )
    leases = [
        resolve_timeline(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=video_column_id,
        )
        for row_id in row_ids
    ]

    transcript_column_id = project.add_column(
        sheet_id,
        "transcript",
        "timestamped_transcript",
        ai_generated=True,
    )
    op_id = project.append_op(
        "media.transcribe",
        {"kind": "media.transcribe", "params": {"output_name": "transcript"}},
    )
    runs = RunResultStore(project)
    run_id = runs.start_run(
        op_id,
        sheet_id,
        "media.transcribe",
        params={"output_name": "transcript"},
        total_rows=2,
        row_ids=row_ids,
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_ids[0],
                "column_id": transcript_column_id,
                "value": "First subject. Second subject.",
            },
            {
                "row_id": row_ids[1],
                "column_id": transcript_column_id,
                "value": "Transcript without timing evidence.",
            },
        ],
    )
    runs.finish_run(run_id)
    runs.point_column_at_run(op_id, transcript_column_id, run_id)

    span_refs = []
    for rank, (start_ms, end_ms, quote) in enumerate(
        [(0, 2_000, "First subject."), (2_000, 4_000, "Second subject.")]
    ):
        span = record_source_span(
            project,
            artifact_id=leases[0].anchor.artifact_id,
            span_kind="temporal",
            start_ms=start_ms,
            end_ms=end_ms,
            quote=quote,
            selector={
                "segment_index": rank,
                **({"speaker": "S1"} if rank == 0 else {}),
            },
        )
        span_refs.append({"span_id": span["id"], "rank": rank})
    _values, refs = project.get_values_with_refs(
        sheet_id,
        transcript_column_id,
        row_ids=[row_ids[0]],
    )
    link = record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref=refs[row_ids[0]],
        spans=span_refs,
        sheet_id=sheet_id,
        row_id=row_ids[0],
        column_id=transcript_column_id,
        run_id=run_id,
        op_id=op_id,
        link_role="media_transcribe_temporal",
        producer={"action_kind": "media.transcribe"},
        metadata={
            "schema_version": TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
            "semantic_type": "timestamped_transcript",
            "language": "en",
        },
    )
    return sheet_id, row_ids, transcript_column_id, link


def test_topic_finder_uses_exact_engine_and_persists_ranges_and_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id, project = _project(client, "Topic finder")
    sheet_id, row_ids, _transcript_column_id, source_link = (
        _seed_timestamped_transcripts(project)
    )
    engine = _ExactTopicSegmenter()
    selected_engine_ids: list[str] = []

    def exact_getter(engine_id: str):
        selected_engine_ids.append(engine_id)
        assert engine_id == "texttiling"
        return engine

    monkeypatch.setattr(
        "frisket.features.topic_segmentation.engines.get_segmenter",
        exact_getter,
    )
    result = _queue_action(
        client,
        project_id,
        {
            "action_id": "map.find_topic_sections",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
            "params": {
                "source": "transcript",
                "engine": "texttiling",
                "settings": {"detail": "more"},
            },
            "output_names": {"sections": "Topic sections"},
            "idempotency_key": "topic-finder@sha256:test",
        },
    )

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.max_attempts == 1
    assert job.payload["spec"]["action_kind"] == "map.find_topic_sections"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["spec"]["input_columns"] == ["transcript"]
    assert job.payload["spec"]["params"]["engine"] == "texttiling"
    assert job.payload["spec"]["params"]["settings"] == {"detail": "more"}
    drain_queue(client)

    assert selected_engine_ids
    assert set(selected_engine_ids) == {"texttiling"}
    assert engine.segment_calls == 1
    run = project.db.execute(
        "SELECT * FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert (run["status"], run["total_rows"], run["completed_rows"]) == (
        "completed",
        2,
        2,
    )
    assert run["failed_rows"] == 1

    output_column = _column(project, sheet_id, "Topic sections")
    assert output_column["type"] == "timeline_ranges"
    values = project.get_values(
        sheet_id,
        int(output_column["id"]),
        row_ids=row_ids,
    )
    value = values[row_ids[0]]
    assert value["schema_version"] == "frisket.timeline_ranges.v1"
    assert [(item["start_ms"], item["end_ms"]) for item in value["items"]] == [
        (0, 2_000),
        (2_000, 4_000),
    ]
    assert [item["id"] for item in value["items"]] == [
        "topic-section-0001",
        "topic-section-0002",
    ]
    assert all(item["label"] is None for item in value["items"])
    assert all(item["metadata"] == {} for item in value["items"])

    sidecar_column = project.db.execute(
        "SELECT * FROM columns WHERE sheet_id=? AND name=? AND hidden=1",
        (sheet_id, TOPIC_ANALYSIS_SIDECAR_COLUMN),
    ).fetchone()
    assert sidecar_column is not None
    sidecar_values = project.get_values(
        sheet_id,
        int(sidecar_column["id"]),
        row_ids=row_ids,
    )
    analysis_sidecar = sidecar_values[row_ids[0]]
    assert analysis_sidecar["schema_version"] == (TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION)
    assert analysis_sidecar["engine_id"] == "texttiling"
    assert analysis_sidecar["engine_version"] == "fixture-1"
    assert analysis_sidecar["resolved_settings"] == {"detail": "more"}
    assert analysis_sidecar["transcript_evidence_id"] == source_link["stable_id"]
    assert analysis_sidecar["transcript_run_id"] == source_link["run_id"]
    assert analysis_sidecar["transcript_run_id"] > 0
    native_candidate = analysis_sidecar["native_candidates"][0]
    assert native_candidate["id"] == "topic-boundary-1"
    assert native_candidate["locator"]["kind"] == "between_units"
    assert (
        native_candidate["locator"]["left_unit_id"]
        != (native_candidate["locator"]["right_unit_id"])
    )
    assert native_candidate["strength"] == 0.9
    assert native_candidate["label"] == "Subject changes"
    assert native_candidate["diagnostics"] == {"fixture": True}
    assert sidecar_values[row_ids[1]] is None
    assert values[row_ids[1]] is None
    rows = _run_rows(project, int(result.run_id), int(output_column["id"]))
    assert rows[0]["error"] is None
    assert "no current timestamped-transcript evidence" in rows[1]["error"]
    assert rows[1]["error_code"] == "stale_input"

    receipt = Receipt.model_validate(
        json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()["body"]
        )
    )
    evidence = {item.ref["kind"]: item.ref for item in receipt.evidence}
    analysis = evidence["topic_segmentation_analysis"]["rows"]
    assert [row["row_id"] for row in analysis] == [row_ids[0]]
    assert analysis[0]["engine_id"] == "texttiling"
    assert analysis[0]["engine_version"] == "fixture-1"
    assert analysis[0]["resolved_settings"] == {"detail": "more"}
    assert analysis[0]["transcript_evidence_id"] == source_link["stable_id"]
    assert analysis[0]["timeline_ranges_hash"].startswith("sha256:")
    assert evidence["topic_segmentation_analysis"]["sidecar_column_id"] == int(
        sidecar_column["id"]
    )
    assert len(receipt.outputs) == 1
    output_ref = receipt.outputs[0].ref
    assert output_ref["type"] == "timeline_ranges"
    assert output_ref["source_columns"] == ["transcript"]
    assert output_ref["analysis"] == "map.find_topic_sections"
