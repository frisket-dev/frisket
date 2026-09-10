"""Crash-window regressions for the paid embedding.index_refresh batch call.

Hardening spec Lane 2.3: the refresh made its one batched provider call and
persisted the fact/vectors/receipt in later, separate steps — a crash between
them re-bought the whole batch on retry, and the fact carried NULL
``attempt_id``.  These tests pin the port onto the shared effect-checkpoint
store: the fact, cap spend, and replayable vectors are durable in one
transaction at provider return; a retry materializes without re-calling; an
ambiguous reservation refuses by name; local providers write no checkpoints.
"""

from __future__ import annotations

from typing import Any

import pytest

from frisket.ai.embeddings import EmbeddingStore, VectorBackend, build_batch_result
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store import Project
from frisket.team.security.secrets import encrypt_secret

PROJECT_ID = "project-embedding-effects"


class PaidRemoteGateway:
    """Deterministic fake of a paid remote embedder; records calls."""

    def __init__(self, dim: int = 1536):
        self.dim = dim
        self.calls: list[tuple] = []
        # Raised BEFORE the call is recorded: models a failure where the wire
        # was never reached (pre-egress).
        self.raise_error: Exception | None = None
        # Raised AFTER the call is recorded: models a post-egress failure —
        # the provider received (and may have billed) the request, but no
        # usable response came back.
        self.raise_after_record: Exception | None = None

    def embed(self, texts, *, provider, model, modality):
        if self.raise_error is not None:
            error, self.raise_error = self.raise_error, None
            raise error
        self.calls.append((list(texts), provider, model, modality))
        if self.raise_after_record is not None:
            error, self.raise_after_record = self.raise_after_record, None
            raise error
        vectors = [[float(i + 1)] + [0.0] * (self.dim - 1) for i in range(len(texts))]
        return build_batch_result(
            vectors,
            provider_id="openai",
            provider_kind="platform_api",
            requested_model=model,
            actual_model_id=model or "text-embedding-3-small",
            modality=modality,
            credential_source="project_key",
            provider_reported_cost_usd=0.001,
            provider_cost_usd=0.001,
            cost_source="pricing_data",
            usage={"input_count": len(texts)},
        )


class LocalGateway:
    def __init__(self, dim: int = 384):
        self.dim = dim
        self.calls: list[tuple] = []

    def embed(self, texts, *, provider, model, modality):
        self.calls.append((list(texts), provider, model, modality))
        vectors = [[1.0] + [0.0] * (self.dim - 1) for _ in texts]
        return build_batch_result(
            vectors,
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            requested_model=model,
            actual_model_id=model or "fake",
            modality=modality,
        )


@pytest.fixture()
def project(tmp_path):
    p = Project.create(tmp_path / "emb-effects.frisket", name="emb-effects")
    sheet = p.add_sheet("data")
    cols = {"headline": p.add_column(sheet, "headline")}
    p.add_rows(sheet, [{"headline": f"story {i}"} for i in range(3)], cols)
    p._sheet_id = sheet  # type: ignore[attr-defined]
    try:
        yield p
    finally:
        p.close()


def _run(project, data, *, gateway):
    return run_action_spec(
        project,
        data,
        project_id=PROJECT_ID,
        deps=ExecutorDeps(embedding_gateway=gateway),
    )


def _create_remote_index(project, *, gateway) -> str:
    result = _run(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": project._sheet_id,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": "openai",
                "source_policy": {"kind": "text_cell"},
                "provider_policy": {"allow_remote": True},
            },
            "idempotency_key": "emb_effects_create@1",
        },
        gateway=gateway,
    )
    assert result.status == "completed", result.errors
    return result.outputs[0].ref["index_id"]


def _refresh_action(index_id: str, *, key: str) -> dict[str, Any]:
    return {
        "action_id": "embedding.index_refresh",
        "scope": {"kind": "project"},
        "params": {"index_id": index_id, "mode": "incremental"},
        "idempotency_key": key,
    }


