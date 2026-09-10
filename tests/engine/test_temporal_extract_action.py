from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from frisket.contracts.action import (
    ActionError,
    Receipt,
    ReceiptIO,
)
from frisket.actions.core import RegisteredAction
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.temporal_extract import EXTRACT_RANGE
from frisket.actions.types import ActionRequest
from frisket.engine.executor.temporal_extract_action import (
    run_typed_temporal_extract_action,
)
from frisket.engine.executor.temporal_materialization import (
    CoreTemporalMediaMaterializer,
)
from frisket.engine.executor.temporal_transcripts import resolve_timestamped_transcript
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import resolve_timeline
from frisket.engine.store.media_blobs import (
    MediaBlobStore,
    owned_media_metadata_document,
)
from frisket.engine.store.evidence import (
    TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
    record_evidence_link,
    record_source_span,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results


@dataclass(frozen=True)
class _FakeRenderedClip:
    path: Path
    filename: str = "clip.mp4"
    mime: str = "video/mp4"
    media_kind: str = "video"
    requested_start_ms: int = 2_000
    requested_end_ms: int = 5_000
    resolved_source_start_ms: int = 2_000
    resolved_source_end_ms: int = 5_000
    duration_ms: int = 3_000
    alignment_error_ms: int = 0
    alignment_tolerance_ms: int = 100
    precision: str = "exact"
    renderer_profile: str = "fake.test.v1"
    renderer_params: dict[str, Any] | None = None
    probe: dict[str, Any] | None = None

    def receipt_metadata(self) -> dict[str, Any]:
        return {
            "renderer_profile": self.renderer_profile,
            "renderer_params": self.renderer_params or {},
            "media_kind": self.media_kind,
            "mime": self.mime,
            "filename": self.filename,
            "requested_source_range": {
                "start_ms": self.requested_start_ms,
                "end_ms": self.requested_end_ms,
            },
            "resolved_source_range": {
                "start_ms": self.resolved_source_start_ms,
                "end_ms": self.resolved_source_end_ms,
            },
            "duration_ms": self.duration_ms,
            "alignment_error_ms": 0,
            "alignment_tolerance_ms": 100,
            "precision": self.precision,
            "probe": self.probe or {"duration_ms": self.duration_ms},
        }


def _fake_rendered_clip(
    output_path: Path,
    *,
    start_ms: int,
    end_ms: int,
    media_kind: str = "video",
) -> _FakeRenderedClip:
    return _FakeRenderedClip(
        path=output_path,
        filename="clip.flac" if media_kind == "audio" else "clip.mp4",
        mime="audio/flac" if media_kind == "audio" else "video/mp4",
        media_kind=media_kind,
        requested_start_ms=start_ms,
        requested_end_ms=end_ms,
        resolved_source_start_ms=start_ms,
        resolved_source_end_ms=end_ms,
        duration_ms=end_ms - start_ms,
        renderer_params={},
        probe={"duration_ms": end_ms - start_ms},
    )


def _seed_two_media_rows(project: Project) -> tuple[int, int, list[int], list[str]]:
    sheet_id = project.add_sheet("Media")
    source_column_id = project.add_column(sheet_id, "video", type="video")
    blob_hashes = [
        project.add_blob(
            f"source-video-{index}".encode(),
            filename=f"source-{index}.mp4",
            mime="video/mp4",
            metadata=owned_media_metadata_document(
                probe={"kind": "video", "duration_seconds": 10.0}
            ),
        )
        for index in range(2)
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
            for index, blob_hash in enumerate(blob_hashes)
        ],
        {"video": source_column_id},
    )
    return sheet_id, source_column_id, row_ids, blob_hashes


def _seed_generic_file_source(
    project: Project,
    *,
    blob_mime: str,
) -> tuple[int, int, int, str]:
    sheet_id = project.add_sheet("Media")
    source_column_id = project.add_column(sheet_id, "video", type="audio")
    blob_hash = project.add_blob(
        b"generic-audio-source",
        filename="source.bin",
        mime=blob_mime,
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 10.0, "kind": "audio"}
        ),
    )
    row_id = project.add_rows(
        sheet_id,
        [
            {
                "video": {
                    "blob": blob_hash,
                    "filename": "source.bin",
                    "mime": blob_mime,
                }
            }
        ],
        {"video": source_column_id},
    )[0]
    return sheet_id, source_column_id, row_id, blob_hash


def _mutate_generic_media_classification(
    project: Project,
    blob_hash: str,
    mutation: str,
) -> None:
    if mutation == "media_kind":
        metadata = MediaBlobStore(project).metadata(blob_hash)
        metadata["kind"] = "video"
        project.db.execute(
            "UPDATE blobs SET metadata=? WHERE hash=?",
            (json.dumps(metadata), blob_hash),
        )
    else:
        project.db.execute(
            "UPDATE blobs SET mime='audio/mpeg' WHERE hash=?", (blob_hash,)
        )
    project.db.commit()


