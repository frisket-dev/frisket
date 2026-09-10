import json

import pytest

from frisket.actions.cluster import CLUSTER_VALUES
from frisket.actions.core import RegisteredAction
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor.actions import _default_map_runner_factory
from frisket.engine.executor.cluster_action import run_typed_cluster_action
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


@pytest.fixture
def cluster_project(tmp_path, monkeypatch):
    registered = RegisteredAction("custom.cluster", CLUSTER_VALUES)
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, "custom.cluster": registered},
    )
    project = Project.create(tmp_path / "fixture.frisket")
    sheet = project.add_sheet("Names")
    column = project.add_column(sheet, "name")
    project.add_rows(
        sheet, [{"name": "Jon Smith"}, {"name": "Smith Jon"}], {"name": column}
    )

    def execute(
        *,
        router=None,
        options=None,
        output="canonical",
        confirmation=None,
        key="cluster",
    ):
        request = ActionRequest(
            action_id="custom.cluster",
            scope={"kind": "sheet_rows", "sheet_id": sheet},
            params={"source": "name", **(options or {})},
            output_names={"canonical": output},
            idempotency_key=key,
            confirmation=confirmation,
        )
        return run_typed_cluster_action(
            project,
            "test",
            BoundTypedActionRequest.bind(registered, request),
            router,
            _default_map_runner_factory,
        )

    yield project, sheet, column, execute
    project.close()


def test_remote_cluster_exact_consent_accounting_and_cache(
    cluster_project, monkeypatch
):
    from test_cluster_values_executor import _remote_embedding_router

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project, sheet, source, execute = cluster_project
    router, adapter = _remote_embedding_router()
    from frisket.team.security.secrets import encrypt_secret, key_hint

    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-test-cluster"),
        hint=key_hint("sk-test-cluster"),
        spend_cap_micro=1_000_000,
    )
    quote = execute(router=router, options={"method": "semantic"})
    assert quote.status == "needs_confirmation", "\n".join(
        error.message for error in quote.errors
    )
    assert adapter.calls == []
    assert [column["name"] for column in project.columns(sheet)] == ["name"]
    confirmed = quote.errors[0].details["promise_set_hash"]
    result = execute(
        router=router, options={"method": "semantic"}, confirmation=confirmed
    )
    assert result.status == "completed", result.model_dump()
    assert len(adapter.calls) == 1
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt.provider_use[0]["cost_actual"] == adapter.COST_USD
    call = project.db.execute(
        "SELECT * FROM model_calls WHERE run_id=?", (result.run_id,)
    ).fetchone()
    assert call["provider_cost_usd"] == adapter.COST_USD
    spent = project.provider_spend_state("openai").spent_micro
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-test-cluster"),
        hint=key_hint("sk-test-cluster"),
        spend_cap_micro=spent,
    )
    cached = execute(
        router=router, options={"method": "semantic"}, output="cached", key="cached"
    )
    assert cached.status == "completed", cached.model_dump()
    assert len(adapter.calls) == 1


@pytest.mark.parametrize("method", ["fingerprint", "ngram_fingerprint", "semantic"])
def test_network_off_keeps_local_clustering(cluster_project, monkeypatch, method):
    project, sheet, source, execute = cluster_project
    monkeypatch.setattr(project, "effective_network_policy", lambda: "off")
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda *a, **kw: (lambda texts: [[1.0, 0.0] for _ in texts], "fastembed/test"),
    )
    result = execute(options={"method": method})
    assert result.status == "completed", result.model_dump()


def test_network_off_refuses_remote_before_any_call(cluster_project, monkeypatch):
    from test_cluster_values_executor import _remote_embedding_router

    project, sheet, source, execute = cluster_project
    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    monkeypatch.setattr(project, "effective_network_policy", lambda: "off")
    router, adapter = _remote_embedding_router()
    result = execute(router=router, options={"method": "semantic"})
    assert result.status == "failed", result.model_dump()
    assert result.errors[0].code == "network_disabled"
    assert adapter.calls == []
    assert [column["name"] for column in project.columns(sheet)] == ["name"]