def test_returned_embedding_checkpoint_accounts_spend_before_result_commit(
    project, monkeypatch
) -> None:
    """The embedding twin of the MapRunner regression: crash after the batch
    returned, before the terminal receipt — the fact, cap spend, and
    replayable vectors are already durable under the authorizing attempt, and
    the retry finalizes the index without calling the provider again."""

    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-project-openai"),
        hint="sk-p***",
        spend_cap_micro=50_000,
    )
    gateway = PaidRemoteGateway()
    index_id = _create_remote_index(project, gateway=gateway)

    real_upsert = VectorBackend.upsert_item
    crashes = [True]

    def crash_once_before_vector_commit(self, **kwargs):
        if crashes[0]:
            crashes[0] = False
            raise RuntimeError("injected crash after accounted provider return")
        return real_upsert(self, **kwargs)

    # The exact vulnerable boundary: the provider has returned and charged,
    # but neither the sidecar vectors nor the terminal receipt exist yet.
    monkeypatch.setattr(VectorBackend, "upsert_item", crash_once_before_vector_commit)

    with pytest.raises(RuntimeError, match="after accounted provider return"):
        _run(
            project,
            _refresh_action(index_id, key="emb_effects_refresh@r1"),
            gateway=gateway,
        )
    assert len(gateway.calls) == 1

    # The batch invoice is durable truth already: the fact under the
    # authorizing attempt, project-key cap spend, run cost — and the vectors
    # are replayable from the checkpoint even though no receipt committed.
    run_id = int(project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0])
    attempt = project.db.execute(
        "SELECT id, state FROM execution_attempts WHERE run_id=?", (run_id,)
    ).fetchone()
    assert attempt is not None
    call = project.db.execute(
        "SELECT attempt_id, provider_cost_usd, credential_source "
        "FROM model_calls WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert call is not None
    assert call["attempt_id"] == attempt["id"]
    assert call["provider_cost_usd"] == pytest.approx(0.001)
    assert call["credential_source"] == "project_key"
    assert project.provider_spend_state("openai").spent_micro == 1_000
    checkpoint = project.db.execute(
        "SELECT state, authorized_attempt_id FROM effect_checkpoints"
    ).fetchone()
    assert checkpoint is not None
    assert checkpoint["state"] == "returned"
    assert checkpoint["authorized_attempt_id"] == attempt["id"]
    assert (
        EmbeddingStore(project).get_index(index_id)["last_refresh_receipt_id"] is None
    )

    # Retry: the durable response replays, the index finalizes, and the
    # provider is NOT called again.
    resumed = _run(
        project,
        _refresh_action(index_id, key="emb_effects_refresh@r2"),
        gateway=gateway,
    )
    assert resumed.status == "completed", resumed.errors
    assert len(gateway.calls) == 1
    assert resumed.outputs[0].ref["refreshed"] == 3
    assert resumed.outputs[0].ref["ready_items"] == 3
    backend = VectorBackend(project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {"ready": 3}
    backend.close()
    # One fact, one accounting run, spend counted once; checkpoint retired.
    assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
    assert project.provider_spend_state("openai").spent_micro == 1_000
    assert resumed.run_id == run_id
    run = project.db.execute(
        "SELECT status, cost_actual FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert run["status"] == "completed"
    assert run["cost_actual"] == pytest.approx(0.001)
    assert (
        project.db.execute("SELECT COUNT(*) FROM effect_checkpoints").fetchone()[0] == 0
    )


def test_reserved_embedding_checkpoint_refuses_resume(project) -> None:
    """An unclassified in-flight failure leaves the reservation standing (a
    provider may have been reached), and the next refresh refuses by name
    without another egress."""

    gateway = PaidRemoteGateway()
    index_id = _create_remote_index(project, gateway=gateway)

    gateway.raise_error = RuntimeError("connection dropped mid-flight")
    with pytest.raises(RuntimeError, match="connection dropped mid-flight"):
        _run(
            project,
            _refresh_action(index_id, key="emb_effects_refresh@r1"),
            gateway=gateway,
        )
    assert gateway.calls == []
    checkpoint = project.db.execute("SELECT state FROM effect_checkpoints").fetchone()
    assert checkpoint is not None and checkpoint["state"] == "reserved"

    refused = _run(
        project,
        _refresh_action(index_id, key="emb_effects_refresh@r2"),
        gateway=gateway,
    )
    assert refused.status == "failed"
    assert refused.errors[0].code == "external_effect_reconciliation_required"
    assert gateway.calls == []


def test_backend_unavailable_discards_and_stays_retryable(project) -> None:
    """``EmbeddingBackendUnavailable`` is raised before any wire I/O, so the
    discard contract's "egress did not start" is provable: the reservation is
    discarded and the retry buys the batch fresh."""

    from frisket.ai.embeddings import EmbeddingBackendUnavailable

    gateway = PaidRemoteGateway()
    index_id = _create_remote_index(project, gateway=gateway)

    gateway.raise_error = EmbeddingBackendUnavailable("no adapter for openai")
    failed = _run(
        project,
        _refresh_action(index_id, key="emb_effects_refresh@r1"),
        gateway=gateway,
    )
    assert failed.status == "failed"
    assert failed.errors[0].code == "embedding_backend_unavailable"
    assert gateway.calls == []
    assert (
        project.db.execute("SELECT COUNT(*) FROM effect_checkpoints").fetchone()[0] == 0
    )

    retried = _run(
        project,
        _refresh_action(index_id, key="emb_effects_refresh@r2"),
        gateway=gateway,
    )
    assert retried.status == "completed", retried.errors
    assert len(gateway.calls) == 1


def test_provider_error_after_egress_replays_by_name_without_rebuying(
    project,
) -> None:
    """``EmbeddingProviderError`` is post-egress on every reachable remote
    path (HTTP error responses, post-send timeouts, post-acceptance parse
    failures): the failure completes the checkpoint as returned-with-error,
    and resume replays the same named error at zero provider cost instead of
    re-buying or discarding billable evidence."""

    from frisket.ai.embeddings import EmbeddingProviderError

    gateway = PaidRemoteGateway()
    index_id = _create_remote_index(project, gateway=gateway)

    gateway.raise_after_record = EmbeddingProviderError("provider 503")
    failed = _run(
        project,
        _refresh_action(index_id, key="emb_effects_refresh@r1"),
        gateway=gateway,
    )
    assert failed.status == "failed"
    assert failed.errors[0].code == "embedding_provider_error"
    assert len(gateway.calls) == 1
    # The post-egress failure is durable truth: returned-with-error, no
    # vectors, and no fact (the error carries nothing a fact could honestly
    # be derived from).
    checkpoint = project.db.execute(
        "SELECT state, payload FROM effect_checkpoints"
    ).fetchone()
    assert checkpoint is not None
    assert checkpoint["state"] == "returned"
    assert '"error"' in checkpoint["payload"]
    assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0

    # Resume: the same named error replays; the provider is NOT called again
    # and the durable failure record survives.
    replayed = _run(
        project,
        _refresh_action(index_id, key="emb_effects_refresh@r2"),
        gateway=gateway,
    )
    assert replayed.status == "failed"
    assert replayed.errors[0].code == "embedding_provider_error"
    assert "provider 503" in replayed.errors[0].message
    assert replayed.errors[0].details.get("replayed_effect_error") is True
    assert len(gateway.calls) == 1
    still = project.db.execute("SELECT state FROM effect_checkpoints").fetchone()
    assert still is not None and still["state"] == "returned"


def test_local_embedding_refresh_makes_no_checkpoints(project) -> None:
    """A local-provider refresh spends nothing and must produce zero durable
    checkpoint writes while still closing its claimless execution attempt."""

    gateway = LocalGateway()
    result = _run(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": project._sheet_id,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": "fastembed",
                "source_policy": {"kind": "text_cell"},
                "provider_policy": {"allow_remote": False},
            },
            "idempotency_key": "emb_effects_local_create@1",
        },
        gateway=gateway,
    )
    assert result.status == "completed", result.errors
    index_id = result.outputs[0].ref["index_id"]

    refreshed = _run(
        project,
        _refresh_action(index_id, key="emb_effects_local_refresh@1"),
        gateway=gateway,
    )
    assert refreshed.status == "completed", refreshed.errors
    assert len(gateway.calls) == 1
    assert (
        project.db.execute("SELECT COUNT(*) FROM effect_checkpoints").fetchone()[0] == 0
    )
    attempt = project.db.execute(
        "SELECT state FROM execution_attempts ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert attempt is not None
    assert attempt["state"] == "effected"


def test_local_embedding_terminal_write_refuses_a_replaced_writer(
    project,
    monkeypatch,
) -> None:
    """Replacement after the local vector write fences terminal project state."""

    from frisket.engine.executor.action_families import embeddings
    from frisket.execution.attempt import StaleAttemptWriter

    gateway = LocalGateway()
    created = _run(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": project._sheet_id,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": "fastembed",
                "source_policy": {"kind": "text_cell"},
                "provider_policy": {"allow_remote": False},
            },
            "idempotency_key": "emb_effects_local_terminal_create@1",
        },
        gateway=gateway,
    )
    assert created.status == "completed", created.errors
    index_id = created.outputs[0].ref["index_id"]
    real_refresh_receipt = embeddings._refresh_receipt
    captured: dict[str, Any] = {}

    def replace_after_receipt_is_built(**kwargs):
        receipt = real_refresh_receipt(**kwargs)
        run_id = int(kwargs["run_id"])
        attempt = project.db.execute(
            "SELECT id, seq FROM execution_attempts "
            "WHERE run_id=? AND state='dispatching'",
            (run_id,),
        ).fetchone()
        assert attempt is not None
        captured["run_id"] = run_id
        captured["attempt_a"] = str(attempt["id"])
        captured["index_before"] = {
            key: EmbeddingStore(project).get_index(index_id)[key]
            for key in (
                "total_items",
                "ready_items",
                "stale_items",
                "error_items",
                "last_refresh_receipt_id",
            )
        }
        captured["receipt_count"] = project.db.execute(
            "SELECT COUNT(*) FROM receipts WHERE id=?",
            (receipt.receipt_id,),
        ).fetchone()[0]
        project.db.execute("BEGIN IMMEDIATE")
        project.db.execute(
            "UPDATE execution_attempts SET state='abandoned' WHERE id=?",
            (attempt["id"],),
        )
        project.db.execute(
            "INSERT INTO execution_attempts "
            "(id, run_id, seq, state, action_identity_hash, scope_json, created_at) "
            "VALUES ('attempt_embedding_b', ?, ?, 'dispatching', "
            "'test-replacement', '[]', datetime('now'))",
            (run_id, int(attempt["seq"]) + 1),
        )
        project.db.execute(
            "UPDATE runs SET current_attempt_id='attempt_embedding_b' WHERE id=?",
            (run_id,),
        )
        project.db.commit()
        return receipt

    monkeypatch.setattr(embeddings, "_refresh_receipt", replace_after_receipt_is_built)

    with pytest.raises(StaleAttemptWriter) as stale:
        _run(
            project,
            _refresh_action(
                index_id,
                key="emb_effects_local_terminal_refresh@1",
            ),
            gateway=gateway,
        )

    assert captured, str(stale.value)
    run_id = captured["run_id"]
    assert (
        project.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[
            0
        ]
        == "running"
    )
    assert (
        project.db.execute(
            "SELECT current_attempt_id FROM runs WHERE id=?", (run_id,)
        ).fetchone()[0]
        == "attempt_embedding_b"
    )
    assert (
        project.db.execute(
            "SELECT state FROM execution_attempts WHERE id='attempt_embedding_b'"
        ).fetchone()[0]
        == "dispatching"
    )
    assert (
        project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?",
            (captured["attempt_a"],),
        ).fetchone()[0]
        == "abandoned"
    )
    assert {
        key: EmbeddingStore(project).get_index(index_id)[key]
        for key in (
            "total_items",
            "ready_items",
            "stale_items",
            "error_items",
            "last_refresh_receipt_id",
        )
    } == captured["index_before"]
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM receipts WHERE action_kind='embedding.index_refresh'"
        ).fetchone()[0]
        == captured["receipt_count"]
    )