def _extract_action(
    *,
    sheet_id: int,
    row_ids: list[int],
    selection: dict[str, Any],
    output_name: str,
    idempotency_key: str,
    repeat_for_rows: bool = False,
    output_names: dict[str, str] | None = None,
) -> tuple[ActionRequest, BoundTypedActionRequest]:
    if repeat_for_rows:
        selection = {**selection, "repeat_for_rows": True}
    request = ActionRequest(
        action_id="temporal.extract_range",
        scope={"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        params={"source": "video", "selection": selection},
        output_names={"clip": output_name, **(output_names or {})},
        idempotency_key=idempotency_key,
    )
    return request, BoundTypedActionRequest.bind(
        RegisteredAction(request.action_id, EXTRACT_RANGE), request
    )


def _run_extract(project, bound, *, project_id, stage_fn=None, **reservation):
    with pytest.MonkeyPatch.context() as patch:
        if stage_fn is not None:
            patch.setattr(
                CoreTemporalMediaMaterializer,
                "stage",
                lambda _self, *args: stage_fn(*args),
            )
        return run_typed_temporal_extract_action(
            project, project_id, bound, **reservation
        )


def test_extract_projects_compatible_typed_annotations_only(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "extract-annotations.frisket")
    try:
        sheet_id, source_column_id, row_ids, _blob_hashes = _seed_two_media_rows(
            project
        )
        row_id = row_ids[0]
        marker_column_id = project.add_column(
            sheet_id, "markers", type="timeline_points"
        )
        notes_column_id = project.add_column(sheet_id, "notes", type="text")
        source = resolve_timeline(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=source_column_id,
        )
        project.apply_edits(
            [
                {
                    "row_id": row_id,
                    "column_id": marker_column_id,
                    "value": {
                        "schema_version": "frisket.timeline_points.v1",
                        "timeline": source.anchor.wire_value(),
                        "items": [
                            {
                                "id": "inside",
                                "at_ms": 3_000,
                                "label": "Keep",
                                "metadata": {"source": "csv"},
                            },
                            {"id": "outside", "at_ms": 7_000},
                        ],
                    },
                },
                {
                    "row_id": row_id,
                    "column_id": notes_column_id,
                    "value": "must not be copied",
                },
            ],
            label="seed annotations",
        )
        action, bound = _extract_action(
            sheet_id=sheet_id,
            row_ids=[row_id],
            selection={"kind": "draft_range", "start_ms": 2_000, "end_ms": 5_000},
            output_name="clip",
            idempotency_key="extract-annotations",
        )

        def fake_stage(_project, source_plan, temporal_range, output_path):
            output_path.write_bytes(b"derived-video")
            return _fake_rendered_clip(
                output_path,
                start_ms=temporal_range.start_ms,
                end_ms=temporal_range.end_ms,
                media_kind=source_plan.lease.media_kind,
            )

        result = _run_extract(
            project,
            bound,
            project_id="project-extract-annotations",
            stage_fn=fake_stage,
        )

        assert result.status == "completed"
        assert {output.name for output in result.outputs} == {"clip", "clip_markers"}
        marker_output = next(
            output for output in result.outputs if output.name == "clip_markers"
        )
        projected = project.get_values(
            sheet_id,
            marker_output.column_id,
            row_ids=[row_id],
        )[row_id]
        assert projected["timeline"]["duration_ms"] == 3_000
        assert projected["items"] == [
            {
                "id": "inside",
                "at_ms": 1_000,
                "label": "Keep",
                "metadata": {"source": "csv"},
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
    finally:
        project.close()


def test_extract_range_stages_then_atomically_publishes_lineage_and_replays(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "extract.frisket", name="Extract")
    try:
        sheet_id = project.add_sheet("Media")
        source_column_id = project.add_column(sheet_id, "video", type="video")
        source_hash = project.add_blob(
            b"source-video",
            filename="source.mp4",
            mime="video/mp4",
            metadata=owned_media_metadata_document(
                probe={"kind": "video", "duration_seconds": 10.0}
            ),
        )
        row_id = project.add_rows(
            sheet_id,
            [
                {
                    "video": {
                        "blob": source_hash,
                        "filename": "source.mp4",
                        "mime": "video/mp4",
                    }
                }
            ],
            {"video": source_column_id},
        )[0]
        action, bound = _extract_action(
            sheet_id=sheet_id,
            row_ids=[row_id],
            selection={"kind": "draft_range", "start_ms": 2000, "end_ms": 5000},
            output_name="clip",
            idempotency_key="extract-range-test@sha256:v1",
        )
        stage_calls: list[tuple[int, int]] = []

        def fake_stage(project_arg, source, temporal_range, output_path):
            assert project_arg is project
            assert project.db.in_transaction is False
            stage_calls.append((temporal_range.start_ms, temporal_range.end_ms))
            output_path.write_bytes(b"derived-video")
            return _FakeRenderedClip(
                path=output_path,
                renderer_params={},
                probe={"duration_ms": 3_000},
            )

        result = _run_extract(
            project,
            bound,
            project_id="project-extract",
            stage_fn=fake_stage,
        )
        assert result.status == "completed"
        assert stage_calls == [(2_000, 5_000)]
        assert len(result.op_ids) == 1
        output = result.outputs[0]
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert receipt is not None and receipt.status == "completed"
        selection = next(
            item.ref for item in receipt.inputs if item.name == "selection"
        )
        assert selection["value"]["item"]["start_ms"] == 2_000
        assert selection["value"]["item"]["end_ms"] == 5_000
        lineage = next(
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "derived_temporal_artifact"
        )
        values = project.get_values(sheet_id, output.column_id, row_ids=[row_id])
        assert values[row_id]["blob"] == lineage["blob_hash"]
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM blob_derivations WHERE source_hash=?",
                (source_hash,),
            ).fetchone()[0]
            == 1
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM artifact_timeline_segments WHERE receipt_id=?",
                (result.receipt_id,),
            ).fetchone()[0]
            == 1
        )
        assert receipt.outputs[0].ref["kind"] == "materialized_column"

        replay = _run_extract(
            project,
            bound,
            project_id="project-extract",
            stage_fn=fake_stage,
        )
        assert replay.status == "completed", replay.model_dump(mode="json")
        assert replay.receipt_id == result.receipt_id
        assert len(replay.outputs) == 1
        replay_output = replay.outputs[0]
        assert replay_output.kind == "column"
        assert replay_output.name == "clip"
        assert replay_output.sheet_id == sheet_id
        assert replay_output.column_id == output.column_id
        assert replay_output.row_ids == [row_id]
        assert replay_output.ref == output.ref
        assert stage_calls == [(2_000, 5_000)]
    finally:
        project.close()