def test_review_hash_refuses_changed_source_before_publication(cluster_project):
    project, sheet, source, execute = cluster_project
    result = execute(options={"review": {"source_hash": "sha256:" + "0" * 64}})
    assert result.status == "failed", result.model_dump()
    assert result.errors[0].code == "stale_input"
    assert [column["name"] for column in project.columns(sheet)] == ["name"]
    assert project.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_source_change_during_paid_clustering_preserves_cost_not_output(
    cluster_project, monkeypatch
):
    from test_cluster_values_executor import _remote_embedding_router

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project, sheet, source, execute = cluster_project
    router, adapter = _remote_embedding_router()
    quote = execute(router=router, options={"method": "semantic"})
    adapter.on_embed = lambda: project.add_rows(
        sheet, [{"name": "New name"}], {"name": source}
    )
    result = execute(
        router=router,
        options={"method": "semantic"},
        confirmation=quote.errors[0].details["promise_set_hash"],
    )
    assert result.status == "failed", result.model_dump()
    assert result.errors[0].code == "stale_input"
    assert len(adapter.calls) == 1
    assert [column["name"] for column in project.columns(sheet)] == ["name"]
    assert (
        project.db.execute("SELECT count(*) FROM cell_result_heads").fetchone()[0] == 0
    )
    call = project.db.execute(
        "SELECT provider_cost_usd FROM model_calls WHERE run_id=?", (result.run_id,)
    ).fetchone()
    assert call["provider_cost_usd"] == adapter.COST_USD
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt.provider_use[0]["cost_actual"] == adapter.COST_USD


def test_partial_vector_cache_loss_recovers_paid_full_batch(
    cluster_project, monkeypatch
):
    import sqlite3
    from array import array
    from frisket import semantic
    from test_cluster_values_executor import _remote_embedding_router

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project, sheet, source, execute = cluster_project
    router, adapter = _remote_embedding_router()
    quote = execute(router=router, options={"method": "semantic"})
    confirmation = quote.errors[0].details["promise_set_hash"]
    original = semantic._sidecar

    class BrokenCache:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def commit(self):
            self.db.rollback()
            raise sqlite3.OperationalError("cache write failed")

    monkeypatch.setattr(semantic, "_sidecar", lambda p: BrokenCache(original(p)))
    with pytest.raises(sqlite3.OperationalError, match="cache write failed"):
        execute(
            router=router, options={"method": "semantic"}, confirmation=confirmation
        )
    assert len(adapter.calls) == 1
    assert (
        project.db.execute("SELECT state FROM effect_checkpoints").fetchone()[0]
        == "returned"
    )
    monkeypatch.setattr(semantic, "_sidecar", original)
    db = original(project)
    db.execute(
        "INSERT INTO cell_vec(key,vec) VALUES (?,?)",
        (
            semantic._vec_key("openai/text-embedding-3-small", adapter.calls[0][0]),
            array("f", [1.0, 0.0, 0.0]).tobytes(),
        ),
    )
    db.commit()
    db.close()
    project.db.execute(
        "UPDATE receipts SET created_at='2000-01-01T00:00:00Z' WHERE status='running'"
    )
    project.db.commit()
    cleared = execute(
        router=router, options={"method": "semantic"}, confirmation=confirmation
    )
    assert cleared.errors[0].code == "idempotency_stale_running", cleared.model_dump()
    live = execute(
        router=router, options={"method": "semantic"}, confirmation=confirmation
    )
    assert live.errors[0].code == "output_column_busy", live.model_dump()
    assert len(adapter.calls) == 1
    from frisket.execution.attempt import abandon_stale_dispatching_attempts
    from frisket.engine.store.output_claims import OutputColumnClaimStore

    project.db.execute(
        "UPDATE execution_attempts SET created_at='2000-01-01T00:00:00Z' WHERE state='dispatching'"
    )
    project.db.execute(
        "UPDATE output_column_claims SET lease_expires_at='2000-01-01T00:00:00Z' WHERE status='active'"
    )
    project.db.commit()
    run_id = project.db.execute("SELECT id FROM runs").fetchone()[0]
    assert abandon_stale_dispatching_attempts(project, run_id) == 1
    claims = OutputColumnClaimStore(project)
    for claim in project.db.execute(
        "SELECT id FROM output_column_claims WHERE run_id=? AND status='active'",
        (run_id,),
    ).fetchall():
        claims.release_stale(
            claim_id=claim["id"], reason="test: verified expired abandoned writer"
        )
    result = execute(
        router=router, options={"method": "semantic"}, confirmation=confirmation
    )
    if result.status == "needs_confirmation":
        result = execute(
            router=router,
            options={"method": "semantic"},
            confirmation=result.errors[0].details["promise_set_hash"],
        )
    assert result.status == "completed", result.model_dump()
    assert len(adapter.calls) == 1
    assert project.db.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 1
    assert (
        project.db.execute("SELECT count(*) FROM effect_checkpoints").fetchone()[0] == 0
    )


