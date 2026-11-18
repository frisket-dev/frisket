"""Behavioral coverage for funding attribution on queued project runs."""

from __future__ import annotations

import logging
from pathlib import Path
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from frisket.execution.provider import ExecutionCompositionContext
from frisket.server.action_enqueue import (
    QueuedV1ActionRunContext,
    _queue_v1_project_run_request,
)


class _ReachedRouter(RuntimeError):
    pass


def test_funding_org_id_stays_injectable_queue_metadata() -> None:
    """Hosted composition may inject a funding owner distinct from storage."""

    def router_for(_project):
        raise _ReachedRouter

    ctx = QueuedV1ActionRunContext(
        queue=object(),  # type: ignore[arg-type]
        workspace_root=Path("."),
        router_for=router_for,
        execution_composition_for=lambda _project, _router, _context: None,  # type: ignore[arg-type]
        active_runs={},
        run_jobs={},
        queue_payload_extra={"org_id": 91},
        logger=logging.getLogger("test.queue_funding_attribution"),
    )
    request = SimpleNamespace(entry=SimpleNamespace(payload_codecs=()))

    # The router boundary immediately follows the protected-key merge. If
    # ``org_id`` becomes protected, this raises ValueError before reaching it.
    with pytest.raises(_ReachedRouter):
        _queue_v1_project_run_request(
            object(),  # type: ignore[arg-type]
            "cross-funded-project",
            {},
            ctx=ctx,
            execution_context=ExecutionCompositionContext.direct(),
            request=request,  # type: ignore[arg-type]
        )


def test_actual_hosted_enqueue_producers_persist_storage_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "actual-producers.frisket"
    bundle.mkdir()
    (bundle / "project.db").touch()
    (bundle / "manifest.json").touch()
    _assert_actual_hosted_enqueue_producers_persist_storage_identity(
        monkeypatch=monkeypatch,
        storage_root=tmp_path,
        project_id="actual-producers",
        storage_org_id=23,
    )