def test_extract_range_reports_manual_draft_beyond_source_duration(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "extract-out-of-bounds.frisket")
    try:
        sheet_id, _source_column_id, row_ids, _blob_hashes = _seed_two_media_rows(
            project
        )
        action, bound = _extract_action(
            sheet_id=sheet_id,
            row_ids=[row_ids[0]],
            selection={"kind": "draft_range", "start_ms": 9_000, "end_ms": 12_000},
            output_name="clip",
            idempotency_key="extract-out-of-bounds@sha256:v1",
        )
        stage_called = False

        def fake_stage(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal stage_called
            stage_called = True
            raise AssertionError("out-of-bounds Extract must fail before staging")

        result = _run_extract(
            project,
            bound,
            project_id="project-extract-out-of-bounds",
            stage_fn=fake_stage,
        )

        assert result.status == "failed"
        assert result.errors[0].code == "range_out_of_bounds"
        assert "outside its timeline duration" in result.errors[0].message
        assert stage_called is False
    finally:
        project.close()


def test_queued_extract_failure_uses_ordinary_action_run_terminalization(
    tmp_path: Path,
) -> None:
    from frisket.engine.executor.action_jobs import (
        ActionJobEnvelope,
        reserve_typed_action_job,
        run_action_run_job,
    )
    from frisket.engine.jobs.worker import (
        HandlerRegistry,
        register_action_job_binding_handlers,
    )

    registry = HandlerRegistry()
    register_action_job_binding_handlers(registry)
    project = Project.create(tmp_path / "queued-extract-failure.frisket")
    try:
        sheet_id, _source_column_id, row_ids, _blob_hashes = _seed_two_media_rows(
            project
        )
        action, bound = _extract_action(
            sheet_id=sheet_id,
            row_ids=[row_ids[0]],
            selection={"kind": "draft_range", "start_ms": 9_000, "end_ms": 11_000},
            output_name="clip",
            idempotency_key="queued-extract-failure@sha256:v1",
            repeat_for_rows=True,
        )
        envelope = reserve_typed_action_job(
            project, "project-queued-extract-failure", bound
        )
        assert isinstance(envelope, ActionJobEnvelope)

        result = run_action_run_job(
            project,
            {"action_job": envelope.to_json()},
            executor_lookup=registry.action_executor,
        )

        assert result.status == "failed"
        assert result.errors[0].code == "range_out_of_bounds"
        stored = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
        assert stored is not None
        assert stored.status == "failed"
        assert stored.errors[0].code == "range_out_of_bounds"
    finally:
        project.close()


def test_extract_range_has_one_canonical_action_job_registration_owner() -> None:
    import frisket.engine.jobs as jobs

    from frisket.engine.executor.action_bindings import action_job_bindings
    from frisket.engine.executor.temporal_extract_action import (
        run_typed_temporal_extract_job,
    )
    from frisket.engine.jobs.worker import (
        HandlerRegistry,
        register_action_job_binding_handlers,
    )

    registry = HandlerRegistry()
    register_action_job_binding_handlers(registry)

    executor = action_job_bindings()["temporal.extract_range"]
    assert executor is run_typed_temporal_extract_job
    assert registry.action_executor("temporal.extract_range") is executor
    assert not hasattr(jobs, "TEMPORAL_ACTION_JOB_KINDS")
    assert not hasattr(jobs, "register_temporal_action_job_handlers")
    jobs_root = Path(__file__).resolve().parents[2] / "src/frisket/engine/jobs"
    assert not (jobs_root / "temporal.py").exists()
    for path in (jobs_root / "__init__.py", jobs_root / "worker.py"):
        # rule19: legacy-registration deletion is a source composition contract.
        source = path.read_text(encoding="utf-8")
        assert "register_temporal_action_job_handlers" not in source
        assert "TEMPORAL_ACTION_JOB_KINDS" not in source


def test_extract_range_binding_runs_through_http_queue_worker_and_exact_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from frisket.contracts.action import ActionResult
    from frisket.engine.executor.temporal_materialization import (
        CoreTemporalMediaMaterializer,
    )
    from frisket.engine.jobs.queue import ACTION_RUN_KIND
    from frisket.engine.jobs.worker import Worker
    from frisket.server.app import create_app

    stage_calls: list[tuple[int, int, int]] = []

    def fake_stage(_self, project, source, temporal_range, output_path):
        assert not project.db.in_transaction
        stage_calls.append(
            (source.row_id, temporal_range.start_ms, temporal_range.end_ms)
        )
        output_path.write_bytes(b"http-worker-derived-clip")
        return _fake_rendered_clip(
            output_path,
            start_ms=temporal_range.start_ms,
            end_ms=temporal_range.end_ms,
        )

    monkeypatch.setattr(CoreTemporalMediaMaterializer, "stage", fake_stage)
    with TestClient(
        create_app(
            tmp_path / "extract-binding-lifecycle", run_status_grace_seconds=3600
        )
    ) as client:
        project_id = client.post(
            "/api/projects", json={"name": "Extract binding lifecycle"}
        ).json()["id"]
        workspace = client.app.state.workspace
        project = workspace.get(project_id)
        sheet_id, _source_column_id, row_ids, _blob_hashes = _seed_two_media_rows(
            project
        )
        action, _bound = _extract_action(
            sheet_id=sheet_id,
            row_ids=[row_ids[0]],
            selection={"kind": "draft_range", "start_ms": 2_000, "end_ms": 5_000},
            output_name="clips",
            idempotency_key="extract-binding-lifecycle@sha256:stable",
        )
        wire = action.model_dump(mode="json", exclude_none=True)

        # Capabilities are host-owned now; the typed request refuses a caller's
        # attempted capability declaration before queuing any work.
        client_capabilities = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json={**wire, "capabilities": []},
        )
        assert client_capabilities.status_code == 400, client_capabilities.text
        refused = ActionResult.model_validate(client_capabilities.json())
        assert refused.status == "failed"
        assert refused.errors[0].code == "invalid_params"
        assert not any(
            job.kind == ACTION_RUN_KIND and job.project_id == project_id
            for job in workspace.queue.list_jobs()
        )

        launched_response = client.post(
            f"/api/projects/{project_id}/actions/v1/run", json=wire
        )
        assert launched_response.status_code == 200, launched_response.text
        launched = ActionResult.model_validate(launched_response.json())
        assert launched.status == "queued"
        assert launched.job_id is not None
        assert launched.receipt_id is not None
        assert launched.run_id is None

        queued = client.get(
            f"/api/projects/{project_id}/actions/jobs/{launched.job_id}"
        ).json()
        assert queued["status"] == "queued"
        assert queued["action_kind"] == "temporal.extract_range"
        assert queued["receipt_id"] == launched.receipt_id
        assert queued["run_id"] is None

        worker = Worker(
            workspace.queue,
            workspace.registry,
            worker_id="extract-binding-lifecycle-worker",
        )
        assert worker.run_once() is True

        done = client.get(
            f"/api/projects/{project_id}/actions/jobs/{launched.job_id}"
        ).json()
        assert done["status"] == "done"
        assert done["run_id"] is None
        assert done["result_summary"] == {
            "status": "completed",
            "action_kind": "temporal.extract_range",
            "receipt_id": launched.receipt_id,
        }
        receipt = client.get(
            f"/api/projects/{project_id}/actions/v1/receipts/{launched.receipt_id}"
        ).json()
        assert receipt["status"] == "completed"
        assert receipt["action_kind"] == "temporal.extract_range"
        assert receipt["outputs"]
        assert stage_calls == [(row_ids[0], 2_000, 5_000)]

        replay_response = client.post(
            f"/api/projects/{project_id}/actions/v1/run", json=wire
        )
        assert replay_response.status_code == 200, replay_response.text
        replay = ActionResult.model_validate(replay_response.json())
        assert replay.status == "completed"
        assert replay.job_id == launched.job_id
        assert replay.receipt_id == launched.receipt_id
        assert stage_calls == [(row_ids[0], 2_000, 5_000)]
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM columns WHERE sheet_id=? AND name='clips'",
                (sheet_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            sum(
                job.kind == ACTION_RUN_KIND and job.project_id == project_id
                for job in workspace.queue.list_jobs()
            )
            == 1
        )

        cancelled_action, _cancelled_bound = _extract_action(
            sheet_id=sheet_id,
            row_ids=[row_ids[1]],
            selection={"kind": "draft_range", "start_ms": 1_000, "end_ms": 3_000},
            output_name="cancelled_clips",
            idempotency_key="extract-cancel-before-claim@sha256:stable",
        )
        cancelled_launch = ActionResult.model_validate(
            client.post(
                f"/api/projects/{project_id}/actions/v1/run",
                json=cancelled_action.model_dump(mode="json", exclude_none=True),
            ).json()
        )
        assert cancelled_launch.status == "queued"
        cancelled = client.post(
            f"/api/projects/{project_id}/actions/jobs/{cancelled_launch.job_id}/cancel"
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        cancelled_receipt = ReceiptStore(project).parsed_by_id(
            str(cancelled_launch.receipt_id)
        )
        assert cancelled_receipt is not None
        assert cancelled_receipt.status == "cancelled"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM columns "
                "WHERE sheet_id=? AND name='cancelled_clips'",
                (sheet_id,),
            ).fetchone()[0]
            == 0
        )


