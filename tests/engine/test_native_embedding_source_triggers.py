"""Queued embedding refresh + on_source_append trigger pilot (handoff Lane 3).

Proves the recurring/source-triggered path is queue-shaped, not inline:
- an on_source_append append ENQUEUES refresh work and never calls a provider;
- duplicate triggers do not create duplicate pending work;
- the worker embeds only the affected rows, not the whole sheet;
- a remote index without allow_remote is blocked before the gateway is called;
- crash/stale-running is handled at the narrow queue layer (recover_expired).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from frisket.ai.embeddings import (
    EmbeddingProviderError,
    VectorBackend,
    build_batch_result,
)
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.jobs import (
    EMBEDDING_REFRESH_KIND,
    WATCH_EVALUATE_KIND,
    HandlerRegistry,
    SqliteJobQueue,
    Worker,
    enqueue_source_append_refreshes,
    find_on_source_append_indexes,
    refresh_dedupe_key,
    register_embedding_refresh_handler,
    register_watch_evaluate_handler,
)
from frisket.ai.llm import ModelRouter
from frisket.server.workspace import Workspace
from frisket.engine.store import Project


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


class FlakyGateway(FakeGateway):
    def __init__(self, dim: int = 384):
        super().__init__(dim=dim)
        self.attempts = 0

    def embed(self, texts, *, provider, model, modality):
        self.attempts += 1
        if self.attempts == 1:
            raise EmbeddingProviderError("temporary provider failure")
        return super().embed(texts, provider=provider, model=model, modality=modality)


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


def _create_index(env, *, provider="fastembed", policy=None, maintenance=None, key="c"):
    action = {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": env.sheet,
            "source_columns": ["headline"],
            "modality": "text",
            "provider": provider,
            "source_policy": {"kind": "text_cell"},
            "maintenance_policy": maintenance
            if maintenance is not None
            else {"mode": "on_source_append"},
            "provider_policy": policy
            if policy is not None
            else {"allow_remote": False},
        },
        "idempotency_key": f"emb_create@{key}",
    }
    result = run_action_spec(env.project, action, project_id=env.project_id)
    assert result.status == "completed", result.errors
    return result.outputs[0].ref["index_id"]


def _row_id(env, text):
    values = env.project.get_values(env.sheet, env.cols["headline"])
    return next(rid for rid, v in values.items() if v == text)


def _registry(env, gateway):
    reg = HandlerRegistry()
    register_embedding_refresh_handler(
        reg, workspace_root=env.workspace, gateway=gateway
    )
    return reg


def _all_rows(env):
    return list(env.project.visible_row_ids(env.sheet))


def _trigger(env, row_ids, *, source_run_id=7):
    return enqueue_source_append_refreshes(
        env.queue,
        project=env.project,
        project_id=env.project_id,
        workspace_root=env.workspace,
        sheet_id=env.sheet,
        row_ids=row_ids,
        trigger_ref={
            "trigger_kind": "source_run_completed",
            "source_run_id": source_run_id,
        },
    )


# --------------------------------------------------------------------------


def test_append_enqueues_without_calling_provider(env):
    index_id = _create_index(env)
    job_ids = _trigger(env, _all_rows(env))
    assert len(job_ids) == 1
    [job] = env.queue.list_project_jobs(env.project_id, kind=EMBEDDING_REFRESH_KIND)
    assert job.status == "queued"  # enqueued, not run
    assert job.payload["index_id"] == index_id
    assert job.payload["mode"] == "incremental"
    assert job.payload["row_ids"] == sorted(_all_rows(env))
    # no provider ran inline → no sidecar vectors yet
    backend = VectorBackend(env.project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {}
    backend.close()


def test_manual_index_is_not_triggered(env):
    _create_index(env, maintenance={"mode": "manual"}, key="manual")
    assert find_on_source_append_indexes(env.project, env.sheet) == []
    assert _trigger(env, _all_rows(env)) == []
    assert env.queue.counts()["queued"] == 0


def test_duplicate_trigger_does_not_duplicate_pending(env):
    _create_index(env)
    first = _trigger(env, _all_rows(env), source_run_id=42)
    second = _trigger(env, _all_rows(env), source_run_id=42)
    assert len(first) == 1
    assert second == []  # same (index, source run) → no duplicate pending job
    assert env.queue.counts()["queued"] == 1


def test_worker_embeds_only_affected_rows(env):
    index_id = _create_index(env)
    affected = [_row_id(env, "story 1")]
    _trigger(env, affected)
    gw = FakeGateway()
    worker = Worker(env.queue, _registry(env, gw))
    assert worker.run_once() is True
    # only the one appended row was embedded, not the whole 3-row sheet
    assert gw.calls == [["story 1"]]
    backend = VectorBackend(env.project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {"ready": 1}
    assert backend.get_item(index_id, str(affected[0])) is not None
    backend.close()
    [job] = env.queue.list_project_jobs(env.project_id, kind=EMBEDDING_REFRESH_KIND)
    assert job.status == "done"
    assert job.result["status"] == "completed"
    assert job.result["refreshed"] == 1


def test_remote_index_without_policy_blocks_before_gateway(env):
    _create_index(env, provider="openai", policy={"allow_remote": False})
    _trigger(env, _all_rows(env))
    gw = FakeGateway(dim=1536)
    worker = Worker(env.queue, _registry(env, gw))
    assert worker.run_once() is True
    assert gw.calls == []  # provider never called — no egress
    [job] = env.queue.list_project_jobs(env.project_id, kind=EMBEDDING_REFRESH_KIND)
    assert job.status == "done"
    assert job.result["blocked"] is True
    assert job.result["error_code"] == "embedding_remote_confirmation_required"


def test_remote_index_automatic_without_auto_flag_blocks(env):
    # allow_remote alone confirms a MANUAL remote refresh; an on_source_append
    # (automatic) trigger needs allow_remote_automatic_refresh too. Blocks before
    # the gateway — no egress.
    _create_index(env, provider="openai", policy={"allow_remote": True})
    _trigger(env, _all_rows(env))
    gw = FakeGateway(dim=1536)
    worker = Worker(env.queue, _registry(env, gw))
    assert worker.run_once() is True
    assert gw.calls == []
    [job] = env.queue.list_project_jobs(env.project_id, kind=EMBEDDING_REFRESH_KIND)
    assert job.result["blocked"] is True
    assert job.result["error_code"] == "embedding_remote_confirmation_required"


def test_remote_index_with_policy_runs_in_worker(env):
    # source_run_completed is an AUTOMATIC trigger, so a remote index needs the
    # stricter unattended policy (allow_remote_automatic_refresh + a cost ceiling),
    # not allow_remote alone (see native-embedding-scheduled-refresh).
    index_id = _create_index(
        env,
        provider="openai",
        policy={
            "allow_remote": True,
            "allow_remote_automatic_refresh": True,
            "max_cost_usd_per_refresh": 1.0,
        },
    )
    _trigger(env, _all_rows(env))
    gw = FakeGateway(dim=1536)
    worker = Worker(env.queue, _registry(env, gw))
    assert worker.run_once() is True
    assert len(gw.calls) == 1
    backend = VectorBackend(env.project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {"ready": 3}
    backend.close()


def test_retryable_provider_error_requeues_then_succeeds(env):
    index_id = _create_index(env)
    _trigger(env, _all_rows(env))
    gw = FlakyGateway()
    worker = Worker(env.queue, _registry(env, gw), retry_base_seconds=0)

    assert worker.run_once() is True
    [job] = env.queue.list_project_jobs(env.project_id, kind=EMBEDDING_REFRESH_KIND)
    assert job.status == "queued"
    assert job.attempts == 1
    assert "embedding_provider_error" in (job.error or "")
    assert gw.calls == []

    assert worker.run_once() is True
    [job] = env.queue.list_project_jobs(env.project_id, kind=EMBEDDING_REFRESH_KIND)
    assert job.status == "done"
    assert job.result["status"] == "completed"
    assert len(gw.calls) == 1
    backend = VectorBackend(env.project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {"ready": 3}
    backend.close()


# --------------------------------------------------------------------------
# The server Workspace registry can drain embedding-refresh jobs
# --------------------------------------------------------------------------


class _FakeOpenAIAdapter:
    """A 1536-d remote embedder so the workspace worker drains without a real
    model — exercising the router/gateway shape the Workspace wires in."""

    base_url = "https://example/v1"

    async def embed_with_meta(self, texts, model, client):
        vectors = [[1.0] + [0.0] * 1535 for _ in texts]
        return vectors, {"actual_model_id": model, "dimension": 1536}


def _remote_index_action(sheet_id):
    return {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "source_columns": ["headline"],
            "modality": "text",
            "provider": "openai",
            "source_policy": {"kind": "text_cell"},
            "maintenance_policy": {"mode": "on_source_append"},
            "provider_policy": {
                "allow_remote": True,
                "allow_remote_automatic_refresh": True,
                "max_cost_usd_per_refresh": 1.0,
            },
        },
        "idempotency_key": "ws_create@1",
    }


def test_server_workspace_registry_drains_embedding_refresh(tmp_path):
    router = ModelRouter(keys={})
    router._adapters["openai"] = _FakeOpenAIAdapter()
    ws = Workspace(tmp_path / "ws", router=router)
    try:
        # The Workspace registry has the handler (source.poll enqueues
        # these onto ws.queue, so any worker on ws.registry must run them).
        assert ws.registry.get(EMBEDDING_REFRESH_KIND) is not None

        pid = ws.create("proj")["id"]
        project = ws.get(pid)
        sheet = project.add_sheet("feed")
        cols = {"headline": project.add_column(sheet, "headline")}
        project.add_rows(sheet, [{"headline": f"s{i}"} for i in range(2)], cols)
        result = run_action_spec(project, _remote_index_action(sheet), project_id=pid)
        assert result.status == "completed", result.errors
        index_id = result.outputs[0].ref["index_id"]

        enqueue_source_append_refreshes(
            ws.queue,
            project=project,
            project_id=pid,
            workspace_root=ws.root,
            sheet_id=sheet,
            row_ids=project.visible_row_ids(sheet),
            trigger_ref={"trigger_kind": "source_run_completed", "source_run_id": 1},
        )
        # Drain through Worker(ws.queue, ws.registry) end to end.
        worker = Worker(ws.queue, ws.registry)
        assert worker.run_once() is True
        backend = VectorBackend(project)
        backend.ensure_schema()
        assert backend.item_counts(index_id) == {"ready": 2}
        backend.close()
        [job] = ws.queue.list_project_jobs(pid, kind=EMBEDDING_REFRESH_KIND)
        assert job.status == "done"
        assert job.result["status"] == "completed"
    finally:
        ws.queue.close()


# --------------------------------------------------------------------------
# Dedupe cannot miss an older job behind a result cap
# --------------------------------------------------------------------------


def test_dedupe_finds_oldest_job_beyond_500(env):
    index_id = _create_index(env)
    # the OLDEST embedding-refresh job — the one we will later duplicate
    first = _trigger(env, _all_rows(env), source_run_id=1)
    assert len(first) == 1
    # pile up 600 newer embedding-refresh jobs (distinct dedupe keys) so the
    # oldest is far outside any bounded most-recent window
    for run in range(2, 602):
        env.queue.enqueue(
            EMBEDDING_REFRESH_KIND,
            {
                "project_id": env.project_id,
                "index_id": index_id,
                "dedupe_key": refresh_dedupe_key(
                    index_id,
                    {"trigger_kind": "source_run_completed", "source_run_id": run},
                    [],
                ),
            },
        )
    # the duplicate of the OLDEST trigger must still be found (indexed lookup),
    # not re-enqueued despite 600 newer same-kind jobs.
    dup = _trigger(env, _all_rows(env), source_run_id=1)
    assert dup == []
    matching = [
        j
        for j in env.queue.list_project_jobs(
            env.project_id, kind=EMBEDDING_REFRESH_KIND, limit=1000
        )
        if j.payload.get("dedupe_key") == first_key(env, index_id)
    ]
    assert len(matching) == 1  # exactly one job for run 1, no duplicate


def first_key(env, index_id):
    return refresh_dedupe_key(
        index_id,
        {"trigger_kind": "source_run_completed", "source_run_id": 1},
        sorted(_all_rows(env)),
    )


# --------------------------------------------------------------------------
# source append -> embedding refresh -> watch evaluation order
# --------------------------------------------------------------------------


def _full_refresh(env, index_id):
    run_action_spec(
        env.project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": f"full@{index_id}",
        },
        project_id=env.project_id,
        deps=ExecutorDeps(embedding_gateway=FakeGateway()),
    )


def _embedding_watch(env, index_id, anchor_row):
    return env.project.add_watch(
        "similar",
        scope="sheet",
        sheet_id=env.sheet,
        query={
            "kind": "embedding_similarity",
            "scope": {"kind": "sheet", "sheet_id": env.sheet},
            "embedding_index_id": index_id,
            "anchor": {"kind": "row", "row_id": anchor_row},
        },
    )


def _registry_with_watches(env, gateway):
    reg = HandlerRegistry()
    register_embedding_refresh_handler(
        reg, workspace_root=env.workspace, gateway=gateway, queue=env.queue
    )
    register_watch_evaluate_handler(
        reg,
        workspace_root=env.workspace,
        queue=env.queue,
    )
    return reg


def test_watch_evaluate_handler_requires_delivery_queue(tmp_path):
    with pytest.raises(TypeError, match="queue"):
        register_watch_evaluate_handler(  # type: ignore[call-arg]
            HandlerRegistry(),
            workspace_root=tmp_path,
        )


def _notification_count(env):
    return env.project.db.execute(
        "SELECT COUNT(*) AS n FROM notification_items"
    ).fetchone()["n"]


def test_source_append_embedding_watch_evaluates_after_refresh_not_before(env):
    index_id = _create_index(env)
    _full_refresh(env, index_id)
    anchor = _row_id(env, "story 0")
    watch_id = _embedding_watch(env, index_id, anchor)

    # append a new row + enqueue the source-append refresh (the source-poll path)
    env.project.add_rows(env.sheet, [{"headline": "story 3"}], env.cols)
    _trigger(env, [_row_id(env, "story 3")])

    # the source-append enqueue does NOT evaluate the embedding watch inline
    assert env.project.watch_runs_total(watch_id) == 0

    worker = Worker(env.queue, _registry_with_watches(env, FakeGateway()))
    # job 1: the refresh runs; the watch is STILL not evaluated, but a
    # watch.evaluate job is now queued (refresh -> watch ordering).
    assert worker.run_once() is True
    assert env.project.watch_runs_total(watch_id) == 0
    pending = env.queue.list_project_jobs(env.project_id, kind=WATCH_EVALUATE_KIND)
    assert any(j.status == "queued" for j in pending)

    # job 2: the watch.evaluate runs AFTER the refresh -> one ok run
    assert worker.run_once() is True
    assert env.project.watch_runs_total(watch_id) == 1
    assert env.project.watch_latest_run(watch_id)["status"] == "ok"
    # exactly one notification for the entered rows
    assert _notification_count(env) == 1

    # draining again does nothing (exactly once)
    assert worker.run_once() is False
    assert env.project.watch_runs_total(watch_id) == 1
    assert _notification_count(env) == 1


def test_queued_automatic_watch_rechecks_pause_but_manual_evaluation_remains_available(
    env,
):
    """A pause between enqueue and claim must not turn into a stale auto-run."""
    from frisket.features.watchlists.service import run_watch_evaluation

    index_id = _create_index(env, key="pause-queue")
    _full_refresh(env, index_id)
    watch_id = _embedding_watch(env, index_id, _row_id(env, "story 0"))
    env.project.add_rows(env.sheet, [{"headline": "story queued"}], env.cols)
    _trigger(env, [_row_id(env, "story queued")], source_run_id=99)

    worker = Worker(env.queue, _registry_with_watches(env, FakeGateway()))
    assert worker.run_once() is True  # refresh enqueues watch.evaluate
    [queued] = env.queue.list_project_jobs(
        env.project_id, kind=WATCH_EVALUATE_KIND, status="queued"
    )
    assert queued.payload["watch_id"] == watch_id

    # The lifecycle route owns the corresponding public mutation. This direct
    # state transition isolates the worker's required re-fetch-at-claim seam.
    env.project.db.execute("UPDATE watches SET enabled=0 WHERE id=?", (watch_id,))
    env.project.db.commit()

    assert worker.run_once() is True
    [completed] = env.queue.list_project_jobs(
        env.project_id, kind=WATCH_EVALUATE_KIND, status="done"
    )
    assert completed.result == {
        "project_id": env.project_id,
        "watch_id": watch_id,
        "skipped": True,
        "reason": "disabled",
    }
    assert env.project.watch_runs_total(watch_id) == 0

    # "Run now" is deliberate/manual, so pause does not hide it or turn it
    # into deletion. It produces the first run without re-enabling the Watch.
    manual = run_watch_evaluation(env.project, env.project.get_watch(watch_id))
    assert manual["run"]["status"] == "ok"
    assert env.project.watch_runs_total(watch_id) == 1


def test_source_poll_skips_embedding_watches_inline(env):
    from frisket.features.watchlists.triggers import affected_enabled_watches

    index_id = _create_index(env)
    _full_refresh(env, index_id)
    emb_watch = _embedding_watch(env, index_id, _row_id(env, "story 0"))
    fts_watch = env.project.add_watch(
        "fts",
        scope="sheet",
        sheet_id=env.sheet,
        query={
            "kind": "fts",
            "q": "story",
            "scope": {"kind": "sheet", "sheet_id": env.sheet},
        },
    )
    materialization = {
        "kind": "source_poll_materialized",
        "sheet_id": env.sheet,
        "source_id": 1,
        "source_run_id": 1,
        "op_id": 1,
        "row_ids": [_row_id(env, "story 1")],
    }
    affected = {
        int(w["id"]) for w in affected_enabled_watches(env.project, materialization)
    }
    # embedding watches are deferred to post-refresh; non-embedding stay inline
    assert emb_watch not in affected
    assert fts_watch in affected
