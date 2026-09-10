from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

from pathlib import Path

import pytest

from frisket.contracts.action import (
    ActionError,
    Receipt,
)
from frisket.engine.executor.transcript_read import ACTION_KIND
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.temporal_transcripts import resolve_timestamped_transcript
from frisket.engine.jobs.worker import register_action_job_binding_handlers
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import (
    resolve_artifact_timeline,
    resolve_timeline,
)
from frisket.engine.store.evidence import (
    TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
    record_evidence_link,
    record_source_span,
)
from frisket.engine.store.runs import RunResultStore
from frisket.features.topic_segmentation.contracts import (
    TOPIC_ANALYSIS_SIDECAR_COLUMN,
    TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION,
)
from helpers import write_claimed_test_results


def _seed_timestamped_transcript(
    project: Project,
    *,
    span_values: list[tuple[int, int, str]] | None = None,
) -> dict[str, int | str]:
    span_values = span_values or [
        (1_000, 4_000, "Shared chunk."),
        (4_000, 7_000, "Final chunk."),
    ]
    sheet_id = project.add_sheet("Media")
    media_column_id = project.add_column(sheet_id, "video", "video")
    blob_hash = project.add_blob(
        b"source video",
        filename="source.mp4",
        mime="video/mp4",
        metadata=owned_media_metadata_document(probe={"duration_seconds": 8.0}),
    )
    row_id = project.add_rows(
        sheet_id,
        [
            {
                "video": {
                    "blob": blob_hash,
                    "filename": "source.mp4",
                    "mime": "video/mp4",
                }
            }
        ],
        {"video": media_column_id},
    )[0]
    lease = resolve_timeline(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=media_column_id,
    )
    transcript_column_id = project.add_column(
        sheet_id,
        "transcript",
        "timestamped_transcript",
        ai_generated=True,
    )
    op_id = project.append_op(
        "media.transcribe",
        {"kind": "media.transcribe", "params": {"output_name": "transcript"}},
        label="transcribe",
    )
    runs = RunResultStore(project)
    run_id = runs.start_run(
        op_id,
        sheet_id,
        "media.transcribe",
        params={"output_name": "transcript"},
        total_rows=1,
        row_ids=[row_id],
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": transcript_column_id,
                "value": " ".join(value[2] for value in span_values),
            }
        ],
    )
    runs.finish_run(run_id)
    runs.point_column_at_run(op_id, transcript_column_id, run_id)
    spans = []
    for index, (start_ms, end_ms, quote) in enumerate(span_values):
        span = record_source_span(
            project,
            artifact_id=lease.anchor.artifact_id,
            span_kind="temporal",
            start_ms=start_ms,
            end_ms=end_ms,
            quote=quote,
            selector={"segment_index": index},
        )
        spans.append({"span_id": span["id"], "rank": index})
    _values, refs = project.get_values_with_refs(
        sheet_id,
        transcript_column_id,
        row_ids=[row_id],
    )
    link = record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref=refs[row_id],
        spans=spans,
        sheet_id=sheet_id,
        row_id=row_id,
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
    return {
        "sheet_id": sheet_id,
        "row_id": row_id,
        "transcript_column_id": transcript_column_id,
        "evidence_stable_id": str(link["stable_id"]),
    }


def _run(
    project: Project,
    seeded: dict[str, int | str],
    *,
    selection: dict,
    target: str,
    key: str,
):
    return run_action_spec(
        project,
        {
            "action_id": ACTION_KIND,
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": seeded["sheet_id"],
                "row_ids": [seeded["row_id"]],
            },
            "params": {"source": "transcript", "selection": selection},
            "sheet_name": target,
            "idempotency_key": key,
        },
        project_id="project-test",
    )