def test_claimed_extract_cancel_finishes_its_deterministic_project_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import time

    from fastapi.testclient import TestClient
    from httpx import Response

    from frisket.contracts.action import ActionResult
    from frisket.engine.executor import temporal_extract_action as temporal_module
    from frisket.engine.executor.temporal_materialization import (
        CoreTemporalMediaMaterializer,
    )
    from frisket.engine.jobs.worker import Worker
    from frisket.server.app import create_app

    def fake_stage(_self, project, source, temporal_range, output_path):
        assert not project.db.in_transaction
        output_path.write_bytes(b"claimed-extract-derived-clip")
        return _fake_rendered_clip(
            output_path,
            start_ms=temporal_range.start_ms,
            end_ms=temporal_range.end_ms,
        )

    monkeypatch.setattr(CoreTemporalMediaMaterializer, "stage", fake_stage)
    with TestClient(
        create_app(tmp_path / "claimed-extract-cancel", run_status_grace_seconds=3600)
    ) as client:
        project_id = client.post(
            "/api/projects", json={"name": "Claimed extract cancellation"}
        ).json()["id"]
        workspace = client.app.state.workspace
        project = workspace.get(project_id)
        sheet_id, _source_column_id, row_ids, _blob_hashes = _seed_two_media_rows(
            project
        )
        action, _bound = _extract_action(
            sheet_id=sheet_id,
            row_ids=[row_ids[0]],
            selection={"kind": "draft_range", "start_ms": 2_000, "end_ms": 5_000},
            output_name="claimed_clips",
            idempotency_key="claimed-extract-cancel@sha256:stable",
        )
        queued_response = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json=action.model_dump(mode="json", exclude_none=True),
        )
        assert queued_response.status_code == 200, queued_response.text
        queued = ActionResult.model_validate(queued_response.json())
        assert queued.status == "queued"
        assert queued.job_id is not None
        assert queued.receipt_id is not None

        entered_transaction = threading.Event()
        release_transaction = threading.Event()
        original_write = temporal_module._write_extract_in_transaction

        def blocked_write(*args, **kwargs):
            worker_project = args[0]
            assert worker_project.db.in_transaction
            entered_transaction.set()
            if not release_transaction.wait(timeout=10):  # realtime: thread rendezvous
                raise TimeoutError("test did not release extract transaction")
            return original_write(*args, **kwargs)

        monkeypatch.setattr(
            temporal_module,
            "_write_extract_in_transaction",
            blocked_write,
        )
        worker_results: list[bool] = []
        worker = Worker(
            workspace.queue,
            workspace.registry,
            worker_id="claimed-extract-cancel-worker",
        )
        worker_thread = threading.Thread(  # realtime: executed transaction race
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

        cancel_thread = threading.Thread(target=cancel)  # realtime: race participant
        try:
            worker_thread.start()
            assert entered_transaction.wait(timeout=10)  # realtime: bounded rendezvous
            assert workspace.queue.get(queued.job_id).status == "running"
            cancel_thread.start()
            deadline = time.monotonic() + 5  # realtime: bounds a stuck rendezvous
            while time.monotonic() < deadline:  # realtime: see deadline above
                if cancel_done.wait(timeout=0.01):  # realtime: responsive poll
                    break
                if workspace.queue.get(queued.job_id).status == "cancelled":
                    break
            else:  # pragma: no cover - deterministic rendezvous failure
                pytest.fail("cancel neither returned nor changed the queue row")
        finally:
            release_transaction.set()
            if cancel_thread.ident is not None:
                cancel_thread.join(timeout=10)  # realtime: bounded cleanup
            worker_thread.join(timeout=10)  # realtime: bounded cleanup

        assert not cancel_thread.is_alive()
        assert not worker_thread.is_alive()
        assert cancel_failures == []
        assert len(cancel_responses) == 1
        assert cancel_responses[0].status_code == 409
        assert worker_results == [True]
        assert workspace.queue.get(queued.job_id).status == "done"
        receipt = ReceiptStore(project).parsed_by_id(queued.receipt_id)
        assert receipt is not None and receipt.status == "completed"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM columns "
                "WHERE sheet_id=? AND name='claimed_clips'",
                (sheet_id,),
            ).fetchone()[0]
            == 1
        )