def _assert_actual_hosted_enqueue_producers_persist_storage_identity(
    *,
    monkeypatch: pytest.MonkeyPatch,
    storage_root: Path,
    project_id: str,
    storage_org_id: int,
) -> None:
    from frisket.engine.jobs import (
        embeddings,
        notifications_delivery,
        notifications_digest,
        sources,
        alerts,
    )

    enqueued: list[tuple[str, dict]] = []

    class RecordingQueue:
        def find_job_by_refs(self, _kind, **kwargs):
            assert kwargs.get("storage_org_id") == storage_org_id
            return None

        def enqueue(self, kind, payload, **_kwargs):
            assert payload.get("storage_org_id") == storage_org_id, (
                f"actual {kind} producer omitted first-class storage identity"
            )
            enqueued.append((str(kind), dict(payload)))
            return 9000 + len(enqueued)

    queue = RecordingQueue()
    project = object()

    monkeypatch.setattr(
        alerts,
        "find_embedding_similarity_alerts",
        lambda *_args, **_kwargs: [{"id": 801}],
    )
    assert alerts.enqueue_index_alert_evaluations(
        queue,
        project=project,
        project_id=project_id,
        workspace_root=storage_root,
        storage_org_id=storage_org_id,
        index_id="index-801",
        trigger_ref={"source_run_id": 801},
    )

    monkeypatch.setattr(
        sources,
        "_enclosure_row_already_queued_or_downloaded",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        sources,
        "mark_enclosure_queued",
        lambda *_args, **_kwargs: None,
    )
    assert sources.enqueue_enclosure_downloads(
        queue,
        project=project,
        project_id=project_id,
        workspace_root=storage_root,
        storage_org_id=storage_org_id,
        source={"config": {"download_enclosures": "queued"}},
        sheet_id=802,
        row_ids=[803],
    )

    class FakeProject:
        def __init__(self, _path):
            pass

        def close(self):
            pass

        def notification_delivery_request(self, _request_id):
            return {"status": "queued", "job_id": None}

        def set_notification_delivery_request_job_id(self, _request_id, _job_id):
            pass

        def notification_routes(self, *, enabled_only):
            assert enabled_only
            return [{"id": 805, "delivery_mode": "digest"}]

        def notification_digest_run_by_route_window(self, **_kwargs):
            return None

    monkeypatch.setattr(notifications_delivery, "Project", FakeProject)
    notifications_delivery.enqueue_notification_delivery(
        queue,
        workspace_root=storage_root,
        project_id=project_id,
        storage_org_id=storage_org_id,
        request_id=804,
    )

    digest_window = notifications_digest.NotificationDigestWindow(
        cadence="daily",
        window_key="2026-07-10",
        start_at=datetime(2026, 7, 10, tzinfo=UTC),
        end_at=datetime(2026, 7, 11, tzinfo=UTC),
    )
    monkeypatch.setattr(notifications_digest, "Project", FakeProject)
    monkeypatch.setattr(
        notifications_digest,
        "due_digest_window_for_route",
        lambda *_args, **_kwargs: digest_window,
    )
    assert (
        notifications_digest.enqueue_due_notification_digests(
            queue,
            workspace_root=storage_root,
            project_id=project_id,
            storage_org_id=storage_org_id,
        )
        == 1
    )

    class FakeSourceStore:
        def __init__(self, _project):
            pass

        def sources(self):
            return [{"id": 806, "config": "{}"}]

    monkeypatch.setattr(sources, "Project", FakeProject)
    monkeypatch.setattr(sources, "SourceStore", FakeSourceStore)
    monkeypatch.setattr(sources, "_source_due", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        sources,
        "_source_stale_for_notification",
        lambda *_args, **_kwargs: False,
    )
    assert sources.enqueue_due_source_polls(
        workspace_root=storage_root,
        queue=queue,
        storage_org_id=storage_org_id,
        now=datetime(2026, 7, 10, tzinfo=UTC),
    )

    launch_payloads: list[dict] = []
    from frisket.engine.executor import action_jobs

    monkeypatch.setattr(
        action_jobs,
        "reserve_queued_action_job_receipt",
        lambda *_args, **_kwargs: {
            "action_id": "action-807",
            "receipt_id": "receipt-807",
            "params_hash": "sha256:807",
        },
    )

    def capture_embedding_launch(**kwargs):
        envelope = kwargs["envelope"]
        assert isinstance(envelope, action_jobs.ActionJobEnvelope)
        assert envelope.project_id == project_id
        assert envelope.action_kind == "embedding.index_refresh"
        assert envelope.resolve_phase == "worker"
        assert envelope.action["action_id"] == "embedding.index_refresh"
        assert envelope.action["scope"] == {"kind": "project"}
        assert envelope.action["params"]["index_id"] == "index-807"
        assert envelope.action["params"]["row_scope"] == {
            "kind": "row_ids",
            "row_ids": [808],
        }
        payload_extra = dict(kwargs.get("payload_extra") or {})
        assert payload_extra.get("storage_org_id") == storage_org_id
        launch_payloads.append(payload_extra)
        return SimpleNamespace(job_id=9807)

    monkeypatch.setattr(
        embeddings,
        "launch_queued_action_job",
        capture_embedding_launch,
    )
    monkeypatch.setattr(
        embeddings,
        "find_on_source_append_indexes",
        lambda *_args, **_kwargs: [{"id": "index-807"}],
    )
    assert embeddings.enqueue_source_append_refreshes(
        queue,
        project=project,
        project_id=project_id,
        workspace_root=storage_root,
        storage_org_id=storage_org_id,
        sheet_id=807,
        row_ids=[808],
        trigger_ref={"source_run_id": 807},
    ) == [9807]
    assert launch_payloads

    assert {kind for kind, _payload in enqueued} >= {
        alerts.ALERT_EVALUATE_KIND,
        sources.ENCLOSURE_DOWNLOAD_KIND,
        sources.SOURCE_POLL_KIND,
        notifications_delivery.NOTIFICATION_DELIVER_KIND,
        notifications_digest.NOTIFICATION_DIGEST_KIND,
    }