def test_points_repeat_a_shared_boundary_chunk_and_keep_exact_evidence(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "transcript-points.frisket")
    try:
        seeded = _seed_timestamped_transcript(project)
        result = _run(
            project,
            seeded,
            selection={"kind": "draft_points", "items": [{"at_ms": 2_500}]},
            target="Transcript segments",
            key="transcript-points",
        )
        assert result.status == "completed"
        sheet_id = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        transcript_output = next(
            output for output in result.outputs if output.name == "transcript"
        )
        source_range_output = next(
            output for output in result.outputs if output.name == "source_range"
        )
        row_ids = next(
            output.row_ids for output in result.outputs if output.kind == "rows"
        )
        assert sheet_id is not None
        assert transcript_output.column_id is not None
        assert source_range_output.column_id is not None
        assert row_ids is not None and len(row_ids) == 2

        transcript_values = project.get_values(
            sheet_id,
            transcript_output.column_id,
            row_ids=row_ids,
        )
        assert transcript_values[row_ids[0]] == "Shared chunk."
        assert transcript_values[row_ids[1]] == "Shared chunk. Final chunk."
        ranges = project.get_values(
            sheet_id,
            source_range_output.column_id,
            row_ids=row_ids,
        )
        assert (
            ranges[row_ids[0]]["item"]["start_ms"],
            ranges[row_ids[0]]["item"]["end_ms"],
        ) == (0, 4_000)
        assert (
            ranges[row_ids[1]]["item"]["start_ms"],
            ranges[row_ids[1]]["item"]["end_ms"],
        ) == (1_000, 8_000)

        resolved = [
            resolve_timestamped_transcript(
                project,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=transcript_output.column_id,
            )
            for row_id in row_ids
        ]
        assert all(item is not None for item in resolved)
        assert [item.language for item in resolved if item is not None] == ["en", "en"]
        assert [span["quote"] for span in resolved[0].spans] == ["Shared chunk."]
        assert [span["quote"] for span in resolved[1].spans] == [
            "Shared chunk.",
            "Final chunk.",
        ]
    finally:
        project.close()


def test_overlapping_asr_chunks_are_projected_whole_without_transitive_growth(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "transcript-overlap.frisket")
    try:
        seeded = _seed_timestamped_transcript(
            project,
            span_values=[
                (0, 1_300, "w0"),
                (1_000, 2_300, "w1"),
                (2_000, 3_300, "w2"),
                (3_000, 4_300, "w3"),
                (4_000, 5_300, "w4"),
            ],
        )
        result = _run(
            project,
            seeded,
            selection={
                "kind": "draft_ranges",
                "items": [
                    {"start_ms": 0, "end_ms": 3_300},
                    {"start_ms": 2_000, "end_ms": 6_300},
                ],
            },
            target="Overlapping transcript segments",
            key="transcript-overlap",
        )

        assert result.status == "completed"
        transcript_output = next(
            output for output in result.outputs if output.name == "transcript"
        )
        row_ids = next(
            output.row_ids for output in result.outputs if output.kind == "rows"
        )
        assert row_ids is not None
        values = project.get_values(
            transcript_output.sheet_id,
            transcript_output.column_id,
            row_ids=row_ids,
        )
        assert [values[row_id] for row_id in row_ids] == [
            "w0 w1 w2 w3",
            "w1 w2 w3 w4",
        ]
        assert all("partial speech" not in values[row_id] for row_id in row_ids)

        for row_id in row_ids:
            projected = resolve_timestamped_transcript(
                project,
                sheet_id=transcript_output.sheet_id,
                row_id=row_id,
                column_id=transcript_output.column_id,
            )
            assert projected is not None
            assert all(
                span["metadata"]["projection"]["clipping"] == "full"
                for span in projected.spans
            )
    finally:
        project.close()