def _insert_extract_failure_receipt(
    project: Project,
    *,
    receipt_id: str,
    status: str = "running",
    errors: list[ActionError] | None = None,
    queued: bool = False,
) -> Receipt:
    receipt = Receipt(
        receipt_id=receipt_id,
        project_id="project-extract-terminalization",
        action_id=f"act_{receipt_id}",
        action_kind="temporal.extract_range",
        idempotency_key=f"temporal.extract_range@sha256:{receipt_id}",
        params_hash=f"sha256:{receipt_id}",
        status=status,
        inputs=(
            [
                ReceiptIO(
                    name="execution",
                    ref={"kind": "queued_action_job", "queue_kind": "action.run"},
                )
            ]
            if queued
            else []
        ),
        errors=errors or [],
    )
    ReceiptStore(project).insert(receipt)
    return receipt


def test_extract_failure_preserves_error_facts_by_value(tmp_path: Path) -> None:
    from frisket.engine.executor.temporal_extract_publication import (
        _terminalize_extract_failure,
    )

    project = Project.create(tmp_path / "extract-failure-error-facts.frisket")
    receipt_id = "receipt_extract_failure_error_facts"
    error = ActionError(
        code="extract_provider_declined",
        message="extract provider rejected the request",
        action_kind="temporal.extract_range",
        details={"provider": "fixture", "billable": False},
    )
    try:
        _insert_extract_failure_receipt(project, receipt_id=receipt_id)
        result = _terminalize_extract_failure(
            project,
            project_id="project-extract-terminalization",
            receipt_id=receipt_id,
            error=error,
        )
        stored = ReceiptStore(project).parsed_by_id(receipt_id)

        assert result.errors == [error]
        assert stored is not None and stored.errors == [error]
    finally:
        project.close()


@pytest.mark.parametrize("prior_status", ["cancelled", "failed"])
def test_extract_failure_preserves_prior_terminal_receipt(
    tmp_path: Path,
    prior_status: str,
) -> None:
    from frisket.engine.executor.temporal_extract_publication import (
        _terminalize_extract_failure,
    )

    project = Project.create(tmp_path / f"extract-prior-{prior_status}.frisket")
    receipt_id = f"receipt_extract_prior_{prior_status}"
    prior_error = ActionError(
        code="prior_failure",
        message="the prior failure wins",
        action_kind="temporal.extract_range",
        details={"attempt": 1},
    )
    try:
        prior = _insert_extract_failure_receipt(
            project,
            receipt_id=receipt_id,
            status=prior_status,
            errors=[prior_error] if prior_status == "failed" else None,
        )
        result = _terminalize_extract_failure(
            project,
            project_id="project-extract-terminalization",
            receipt_id=receipt_id,
            error=ActionError(
                code="late_failure",
                message="late failure must not rewrite the receipt",
                action_kind="temporal.extract_range",
            ),
        )
        stored = ReceiptStore(project).parsed_by_id(receipt_id)

        assert result.status == prior_status
        assert result.receipt_id == prior.receipt_id
        assert result.errors == prior.errors
        assert stored == prior
    finally:
        project.close()


def test_queued_extract_failure_leaves_worker_owned_receipt_untouched(
    tmp_path: Path,
) -> None:
    from frisket.engine.executor.temporal_extract_publication import (
        _terminalize_extract_failure,
    )

    project = Project.create(tmp_path / "queued-extract-owned-receipt.frisket")
    receipt_id = "receipt_queued_extract_owned_by_worker"
    error = ActionError(
        code="queued_extract_failure",
        message="the generic worker owns terminalization",
        action_kind="temporal.extract_range",
        details={"owner": "action.run"},
    )
    try:
        prior = _insert_extract_failure_receipt(
            project,
            receipt_id=receipt_id,
            queued=True,
        )
        result = _terminalize_extract_failure(
            project,
            project_id="project-extract-terminalization",
            receipt_id=receipt_id,
            error=error,
        )
        stored = ReceiptStore(project).parsed_by_id(receipt_id)

        assert result.errors == [error]
        assert stored == prior
    finally:
        project.close()


def test_extract_range_maps_one_acknowledged_literal_over_multiple_rows(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "extract-batch-literal.frisket")
    try:
        sheet_id, _source_column_id, row_ids, blob_hashes = _seed_two_media_rows(
            project
        )
        action, bound = _extract_action(
            sheet_id=sheet_id,
            row_ids=row_ids,
            selection={"kind": "draft_range", "start_ms": 2_000, "end_ms": 5_000},
            output_name="clips",
            idempotency_key="extract-batch-literal@sha256:v1",
            repeat_for_rows=True,
        )
        stage_calls: list[tuple[int, int, int]] = []

        def fake_stage(project_arg, source, temporal_range, output_path):
            assert project_arg is project
            assert not project.db.in_transaction
            stage_calls.append(
                (source.row_id, temporal_range.start_ms, temporal_range.end_ms)
            )
            output_path.write_bytes(f"derived-{source.row_id}".encode())
            return _fake_rendered_clip(
                output_path,
                start_ms=temporal_range.start_ms,
                end_ms=temporal_range.end_ms,
            )

        result = _run_extract(
            project,
            bound,
            project_id="project-extract-batch-literal",
            stage_fn=fake_stage,
        )

        assert result.status == "completed", result.model_dump(mode="json")
        assert stage_calls == [
            (row_ids[0], 2_000, 5_000),
            (row_ids[1], 2_000, 5_000),
        ]
        assert len(result.outputs) == 1
        output = result.outputs[0]
        assert output.name == "clips"
        assert output.row_ids == row_ids
        assert output.ref["kind"] == "materialized_column"
        assert output.ref["row_ids"] == row_ids
        values = project.get_values(sheet_id, int(output.column_id), row_ids=row_ids)
        assert all(values[row_id]["blob"] for row_id in row_ids)
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM blob_derivations WHERE source_hash IN (?, ?)",
                blob_hashes,
            ).fetchone()[0]
            == 2
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM artifact_timeline_segments WHERE receipt_id=?",
                (result.receipt_id,),
            ).fetchone()[0]
            == 2
        )
        receipt = ReceiptStore(project).parsed_by_id(str(result.receipt_id))
        assert receipt is not None
        assert {item.name for item in receipt.inputs} == {
            f"source.{row_ids[0]}",
            f"selection.{row_ids[0]}",
            f"source.{row_ids[1]}",
            f"selection.{row_ids[1]}",
        }
        summary = next(
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "temporal_extract_summary"
        )
        assert summary["source_count"] == 2
        assert summary["output_count"] == 2

        replay = _run_extract(
            project,
            bound,
            project_id="project-extract-batch-literal",
            stage_fn=fake_stage,
        )
        assert replay.status == "completed"
        assert replay.receipt_id == result.receipt_id
        assert replay.outputs[0].row_ids == row_ids
        assert len(stage_calls) == 2

        reserved_replay = _run_extract(
            project,
            bound,
            project_id="project-extract-batch-literal",
            reserved_action_id=result.action.action_id,
            reserved_receipt_id=result.receipt_id,
            stage_fn=lambda *_args: pytest.fail(
                "terminal reserved replay must not stage"
            ),
        )
        assert reserved_replay.status == "completed"
        mismatched_reservation = _run_extract(
            project,
            bound,
            project_id="project-extract-batch-literal",
            reserved_action_id="act_wrong",
            reserved_receipt_id=result.receipt_id,
            stage_fn=lambda *_args: pytest.fail(
                "mismatched reserved replay must not stage"
            ),
        )
        assert mismatched_reservation.status == "failed"
        assert mismatched_reservation.errors[0].code == "idempotency_conflict"
    finally:
        project.close()