def test_cluster_publication_rolls_back_then_retries_without_recomputing(
    cluster_project, monkeypatch
):
    import frisket.engine.executor.cluster_action as host
    import frisket.engine.executor.cluster_program as backend

    project, sheet, source, execute = cluster_project
    original = host._typed_receipt
    compute = backend.compute_reviewed_clusters
    calls = []

    def counted(*args, **kwargs):
        calls.append(True)
        return compute(*args, **kwargs)

    monkeypatch.setattr(backend, "compute_reviewed_clusters", counted)

    def broken(*args, **kwargs):
        raise RuntimeError("receipt failure")

    monkeypatch.setattr(host, "_typed_receipt", broken)
    with pytest.raises(RuntimeError, match="receipt failure"):
        execute()
    assert [column["name"] for column in project.columns(sheet)] == ["name"]
    assert (
        project.db.execute("SELECT count(*) FROM cell_result_heads").fetchone()[0] == 0
    )
    assert project.db.execute("SELECT status FROM receipts").fetchone()[0] == "running"
    monkeypatch.setattr(host, "_typed_receipt", original)
    result = execute()
    assert result.status == "completed", result.model_dump()
    assert len(calls) == 1


def test_actual_prepared_call_not_builtin_parameter_names_controls_source(
    cluster_project, monkeypatch
):
    from frisket.actions.cluster_types import (
        ClusterColumn,
        ClusterOptions,
        PreparedClustering,
        ValueClusterer,
    )
    from frisket.actions.types import ActionParams
    from frisket.actions.core import action, ActionCategory

    project, sheet, source, execute = cluster_project
    other = project.add_column(sheet, "other")

    class CustomParams(ActionParams):
        supplied: str

    def derive(params: CustomParams, clusterer: ValueClusterer) -> PreparedClustering:
        # No built-in source/method Params: the actual call owns its intent.
        return clusterer.prepare(
            ClusterColumn(params.supplied.lower()),
            options=ClusterOptions(method="fingerprint"),
        )

    definition = action(
        name="custom",
        title="Custom",
        description="Derived clustering",
        category=ActionCategory.CLEANUP,
        run=derive,
    )
    registered = RegisteredAction("custom.derived", definition)
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, registered.action_id: registered},
    )
    request = ActionRequest(
        action_id=registered.action_id,
        scope={"kind": "sheet_rows", "sheet_id": sheet},
        params={"supplied": "OTHER"},
        idempotency_key="derived",
    )
    result = run_typed_cluster_action(
        project,
        "test",
        BoundTypedActionRequest.bind(registered, request),
        None,
        _default_map_runner_factory,
    )
    assert result.status == "completed", result.model_dump()
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    fact = next(
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == "value_clusters"
    )
    assert fact["source"]["column_id"] == other


@pytest.mark.parametrize("values", [["Jon Smith", "Smith Jon", None, " Acme "], []])
def test_cluster_publishes_actual_groups_with_canonical_values(
    tmp_path, monkeypatch, values
):
    registered = RegisteredAction("custom.cluster", CLUSTER_VALUES)
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, "custom.cluster": registered},
    )
    project = Project.create(tmp_path / "cluster.frisket")
    try:
        sheet = project.add_sheet("Names")
        source = project.add_column(sheet, "name")
        rows = project.add_rows(
            sheet, [{"name": value} for value in values], {"name": source}
        )
        request = ActionRequest(
            action_id="custom.cluster",
            scope={"kind": "sheet_rows", "sheet_id": sheet},
            params={"source": "name"},
            output_names={"canonical": "Reviewed"},
            idempotency_key="cluster-actual",
        )
        bound = BoundTypedActionRequest.bind(registered, request)
        result = run_typed_cluster_action(
            project, "test", bound, None, _default_map_runner_factory
        )
        assert result.status == "completed", result.model_dump(mode="json")
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        [fact] = [
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "value_clusters"
        ]
        output = next(
            column for column in project.columns(sheet) if column["name"] == "Reviewed"
        )
        current = project.get_values(sheet, output["id"])
        assert fact["canonical_values"] == [
            {"row_id": row, "value": current.get(row)} for row in rows
        ]
        assert fact["source"]["column_id"] == source
        assert fact["output"]["column_id"] == output["id"]
        run = project.db.execute(
            "SELECT ops.spec FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.id=?",
            (result.run_id,),
        ).fetchone()
        assert json.loads(run["spec"])["value_clusters_result"] == fact
        assert len(fact["clusters"]) == (1 if values else 0)
        replay = run_typed_cluster_action(
            project, "test", bound, None, _default_map_runner_factory
        )
        assert replay.status == "completed", replay.model_dump()
        assert replay.receipt_id == result.receipt_id
    finally:
        project.close()