def test_transcript_split_projects_typed_annotations_on_the_locked_chunk(
    tmp_path: Path,
) -> None:
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "transcript-annotations.frisket")
    try:
        seeded = _seed_timestamped_transcript(project)
        source = resolve_timestamped_transcript(
            project,
            sheet_id=int(seeded["sheet_id"]),
            row_id=int(seeded["row_id"]),
            column_id=int(seeded["transcript_column_id"]),
        )
        assert source is not None
        anchor = resolve_artifact_timeline(project, source.artifact_id)
        marker_column_id = project.add_column(
            int(seeded["sheet_id"]), "markers", "timeline_points"
        )
        project.apply_edits(
            [
                {
                    "row_id": int(seeded["row_id"]),
                    "column_id": marker_column_id,
                    "value": {
                        "schema_version": "frisket.timeline_points.v1",
                        "timeline": anchor.wire_value(),
                        "items": [
                            {
                                "id": "inside",
                                "at_ms": 2_500,
                                "label": "Quoted claim",
                            },
                            {"id": "outside", "at_ms": 5_000},
                        ],
                    },
                }
            ],
            label="seed transcript annotations",
        )
        selection = {
            "kind": "draft_ranges",
            "items": [{"start_ms": 2_500, "end_ms": 3_000}],
        }
        result = _run(
            project,
            seeded,
            selection=selection,
            target="Annotated transcript",
            key="transcript-annotations",
        )

        assert result.status == "completed"
        marker_output = next(
            output for output in result.outputs if output.name == "markers"
        )
        row_id = next(
            output.row_ids[0] for output in result.outputs if output.kind == "rows"
        )
        projected = project.get_values(
            marker_output.sheet_id,
            marker_output.column_id,
            row_ids=[row_id],
        )[row_id]
        assert projected["timeline"]["duration_ms"] == 3_000
        assert projected["items"] == [
            {
                "id": "inside",
                "at_ms": 1_500,
                "label": "Quoted claim",
                "metadata": {},
            }
        ]
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert receipt is not None
        assert any(
            item.ref.get("kind") == "temporal_annotation_snapshot"
            for item in receipt.inputs
        )
        assert any(
            item.ref.get("kind") == "temporal_annotation_projection"
            for item in receipt.evidence
        )

        replay = _run(
            project,
            seeded,
            selection=selection,
            target="Annotated transcript",
            key="transcript-annotations",
        )
        assert replay.status == "completed"
        assert replay.receipt_id == result.receipt_id
    finally:
        project.close()


def test_topic_range_must_name_the_selected_transcript_evidence(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "transcript-topic-mismatch.frisket")
    try:
        seeded = _seed_timestamped_transcript(project)
        source = resolve_timestamped_transcript(
            project,
            sheet_id=int(seeded["sheet_id"]),
            row_id=int(seeded["row_id"]),
            column_id=int(seeded["transcript_column_id"]),
        )
        assert source is not None
        timeline = resolve_artifact_timeline(project, source.artifact_id).wire_value()
        topic_column_id = project.add_column(
            int(seeded["sheet_id"]),
            "Topic sections",
            "timeline_ranges",
            ai_generated=True,
        )
        sidecar_column_id = project.add_column(
            int(seeded["sheet_id"]),
            TOPIC_ANALYSIS_SIDECAR_COLUMN,
            "json",
            ai_generated=True,
            hidden=True,
        )
        topic_value = {
            "schema_version": "frisket.timeline_ranges.v1",
            "timeline": timeline,
            "items": [
                {
                    "id": "topic-1",
                    "start_ms": 0,
                    "end_ms": 4_000,
                    "metadata": {},
                }
            ],
        }
        op_id = project.append_op(
            "map.find_topic_sections",
            {"action_kind": "map.find_topic_sections"},
            label="find topic sections",
        )
        runs = RunResultStore(project)
        topic_run_id = runs.start_run(
            op_id,
            int(seeded["sheet_id"]),
            "map.find_topic_sections",
            total_rows=1,
            row_ids=[int(seeded["row_id"])],
        )
        write_claimed_test_results(
            project,
            topic_run_id,
            [
                {
                    "row_id": int(seeded["row_id"]),
                    "column_id": topic_column_id,
                    "value": topic_value,
                    "outcome": "ok",
                },
                {
                    "row_id": int(seeded["row_id"]),
                    "column_id": sidecar_column_id,
                    "value": {
                        "schema_version": TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION,
                        "transcript_evidence_id": "evidence_link:other",
                        "transcript_snapshot_hash": source.snapshot_hash,
                        "transcript_run_id": source.transcript_run_id,
                        "transcript_column_id": source.transcript_column_id,
                        "transcript_value_ref": source.transcript_value_ref,
                        "transcript_value_hash": "sha256:" + "a" * 64,
                        "artifact_stable_id": source.artifact_stable_id,
                        "timeline": timeline,
                        "language": source.language,
                        "engine_id": "fixture-engine",
                        "engine_version": "1",
                        "resolved_settings": {},
                        "engine_diagnostics": {},
                        "native_candidates": [],
                        "locking": {},
                    },
                    "outcome": "ok",
                },
            ],
        )
        runs.finish_run(topic_run_id)
        runs.point_column_at_run(op_id, topic_column_id, topic_run_id)
        runs.point_column_at_run(op_id, sidecar_column_id, topic_run_id)
        result = _run(
            project,
            seeded,
            selection={"kind": "column", "column": "Topic sections"},
            target="Should not exist",
            key="topic-mismatch",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "timeline_mismatch"
    finally:
        project.close()


def test_transcript_segments_has_a_dedicated_action_job_executor() -> None:
    from frisket.engine.executor.action_bindings import action_job_bindings
    from frisket.engine.jobs.worker import HandlerRegistry

    registry = HandlerRegistry()
    register_action_job_binding_handlers(registry)

    assert registry.action_executor(ACTION_KIND) is action_job_bindings()[ACTION_KIND]


def _queued_transcript_action(
    seeded: dict[str, int | str], *, target: str, key: str
) -> dict:
    return {
        "action_id": ACTION_KIND,
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": seeded["sheet_id"],
            "row_ids": [seeded["row_id"]],
        },
        "params": {
            "source": "transcript",
            "selection": {
                "kind": "draft_points",
                "items": [{"at_ms": 2_500}],
            },
        },
        "sheet_name": target,
        "idempotency_key": key,
    }