def test_extract_range_uses_each_rows_own_range_column_value(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "extract-batch-column.frisket")
    try:
        sheet_id, source_column_id, row_ids, _blob_hashes = _seed_two_media_rows(
            project
        )
        range_column_id = project.add_column(
            sheet_id, "reviewed_range", type="timeline_range"
        )
        ranges = [(1_000, 3_000), (4_000, 7_000)]
        edits = []
        for index, (row_id, (start_ms, end_ms)) in enumerate(
            zip(row_ids, ranges, strict=True)
        ):
            lease = resolve_timeline(
                project,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=source_column_id,
            )
            edits.append(
                {
                    "row_id": row_id,
                    "column_id": range_column_id,
                    "value": {
                        "schema_version": "frisket.timeline_range.v1",
                        "timeline": lease.anchor.wire_value(),
                        "item": {
                            "id": f"range-{index}",
                            "start_ms": start_ms,
                            "end_ms": end_ms,
                            "metadata": {},
                        },
                    },
                }
            )
        project.apply_edits(edits)
        action, bound = _extract_action(
            sheet_id=sheet_id,
            row_ids=row_ids,
            selection={"kind": "column", "column": "reviewed_range"},
            output_name="excerpt",
            idempotency_key="extract-batch-column@sha256:v1",
        )
        stage_calls: list[tuple[int, int, int]] = []

        def fake_stage(_project, source, temporal_range, output_path):
            stage_calls.append(
                (source.row_id, temporal_range.start_ms, temporal_range.end_ms)
            )
            output_path.write_bytes(f"column-derived-{source.row_id}".encode())
            return _fake_rendered_clip(
                output_path,
                start_ms=temporal_range.start_ms,
                end_ms=temporal_range.end_ms,
            )

        result = _run_extract(
            project,
            bound,
            project_id="project-extract-batch-column",
            stage_fn=fake_stage,
        )

        assert result.status == "completed", result.model_dump(mode="json")
        assert stage_calls == [
            (row_ids[0], 1_000, 3_000),
            (row_ids[1], 4_000, 7_000),
        ]
        receipt = ReceiptStore(project).parsed_by_id(str(result.receipt_id))
        assert receipt is not None
        selections = [
            item.ref["value"]["item"]
            for item in receipt.inputs
            if item.name.startswith("selection.")
        ]
        assert [
            (
                item["start_ms"],
                item["end_ms"],
            )
            for item in selections
        ] == ranges
    finally:
        project.close()


def test_extract_range_mid_stage_failure_publishes_no_partial_outputs(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "extract-batch-failure.frisket")
    try:
        sheet_id, _source_column_id, row_ids, _blob_hashes = _seed_two_media_rows(
            project
        )
        action, bound = _extract_action(
            sheet_id=sheet_id,
            row_ids=row_ids,
            selection={"kind": "draft_range", "start_ms": 2_000, "end_ms": 5_000},
            output_name="clips",
            idempotency_key="extract-batch-failure@sha256:v1",
            repeat_for_rows=True,
        )
        initial_blob_count = project.db.execute(
            "SELECT COUNT(*) FROM blobs"
        ).fetchone()[0]
        stage_calls = 0

        def fail_second_stage(_project, source, temporal_range, output_path):
            nonlocal stage_calls
            stage_calls += 1
            if stage_calls == 2:
                raise RuntimeError("renderer failed after first staged clip")
            output_path.write_bytes(f"staged-{source.row_id}".encode())
            return _fake_rendered_clip(
                output_path,
                start_ms=temporal_range.start_ms,
                end_ms=temporal_range.end_ms,
            )

        result = _run_extract(
            project,
            bound,
            project_id="project-extract-batch-failure",
            stage_fn=fail_second_stage,
        )

        assert result.status == "failed"
        assert stage_calls == 2
        assert (
            project.db.execute(
                "SELECT 1 FROM columns WHERE sheet_id=? AND name='clips'",
                (sheet_id,),
            ).fetchone()
            is None
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM blob_derivations").fetchone()[0]
            == 0
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
            == initial_blob_count
        )
        assert result.receipt_id is not None
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert receipt is not None and receipt.status == "failed"
    finally:
        project.close()


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [("source", "stale_input"), ("collision", "invalid_input_ref")],
)
def test_extract_range_revalidates_source_and_collision_after_staging(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    project = Project.create(tmp_path / f"extract-post-stage-{mutation}.frisket")
    try:
        sheet_id, source_column_id, row_ids, _blob_hashes = _seed_two_media_rows(
            project
        )
        row_id = row_ids[0]
        action, bound = _extract_action(
            sheet_id=sheet_id,
            row_ids=[row_id],
            selection={"kind": "draft_range", "start_ms": 2_000, "end_ms": 5_000},
            output_name="clips",
            idempotency_key=f"extract-post-stage-{mutation}@sha256:v1",
        )

        def mutate_after_stage(project_arg, source, temporal_range, output_path):
            assert not project.db.in_transaction
            output_path.write_bytes(b"staged-before-race")
            rendered = _fake_rendered_clip(
                output_path,
                start_ms=temporal_range.start_ms,
                end_ms=temporal_range.end_ms,
            )
            if mutation == "collision":
                project_arg.add_column(sheet_id, "clips", type="video")
            else:
                replacement_hash = project_arg.add_blob(
                    b"source-changed-after-stage",
                    filename="changed.mp4",
                    mime="video/mp4",
                    metadata=owned_media_metadata_document(
                        probe={"kind": "video", "duration_seconds": 10.0}
                    ),
                )
                project_arg.apply_edits(
                    [
                        {
                            "row_id": row_id,
                            "column_id": source_column_id,
                            "value": {
                                "blob": replacement_hash,
                                "filename": "changed.mp4",
                                "mime": "video/mp4",
                            },
                        }
                    ],
                    label="race source mutation",
                )
            return rendered

        result = _run_extract(
            project,
            bound,
            project_id=f"project-extract-post-stage-{mutation}",
            stage_fn=mutate_after_stage,
        )

        assert result.status == "failed"
        assert result.errors[0].code == expected_code
        assert (
            project.db.execute("SELECT COUNT(*) FROM blob_derivations").fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM artifact_timeline_segments WHERE receipt_id=?",
                (result.receipt_id,),
            ).fetchone()[0]
            == 0
        )
        output_columns = project.db.execute(
            "SELECT COUNT(*) FROM columns WHERE sheet_id=? AND name='clips'",
            (sheet_id,),
        ).fetchone()[0]
        assert output_columns == (1 if mutation == "collision" else 0)
    finally:
        project.close()


