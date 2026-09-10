"""scheduled embedding refresh + automatic remote/cost gates .

Proves scheduled maintenance is queue-shaped (never inline) and that unattended
remote refresh is gated:
- a `mode: scheduled` index enqueues a refresh when due (last_refreshed_at +
  interval <= now), deduped per cadence window, with NO row_ids (full scope);
- the worker runs a scheduled refresh over the whole index;
- automatic remote refresh blocks BEFORE the gateway without
  allow_remote_automatic_refresh (embedding_remote_confirmation_required) and
  without a cost ceiling (embedding_cost_requires_confirmation);
- with both opt-ins it runs; a MANUAL remote refresh ignores the automatic gates;
- crash/stale-running recovers at the queue layer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from frisket.ai.embeddings import EmbeddingStore, VectorBackend, build_batch_result
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.jobs import (
    EMBEDDING_REFRESH_KIND,
    HandlerRegistry,
    SqliteJobQueue,
    Worker,
    enqueue_due_scheduled_refreshes,
    find_scheduled_indexes,
    register_embedding_refresh_handler,
)
from frisket.engine.store import Project

NOW = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


class FakeGateway:
    def __init__(self, dim: int = 384):
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, texts, *, provider, model, modality):
        self.calls.append(list(texts))
        vectors = [[1.0] + [0.0] * (self.dim - 1) for _ in texts]
        return build_batch_result(
            vectors,
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
        )


@pytest.fixture
def env(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project_id = "proj"
    project = Project.create(workspace / f"{project_id}.frisket", name="proj")
    sheet = project.add_sheet("feed")
    cols = {"headline": project.add_column(sheet, "headline")}
    project.add_rows(sheet, [{"headline": f"story {i}"} for i in range(3)], cols)
    queue = SqliteJobQueue(workspace / ".queue.db")
    yield SimpleNamespace(
        workspace=workspace,
        project_id=project_id,
        project=project,
        sheet=sheet,
        cols=cols,
        queue=queue,
    )
    project.close()
    queue.close()


def _create_index(
    env, *, provider="fastembed", policy=None, schedule="@hourly", key="c"
):
    action = {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": env.sheet,
            "source_columns": ["headline"],
            "modality": "text",
            "provider": provider,
            "source_policy": {"kind": "text_cell"},
            "maintenance_policy": {"mode": "scheduled", "schedule": schedule},
            "provider_policy": policy
            if policy is not None
            else {"allow_remote": False},
        },
        "idempotency_key": f"emb_create@{key}",
    }
    result = run_action_spec(env.project, action, project_id=env.project_id)
    assert result.status == "completed", result.errors
    return result.outputs[0].ref["index_id"]


def _set_last_refreshed(env, index_id, when: datetime | None):
    env.project.db.execute(
        "UPDATE embedding_indexes SET last_refreshed_at=? WHERE id=?",
        (when.isoformat() if when else None, index_id),
    )
    env.project.db.commit()


def _registry(env, gateway):
    reg = HandlerRegistry()
    register_embedding_refresh_handler(
        reg, workspace_root=env.workspace, gateway=gateway
    )
    return reg


def _enqueue(env, now=NOW):
    return enqueue_due_scheduled_refreshes(
        workspace_root=env.workspace, queue=env.queue, now=now
    )


# --------------------------------------------------------------------------


def test_scheduled_index_enqueues_when_due(env):
    index_id = _create_index(env)  # never refreshed -> due
    enqueued = _enqueue(env)
    assert len(enqueued) == 1
    [job] = env.queue.list_project_jobs(env.project_id, kind=EMBEDDING_REFRESH_KIND)
    assert job.status == "queued"  # enqueued, not run inline
    assert job.payload["index_id"] == index_id
    assert job.payload["mode"] == "incremental"
    assert "row_ids" not in job.payload  # full-scope, not row-scoped
    assert job.payload["trigger_ref"]["trigger_kind"] == "schedule"
    assert job.payload["dedupe_key"].startswith(f"{index_id}:scheduled:")


def test_not_due_when_recently_refreshed(env):
    index_id = _create_index(env, schedule="@daily")
    _set_last_refreshed(env, index_id, NOW - timedelta(hours=1))  # < 1 day ago
    assert _enqueue(env) == []
    assert env.queue.counts().get("queued", 0) == 0


def test_due_again_after_interval_elapsed(env):
    index_id = _create_index(env, schedule="@hourly")
    _set_last_refreshed(env, index_id, NOW - timedelta(hours=2))  # > 1h ago
    assert len(_enqueue(env)) == 1


def test_unscheduled_index_not_enqueued(env):
    action = {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": env.sheet,
            "source_columns": ["headline"],
            "modality": "text",
            "provider": "fastembed",
            "source_policy": {"kind": "text_cell"},
            "maintenance_policy": {"mode": "manual"},
            "provider_policy": {"allow_remote": False},
        },
        "idempotency_key": "emb_create@manual",
    }
    assert run_action_spec(env.project, action, project_id=env.project_id).status == (
        "completed"
    )
    assert find_scheduled_indexes(env.project) == []
    assert _enqueue(env) == []


def test_duplicate_window_does_not_duplicate_pending(env):
    _create_index(env)
    first = _enqueue(env)
    second = _enqueue(env)  # same cadence window (same now)
    assert len(first) == 1 and second == []
    assert env.queue.counts()["queued"] == 1


def test_worker_runs_scheduled_refresh_over_full_scope(env):
    index_id = _create_index(env)
    _enqueue(env)
    gw = FakeGateway()
    worker = Worker(env.queue, _registry(env, gw))
    assert worker.run_once() is True
    assert sorted(gw.calls[0]) == ["story 0", "story 1", "story 2"]  # whole index
    backend = VectorBackend(env.project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {"ready": 3}
    backend.close()
    [job] = env.queue.list_project_jobs(env.project_id, kind=EMBEDDING_REFRESH_KIND)
    assert job.status == "done"
    assert job.result["status"] == "completed"


def test_automatic_remote_without_auto_flag_blocks_before_gateway(env):
    # allow_remote=true confirms a MANUAL remote refresh, not an unattended one
    _create_index(env, provider="openai", policy={"allow_remote": True})
    _enqueue(env)
    gw = FakeGateway(dim=1536)
    worker = Worker(env.queue, _registry(env, gw))
    assert worker.run_once() is True
    assert gw.calls == []  # no egress
    [job] = env.queue.list_project_jobs(env.project_id, kind=EMBEDDING_REFRESH_KIND)
    assert job.result["blocked"] is True
    assert job.result["error_code"] == "embedding_remote_confirmation_required"


def test_automatic_remote_without_cost_ceiling_requires_confirmation(env):
    _create_index(
        env,
        provider="openai",
        policy={"allow_remote": True, "allow_remote_automatic_refresh": True},
    )
    _enqueue(env)
    gw = FakeGateway(dim=1536)
    worker = Worker(env.queue, _registry(env, gw))
    assert worker.run_once() is True
    assert gw.calls == []  # cost gate fires before the provider call
    [job] = env.queue.list_project_jobs(env.project_id, kind=EMBEDDING_REFRESH_KIND)
    assert job.result["blocked"] is True
    assert job.result["error_code"] == "embedding_cost_requires_confirmation"


def test_automatic_remote_with_flags_and_ceiling_runs(env):
    index_id = _create_index(
        env,
        provider="openai",
        policy={
            "allow_remote": True,
            "allow_remote_automatic_refresh": True,
            "max_cost_usd_per_refresh": 1.0,
        },
    )
    _enqueue(env)
    gw = FakeGateway(dim=1536)
    worker = Worker(env.queue, _registry(env, gw))
    assert worker.run_once() is True
    assert len(gw.calls) == 1  # ran
    backend = VectorBackend(env.project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {"ready": 3}
    backend.close()


def test_manual_remote_refresh_ignores_automatic_gates(env):
    # a human-initiated refresh (no automatic trigger) is NOT subject to the
    # automatic-refresh / cost gates — allow_remote alone is enough.
    index_id = _create_index(env, provider="openai", policy={"allow_remote": True})
    gw = FakeGateway(dim=1536)
    result = run_action_spec(
        env.project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": "manual_refresh@1",
        },
        project_id=env.project_id,
        deps=ExecutorDeps(embedding_gateway=gw),
    )
    assert result.status == "completed", result.errors
    assert len(gw.calls) == 1


def test_crash_recovery_requeues_scheduled_job(env):
    _create_index(env)
    _enqueue(env)
    claimed = env.queue.claim("dead-worker", lease_seconds=0.0)
    assert claimed is not None and claimed.kind == EMBEDDING_REFRESH_KIND
    assert env.queue.recover_expired() == 1
    [job] = env.queue.list_project_jobs(env.project_id, kind=EMBEDDING_REFRESH_KIND)
    assert job.status == "queued"
    gw = FakeGateway()
    worker = Worker(env.queue, _registry(env, gw))
    assert worker.run_once() is True
    assert (
        EmbeddingStore(env.project).get_index(_only_index_id(env))["ready_items"] == 3
    )


def test_blocked_scheduled_attempt_not_retried_until_next_window(env):
    # an automatic remote refresh blocks (no auto flag); the cadence attempt must
    # dedupe across ALL statuses (incl. the done/blocked job), so the same window
    # does not re-enqueue — only the next cadence window does.
    _create_index(env, provider="openai", policy={"allow_remote": True})
    assert len(_enqueue(env, now=NOW)) == 1
    gw = FakeGateway(dim=1536)
    worker = Worker(env.queue, _registry(env, gw))
    assert worker.run_once() is True
    [job] = env.queue.list_project_jobs(env.project_id, kind=EMBEDDING_REFRESH_KIND)
    assert job.status == "done" and job.result["blocked"] is True
    # same cadence window -> the done/blocked attempt is not retried
    assert _enqueue(env, now=NOW) == []
    # the next cadence window (one interval later) enqueues a fresh attempt
    assert len(_enqueue(env, now=NOW + timedelta(hours=1))) == 1


def test_cli_scheduler_helper_enqueues_due(env):
    # the worker's embedding-scheduler thread calls this helper each tick
    from frisket.cli import _enqueue_due_scheduled_embeddings

    _create_index(env)  # due (never refreshed)
    # SchedulerRoot became (root, storage_org_id) in 446962d; this suite was
    # left passing bare Paths and has been red since — same-day-quarantine
    # cleanup 2026-07-17.
    assert _enqueue_due_scheduled_embeddings(env.queue, [(env.workspace, None)]) == 1
    # idempotent within the cadence window
    assert _enqueue_due_scheduled_embeddings(env.queue, [(env.workspace, None)]) == 0


def _only_index_id(env):
    return EmbeddingStore(env.project).list_indexes(env.sheet)[0]["id"]