def test_transcript_binding_runs_through_public_action_job_lifecycle(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from frisket.contracts.action import ActionResult
    from frisket.engine.jobs.queue import ACTION_RUN_KIND
    from frisket.engine.jobs.worker import Worker
    from frisket.server.app import create_app

    target = "Bound transcript segments"
    key = "transcript-binding-lifecycle@sha256:stable"
    with TestClient(create_app(tmp_path / "binding-lifecycle-workspace")) as client:
        project_id = client.post(
            "/api/projects", json={"name": "Transcript binding lifecycle"}
        ).json()["id"]
        workspace = client.app.state.workspace
        project = workspace.get(project_id)
        seeded = _seed_timestamped_transcript(project)
        action = _queued_transcript_action(seeded, target=target, key=key)

        missing_capability = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json={**action, "capabilities": []},
        )
        assert missing_capability.status_code == 400, missing_capability.text
        refused = ActionResult.model_validate(missing_capability.json())
        assert refused.status == "failed"
        assert refused.errors[0].code == "invalid_params"
        assert not any(
            job.kind == ACTION_RUN_KIND and job.project_id == project_id
            for job in workspace.queue.list_jobs()
        )

        launched_response = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json=action,
        )
        assert launched_response.status_code == 200, launched_response.text
        launched = ActionResult.model_validate(launched_response.json())
        assert launched.status == "queued"
        assert launched.job_id is not None
        assert launched.receipt_id is not None
        assert launched.run_id is None

        queued_response = client.get(
            f"/api/projects/{project_id}/actions/jobs/{launched.job_id}"
        )
        assert queued_response.status_code == 200, queued_response.text
        queued = queued_response.json()
        assert queued["status"] == "queued"
        assert queued["run_id"] is None
        assert queued["action_kind"] == ACTION_KIND
        assert queued["receipt_id"] == launched.receipt_id

        worker = Worker(
            workspace.queue,
            workspace.registry,
            worker_id="transcript-binding-lifecycle-worker",
        )
        assert worker.run_once() is True

        done_response = client.get(
            f"/api/projects/{project_id}/actions/jobs/{launched.job_id}"
        )
        assert done_response.status_code == 200, done_response.text
        done = done_response.json()
        assert done["status"] == "done"
        assert done["run_id"] is None
        assert done["result_summary"] == {
            "status": "completed",
            "action_kind": ACTION_KIND,
            "receipt_id": launched.receipt_id,
        }

        listed_response = client.get(f"/api/projects/{project_id}/actions/jobs")
        assert listed_response.status_code == 200, listed_response.text
        listed = next(
            job
            for job in listed_response.json()["jobs"]
            if job["job_id"] == launched.job_id
        )
        assert listed["status"] == "done"
        assert listed["result_summary"] == done["result_summary"]

        receipt_response = client.get(
            f"/api/projects/{project_id}/actions/v1/receipts/{launched.receipt_id}"
        )
        assert receipt_response.status_code == 200, receipt_response.text
        receipt = receipt_response.json()
        assert receipt["status"] == "completed"
        assert receipt["action_kind"] == ACTION_KIND
        assert receipt["receipt_id"] == launched.receipt_id
        assert receipt["outputs"]

        replay_response = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json=action,
        )
        assert replay_response.status_code == 200, replay_response.text
        replay = ActionResult.model_validate(replay_response.json())
        assert replay.status == "completed"
        assert replay.job_id == launched.job_id
        assert replay.receipt_id == launched.receipt_id
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM sheets WHERE name=?", (target,)
            ).fetchone()[0]
            == 1
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM runs WHERE action_kind=?", (ACTION_KIND,)
            ).fetchone()[0]
            == 0
        )
        assert (
            sum(
                job.kind == ACTION_RUN_KIND and job.project_id == project_id
                for job in workspace.queue.list_jobs()
            )
            == 1
        )