def test_extract_range_publication_failure_rolls_back_every_project_output_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project.create(tmp_path / "extract-publication-rollback.frisket")
    try:
        sheet_id, _source_column_id, row_ids, _blob_hashes = _seed_two_media_rows(
            project
        )
        initial_blob_rows = project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[
            0
        ]
        action, bound = _extract_action(
            sheet_id=sheet_id,
            row_ids=[row_ids[0]],
            selection={"kind": "draft_range", "start_ms": 2_000, "end_ms": 5_000},
            output_name="clips",
            idempotency_key="extract-publication-rollback@sha256:v1",
        )
        original_update = ReceiptStore.update_body_status
        completed_update_attempts = 0

        def fail_completed_publication(self, receipt, **kwargs):
            nonlocal completed_update_attempts
            if receipt.status == "completed" and completed_update_attempts == 0:
                completed_update_attempts += 1
                raise RuntimeError("inject failure after complete output publication")
            return original_update(self, receipt, **kwargs)

        monkeypatch.setattr(
            ReceiptStore,
            "update_body_status",
            fail_completed_publication,
        )

        def fake_stage(_project, source, temporal_range, output_path):
            assert not project.db.in_transaction
            output_path.write_bytes(b"derived-before-publication-rollback")
            return _fake_rendered_clip(
                output_path,
                start_ms=temporal_range.start_ms,
                end_ms=temporal_range.end_ms,
            )

        result = _run_extract(
            project,
            bound,
            project_id="project-extract-publication-rollback",
            stage_fn=fake_stage,
        )

        assert completed_update_attempts == 1
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM columns WHERE sheet_id=? AND name='clips'",
                (sheet_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
            == initial_blob_rows
        )
        # Immutable object-file residue is deliberately outside DB atomicity;
        # the existing restore/object-GC policy owns it.
        assert (
            project.db.execute("SELECT COUNT(*) FROM blob_derivations").fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM artifact_timeline_segments WHERE receipt_id=?",
                (result.receipt_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM evidence_links WHERE receipt_id=?",
                (result.receipt_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM ops WHERE kind='temporal.extract_range'"
            ).fetchone()[0]
            == 0
        )
        stored = ReceiptStore(project).parsed_by_id(str(result.receipt_id))
        assert stored is not None and stored.status == "failed"
        assert stored.outputs == []
    finally:
        project.close()


def test_extract_range_rejects_output_collision_before_staging(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "extract-output-collision.frisket")
    try:
        sheet_id, _source_column_id, row_ids, _blob_hashes = _seed_two_media_rows(
            project
        )
        project.add_column(sheet_id, "clips", type="video")
        action, bound = _extract_action(
            sheet_id=sheet_id,
            row_ids=row_ids,
            selection={"kind": "draft_range", "start_ms": 2_000, "end_ms": 5_000},
            output_name="clips",
            idempotency_key="extract-output-collision@sha256:v1",
            repeat_for_rows=True,
        )

        result = _run_extract(
            project,
            bound,
            project_id="project-extract-output-collision",
            stage_fn=lambda *_args: pytest.fail("collision must fail before staging"),
        )

        assert result.status == "failed"
        assert result.errors[0].code == "invalid_input_ref"
        assert result.receipt_id is None
    finally:
        project.close()


def test_extract_batch_projects_every_compatible_current_transcript(
    tmp_path: Path,
) -> None:
    from frisket.engine.executor.action_jobs import reserve_typed_action_job

    project = Project.create(tmp_path / "extract-batch-transcripts.frisket")
    try:
        sheet_id, source_column_id, row_ids, _blob_hashes = _seed_two_media_rows(
            project
        )
        transcript_column_id = project.add_column(
            sheet_id,
            "transcript",
            type="timestamped_transcript",
            ai_generated=True,
        )
        alternate_column_id = project.add_column(
            sheet_id,
            "alternate_transcript",
            type="timestamped_transcript",
            ai_generated=True,
        )
        runs = RunResultStore(project)
        transcript_op_id = project.append_op(
            "media.transcribe",
            {"kind": "media.transcribe", "params": {"output_name": "transcript"}},
            label="transcribe transcript",
        )
        transcript_run_id = runs.start_run(
            transcript_op_id,
            sheet_id,
            "media.transcribe",
            params={"output_name": "transcript"},
            total_rows=2,
            row_ids=row_ids,
        )
        write_claimed_test_results(
            project,
            transcript_run_id,
            [
                {
                    "row_id": row_ids[0],
                    "column_id": transcript_column_id,
                    "value": "First row transcript.",
                },
                {
                    "row_id": row_ids[1],
                    "column_id": transcript_column_id,
                    "value": "Second row transcript.",
                },
            ],
        )
        runs.finish_run(transcript_run_id)
        runs.point_column_at_run(
            transcript_op_id,
            transcript_column_id,
            transcript_run_id,
        )
        alternate_op_id = project.append_op(
            "media.transcribe",
            {
                "kind": "media.transcribe",
                "params": {"output_name": "alternate_transcript"},
            },
            label="transcribe alternate transcript",
        )
        alternate_run_id = runs.start_run(
            alternate_op_id,
            sheet_id,
            "media.transcribe",
            params={"output_name": "alternate_transcript"},
            total_rows=1,
            row_ids=[row_ids[1]],
        )
        write_claimed_test_results(
            project,
            alternate_run_id,
            [
                {
                    "row_id": row_ids[1],
                    "column_id": alternate_column_id,
                    "value": "Conflicting second row transcript.",
                },
            ],
        )
        runs.finish_run(alternate_run_id)
        runs.point_column_at_run(
            alternate_op_id,
            alternate_column_id,
            alternate_run_id,
        )
        for row_id, column_id, quote, run_id, op_id in [
            (
                row_ids[0],
                transcript_column_id,
                "First row transcript.",
                transcript_run_id,
                transcript_op_id,
            ),
            (
                row_ids[1],
                transcript_column_id,
                "Second row transcript.",
                transcript_run_id,
                transcript_op_id,
            ),
            (
                row_ids[1],
                alternate_column_id,
                "Conflicting second row transcript.",
                alternate_run_id,
                alternate_op_id,
            ),
        ]:
            lease = resolve_timeline(
                project,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=source_column_id,
            )
            span = record_source_span(
                project,
                artifact_id=lease.anchor.artifact_id,
                span_kind="temporal",
                start_ms=2_000,
                end_ms=5_000,
                quote=quote,
                selector={"segment_index": 0},
            )
            _values, refs = project.get_values_with_refs(
                sheet_id, column_id, row_ids=[row_id]
            )
            record_evidence_link(
                project,
                subject_kind="cell",
                subject_ref=refs[row_id],
                spans=[{"span_id": span["id"], "rank": 0}],
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=column_id,
                run_id=run_id,
                op_id=op_id,
                link_role="media_transcribe_temporal",
                producer={"action_kind": "media.transcribe"},
                metadata={
                    "schema_version": (TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION),
                    "semantic_type": "timestamped_transcript",
                },
            )

        action, bound = _extract_action(
            sheet_id=sheet_id,
            row_ids=row_ids,
            selection={"kind": "draft_range", "start_ms": 2_000, "end_ms": 5_000},
            output_name="clips",
            idempotency_key="extract-batch-transcripts@sha256:v1",
            output_names={
                "clip_transcript": "clips_transcript",
                "clip_alternate_transcript": "clips_alternate_transcript",
            },
            repeat_for_rows=True,
        )

        def fake_stage(_project, source, temporal_range, output_path):
            output_path.write_bytes(f"transcript-derived-{source.row_id}".encode())
            return _fake_rendered_clip(
                output_path,
                start_ms=temporal_range.start_ms,
                end_ms=temporal_range.end_ms,
            )

        result = _run_extract(
            project,
            bound,
            project_id="project-extract-batch-transcripts",
            stage_fn=fake_stage,
        )

        assert result.status == "completed", result.model_dump(mode="json")
        assert result.warnings == []
        by_name = {output.name: output for output in result.outputs}
        assert by_name["clips_transcript"].row_ids == row_ids
        assert by_name["clips_alternate_transcript"].row_ids == [row_ids[1]]
        transcript_column = project.db.execute(
            "SELECT type FROM columns WHERE id=?",
            (by_name["clips_transcript"].column_id,),
        ).fetchone()
        assert transcript_column["type"] == "timestamped_transcript"
        transcript_values = project.get_values(
            sheet_id,
            int(by_name["clips_transcript"].column_id),
            row_ids=row_ids,
        )
        assert transcript_values[row_ids[0]] == "First row transcript."
        assert transcript_values[row_ids[1]] == "Second row transcript."
        alternate_values = project.get_values(
            sheet_id,
            int(by_name["clips_alternate_transcript"].column_id),
            row_ids=row_ids,
        )
        assert alternate_values.get(row_ids[0]) is None
        assert alternate_values[row_ids[1]] == "Conflicting second row transcript."
        projected = resolve_timestamped_transcript(
            project,
            sheet_id=sheet_id,
            row_id=row_ids[0],
            column_id=int(by_name["clips_transcript"].column_id),
        )
        assert projected is not None
        assert [(span["start_ms"], span["end_ms"]) for span in projected.spans] == [
            (0, 3_000)
        ]
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM evidence_links "
                "WHERE link_role='temporal_transcript_projection' AND receipt_id=?",
                (result.receipt_id,),
            ).fetchone()[0]
            == 3
        )

        queued_replay = reserve_typed_action_job(
            project, "project-extract-batch-transcripts", bound
        )
        assert not isinstance(queued_replay, dict)
        assert queued_replay.status == "completed"
        replay_by_name = {output.name: output for output in queued_replay.outputs}
        assert set(replay_by_name) == {
            "clips",
            "clips_transcript",
            "clips_alternate_transcript",
        }
        assert replay_by_name["clips"].row_ids == row_ids
        assert replay_by_name["clips_transcript"].row_ids == row_ids
        assert replay_by_name["clips_alternate_transcript"].row_ids == [row_ids[1]]
        for name, replay_output in replay_by_name.items():
            assert replay_output.kind == "column"
            assert replay_output.sheet_id == sheet_id
            assert replay_output.column_id == by_name[name].column_id
            assert replay_output.ref == by_name[name].ref

        project.apply_edits(
            [
                {
                    "row_id": row_ids[0],
                    "column_id": transcript_column_id,
                    "value": "Edited after extraction.",
                }
            ]
        )
        idempotent_result = _run_extract(
            project,
            bound,
            project_id="project-extract-batch-transcripts",
            stage_fn=lambda *_args: pytest.fail(
                "an idempotent result must not stage again"
            ),
        )
        assert idempotent_result.status == "completed"
        assert idempotent_result.receipt_id == result.receipt_id
    finally:
        project.close()