def test_embedding_terminalizer_joins_caller_transaction(
    project,
    monkeypatch,
) -> None:
    """A cut after the kernel rolls back project txn B, not sidecar txn A."""

    from frisket.engine.executor.action_families import embeddings
    from frisket.execution.attempt import StaleAttemptWriter

    gateway = PaidRemoteGateway()
    index_id = _create_remote_index(project, gateway=gateway)
    before_index = {
        key: EmbeddingStore(project).get_index(index_id)[key]
        for key in (
            "total_items",
            "ready_items",
            "stale_items",
            "error_items",
            "last_refresh_receipt_id",
        )
    }
    real_terminalize = embeddings.terminalize_project_run
    terminalizations: list[Any] = []

    def cut_after_join(*args, **kwargs):
        assert project.db.in_transaction
        assert kwargs["commit"] is False
        result = real_terminalize(*args, **kwargs)
        terminalizations.append(result)
        assert project.db.in_transaction
        assert (
            project.db.execute(
                "SELECT status FROM runs WHERE id=?",
                (kwargs["run_id"],),
            ).fetchone()["status"]
            == "completed"
        )
        assert (
            project.db.execute(
                "SELECT status FROM receipts WHERE id=?",
                (kwargs["receipt_id"],),
            ).fetchone()["status"]
            == "completed"
        )
        assert (
            project.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (kwargs["authority"].writer_attempt_id,),
            ).fetchone()["state"]
            == "effected"
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM effect_checkpoints").fetchone()[0]
            == 0
        )
        assert (
            EmbeddingStore(project).get_index(index_id)["last_refresh_receipt_id"]
            == kwargs["receipt_id"]
        )
        raise StaleAttemptWriter("injected after joined terminalization")

    monkeypatch.setattr(embeddings, "terminalize_project_run", cut_after_join)

    with pytest.raises(StaleAttemptWriter, match="after joined terminalization"):
        _run(
            project,
            _refresh_action(
                index_id,
                key="emb_effects_terminal_join_refresh@1",
            ),
            gateway=gateway,
        )

    assert len(gateway.calls) == 1
    assert len(terminalizations) == 1
    terminalization = terminalizations[0]
    assert terminalization.disposition == "terminalized"
    assert terminalization.receipt_disposition == "inserted"

    run = project.db.execute(
        "SELECT id, status, finished_at, current_attempt_id FROM runs "
        "WHERE action_kind='embedding.index_refresh' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert run is not None
    run_id = int(run["id"])
    attempt_id = str(run["current_attempt_id"])
    assert run["status"] == "running"
    assert run["finished_at"] is None
    assert attempt_id
    assert (
        project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()["state"]
        == "dispatching"
    )

    checkpoint = project.db.execute(
        "SELECT state, authorized_attempt_id FROM effect_checkpoints"
    ).fetchone()
    assert checkpoint is not None
    assert checkpoint["state"] == "returned"
    assert checkpoint["authorized_attempt_id"] == attempt_id
    assert {
        key: EmbeddingStore(project).get_index(index_id)[key]
        for key in (
            "total_items",
            "ready_items",
            "stale_items",
            "error_items",
            "last_refresh_receipt_id",
        )
    } == before_index
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM receipts WHERE action_kind='embedding.index_refresh'"
        ).fetchone()[0]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        == 0
    )

    backend = VectorBackend(project)
    backend.ensure_schema()
    try:
        assert backend.item_counts(index_id) == {"ready": 3}
    finally:
        backend.close()