def test_queued_transcript_can_be_cancelled_before_claim(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from frisket.contracts.action import ActionResult
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.server.app import create_app

    with TestClient(
        create_app(tmp_path / "queued-cancel-workspace", run_status_grace_seconds=3600)
    ) as client:
        project_id = client.post(
            "/api/projects", json={"name": "Queued transcript cancel"}
        ).json()["id"]
        project = client.app.state.workspace.get(project_id)
        seeded = _seed_timestamped_transcript(project)
        queued_response = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json=_queued_transcript_action(
                seeded,
                target="Cancelled transcript segments",
                key="queued-transcript-cancel-before-claim@sha256:stable",
            ),
        )
        assert queued_response.status_code == 200, queued_response.text
        queued = ActionResult.model_validate(queued_response.json())
        assert queued.status == "queued"
        assert queued.job_id is not None
        assert queued.receipt_id is not None

        cancelled = client.post(
            f"/api/projects/{project_id}/actions/jobs/{queued.job_id}/cancel"
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        assert client.app.state.workspace.queue.get(queued.job_id).status == "cancelled"
        receipt = ReceiptStore(project).parsed_by_id(queued.receipt_id)
        assert receipt is not None
        assert receipt.status == "cancelled"


def test_claimed_transcript_cancel_cannot_race_its_project_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claimed first-party transaction finishes instead of being miscancelled."""
    import threading
    import time

    from fastapi.testclient import TestClient
    from httpx import Response

    from frisket.contracts.action import ActionResult
    from frisket.engine.executor import table_action
    from frisket.engine.jobs.worker import Worker
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.server.app import create_app

    with TestClient(
        create_app(tmp_path / "running-cancel-workspace", run_status_grace_seconds=3600)
    ) as client:
        project_id = client.post(
            "/api/projects", json={"name": "Running transcript cancel"}
        ).json()["id"]
        workspace = client.app.state.workspace
        project = workspace.get(project_id)
        seeded = _seed_timestamped_transcript(project)
        target = "Claimed transcript segments"
        queued_response = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json=_queued_transcript_action(
                seeded,
                target=target,
                key="claimed-transcript-cancel@sha256:stable",
            ),
        )
        assert queued_response.status_code == 200, queued_response.text
        queued = ActionResult.model_validate(queued_response.json())
        assert queued.status == "queued"
        assert queued.job_id is not None
        assert queued.receipt_id is not None

        entered_transaction = threading.Event()
        release_transaction = threading.Event()
        original_write = table_action._perform_table_in_txn

        def blocked_write(*args, **kwargs):
            entered_transaction.set()
            if not release_transaction.wait(timeout=10):
                raise TimeoutError("test did not release transcript transaction")
            return original_write(*args, **kwargs)

        monkeypatch.setattr(
            table_action,
            "_perform_table_in_txn",
            blocked_write,
        )
        worker_results: list[bool] = []
        worker = Worker(
            workspace.queue,
            workspace.registry,
            worker_id="claimed-transcript-cancel-worker",
        )
        # The real worker thread must hold the Project transaction while the
        # product cancellation request races its queue claim.
        # realtime: joined thread completion, not a clock window, is asserted.
        worker_thread = threading.Thread(
            target=lambda: worker_results.append(worker.run_once())
        )
        cancel_responses: list[Response] = []
        cancel_failures: list[BaseException] = []
        cancel_done = threading.Event()

        def cancel() -> None:
            try:
                cancel_responses.append(
                    client.post(
                        f"/api/projects/{project_id}/actions/jobs/"
                        f"{queued.job_id}/cancel"
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                cancel_failures.append(exc)
            finally:
                cancel_done.set()

        cancel_thread = threading.Thread(  # realtime: see worker race above
            target=cancel
        )
        try:
            worker_thread.start()
            assert entered_transaction.wait(timeout=10)
            assert workspace.queue.get(queued.job_id).status == "running"
            cancel_thread.start()

            deadline = time.monotonic() + 5  # realtime: bounds a stuck rendezvous
            while time.monotonic() < deadline:  # realtime: see deadline above
                if cancel_done.wait(timeout=0.01):
                    break
                if workspace.queue.get(queued.job_id).status == "cancelled":
                    break
            else:  # pragma: no cover - deterministic rendezvous failure
                pytest.fail("cancel neither returned nor changed the queue row")
        finally:
            release_transaction.set()
            cancel_thread.join(timeout=10)
            worker_thread.join(timeout=10)

        assert not cancel_thread.is_alive()
        assert not worker_thread.is_alive()
        assert cancel_failures == []
        assert len(cancel_responses) == 1
        cancelled = cancel_responses[0]
        assert cancelled.status_code == 409, cancelled.text
        assert worker_results == [True]
        assert workspace.queue.get(queued.job_id).status == "done"
        receipt = ReceiptStore(project).parsed_by_id(queued.receipt_id)
        assert receipt is not None
        assert receipt.status == "completed"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM sheets WHERE name=?", (target,)
            ).fetchone()[0]
            == 1
        )


def test_queued_transcript_failure_is_terminalized_by_generic_worker(
    tmp_path: Path,
) -> None:
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.action_jobs import (
        reserve_typed_action_job,
        run_action_run_job,
    )
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "queued-transcript-failure.frisket")
    try:
        seeded = _seed_timestamped_transcript(project)
        action = _queued_transcript_action(
            seeded, target="Should not exist", key="queued-transcript-failure"
        )
        action["params"]["source"] = "missing transcript"
        bound = typed_action_for_request(action)
        envelope = reserve_typed_action_job(project, "project-test", bound)
        from frisket.engine.jobs.worker import HandlerRegistry

        registry = HandlerRegistry()
        register_action_job_binding_handlers(registry)

        result = run_action_run_job(
            project,
            {"action_job": envelope.to_json()},
            executor_lookup=registry.action_executor,
        )

        assert result.status == "failed"
        assert result.errors[0].code == "invalid_input_ref"
        receipt = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
        assert receipt is not None
        assert receipt.status == "failed"
        assert receipt.errors[0].code == "invalid_input_ref"
    finally:
        project.close()


def _insert_transcript_failure_receipt(
    project: Project,
    *,
    receipt_id: str,
    status: str = "running",
    errors: list[ActionError] | None = None,
) -> Receipt:
    from frisket.engine.store.receipts import ReceiptStore

    receipt = Receipt(
        receipt_id=receipt_id,
        project_id="project-transcript-terminalization",
        action_id=f"act_{receipt_id}",
        action_kind=ACTION_KIND,
        idempotency_key=f"{ACTION_KIND}@sha256:{receipt_id}",
        params_hash=f"sha256:{receipt_id}",
        status=status,
        errors=errors or [],
    )
    ReceiptStore(project).insert(receipt)
    return receipt


def test_transcript_failure_cas_loss_never_fabricates_terminal_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.engine.executor.action_reservations import (
        _terminalize_claimless_direct_failure,
    )
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "transcript-failure-cas-loss.frisket")
    receipt_id = "receipt_transcript_failure_cas_loss"
    error = ActionError(
        code="source_failed",
        message="the source transcript failed",
        action_kind=ACTION_KIND,
        details={"source_row_id": 17},
    )
    try:
        _insert_transcript_failure_receipt(project, receipt_id=receipt_id)
        monkeypatch.setattr(
            ReceiptStore,
            "update_body_status",
            lambda *_args, **_kwargs: False,
        )

        result = _terminalize_claimless_direct_failure(
            project,
            project_id="project-transcript-terminalization",
            action_kind=ACTION_KIND,
            stored_receipt=ReceiptStore(project).find_by_id(receipt_id),
            error=error,
            project_write_failed_message="Transcript segments receipt could not be finalized.",
        )

        stored = ReceiptStore(project).parsed_by_id(receipt_id)
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert stored is not None and stored.status == "running"
        assert stored.errors == []
    finally:
        project.close()


def test_transcript_failure_preserves_error_facts_by_value(tmp_path: Path) -> None:
    from frisket.engine.executor.action_reservations import (
        _terminalize_claimless_direct_failure,
    )
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "transcript-failure-error-facts.frisket")
    receipt_id = "receipt_transcript_failure_error_facts"
    error = ActionError(
        code="transcript_provider_declined",
        message="transcript provider rejected the request",
        action_kind=ACTION_KIND,
        details={"provider": "fixture", "billable": False},
    )
    try:
        _insert_transcript_failure_receipt(project, receipt_id=receipt_id)
        result = _terminalize_claimless_direct_failure(
            project,
            project_id="project-transcript-terminalization",
            action_kind=ACTION_KIND,
            stored_receipt=ReceiptStore(project).find_by_id(receipt_id),
            error=error,
            project_write_failed_message="Transcript segments receipt could not be finalized.",
        )
        stored = ReceiptStore(project).parsed_by_id(receipt_id)

        assert result.errors == [error]
        assert stored is not None and stored.errors == [error]
    finally:
        project.close()


@pytest.mark.parametrize("prior_status", ["cancelled", "failed"])
def test_transcript_failure_preserves_prior_terminal_receipt(
    tmp_path: Path,
    prior_status: str,
) -> None:
    from frisket.engine.executor.action_reservations import (
        _terminalize_claimless_direct_failure,
    )
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / f"transcript-prior-{prior_status}.frisket")
    receipt_id = f"receipt_transcript_prior_{prior_status}"
    prior_error = ActionError(
        code="prior_failure",
        message="the prior failure wins",
        action_kind=ACTION_KIND,
        details={"attempt": 1},
    )
    try:
        prior = _insert_transcript_failure_receipt(
            project,
            receipt_id=receipt_id,
            status=prior_status,
            errors=[prior_error] if prior_status == "failed" else None,
        )
        result = _terminalize_claimless_direct_failure(
            project,
            project_id="project-transcript-terminalization",
            action_kind=ACTION_KIND,
            stored_receipt=ReceiptStore(project).find_by_id(receipt_id),
            error=ActionError(
                code="late_failure",
                message="late failure must not rewrite the receipt",
                action_kind=ACTION_KIND,
            ),
            project_write_failed_message="Transcript segments receipt could not be finalized.",
        )
        stored = ReceiptStore(project).parsed_by_id(receipt_id)

        assert result.status == prior_status
        assert result.receipt_id == prior.receipt_id
        assert result.errors == prior.errors
        assert stored == prior
    finally:
        project.close()


def test_queued_transcript_failure_leaves_worker_owned_receipt_untouched(
    tmp_path: Path,
) -> None:
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.action_jobs import (
        reserve_typed_action_job,
        _mark_action_job_receipt_running,
    )
    from frisket.engine.executor.table_action import run_typed_table_action_job
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "queued-transcript-owned.frisket")
    try:
        seeded = _seed_timestamped_transcript(project)
        action = _queued_transcript_action(seeded, target="Missing", key="worker-owned")
        action["params"]["source"] = "missing"
        envelope = reserve_typed_action_job(
            project, "project-test", typed_action_for_request(action)
        )
        _mark_action_job_receipt_running(
            project,
            project_id="project-test",
            receipt_id=envelope.receipt_id,
            job_id=None,
        )
        prior = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
        result = run_typed_table_action_job(project, envelope)
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_input_ref"
        assert ReceiptStore(project).parsed_by_id(envelope.receipt_id) == prior
    finally:
        project.close()
