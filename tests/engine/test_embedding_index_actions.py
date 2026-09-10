"""V1 embedding index lifecycle actions (handoff Lane 3).

Covers embedding.index_create / index_refresh / index_delete end to end through
the public action executor with a FAKE gateway (no real model). Proves: create is
metadata-only + idempotent; refresh embeds rows and writes sidecar vectors;
incremental skips current rows and re-embeds stale ones; full rebuilds; unknown
index/space fail before mutation; concurrent refresh is rejected busy; a
remote-backed index without policy never egresses; delete removes metadata +
vectors and drops only orphan spaces.
"""

from __future__ import annotations


import pytest

from frisket.actions.system import root_action_catalog, validate_root_action
from frisket.ai.embeddings import EmbeddingStore, VectorBackend, build_batch_result
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.team.security.secrets import encrypt_secret
from helpers import replace_test_source_cell, write_claimless_test_model_calls

PROJECT_ID = "project-embeddings"


# --------------------------------------------------------------------------
# fakes + fixtures
# --------------------------------------------------------------------------


class FakeGateway:
    """Deterministic in-memory embedder. Records calls so tests can assert that
    a blocked/remote refresh never egresses."""

    def __init__(self, dim: int = 384):
        self.dim = dim
        self.calls: list[tuple] = []

    def embed(self, texts, *, provider, model, modality):
        self.calls.append((list(texts), provider, model, modality))
        vectors = [
            [float((abs(hash(t)) % 97) + 1)] + [0.0] * (self.dim - 1) for t in texts
        ]
        return build_batch_result(
            vectors,
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            requested_model=model,
            actual_model_id=model or "fake",
            modality=modality,
        )


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "emb.frisket", name="emb")
    sheet = p.add_sheet("data")
    cols = {"headline": p.add_column(sheet, "headline")}
    p.add_rows(
        sheet,
        [{"headline": f"story {i}"} for i in range(3)],
        cols,
    )
    p._sheet_id = sheet  # type: ignore[attr-defined]
    p._cols = cols  # type: ignore[attr-defined]
    yield p
    p.close()


def _create_action(sheet_id, *, provider="fastembed", key="emb_create@1", policy=None):
    params = {
        "sheet_id": sheet_id,
        "source_columns": ["headline"],
        "modality": "text",
        "provider": provider,
        "source_policy": {"kind": "text_cell"},
        "provider_policy": policy if policy is not None else {"allow_remote": False},
    }
    return {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": key,
    }


def _refresh_action(index_id, *, mode="incremental", key="emb_refresh@1"):
    return {
        "action_id": "embedding.index_refresh",
        "scope": {"kind": "project"},
        "params": {"index_id": index_id, "mode": mode},
        "idempotency_key": key,
    }


def _delete_action(index_id, *, key="emb_delete@1"):
    return {
        "action_id": "embedding.index_delete",
        "scope": {"kind": "project"},
        "params": {"index_id": index_id},
        "idempotency_key": key,
    }


def _run(project, data, *, gateway=None):
    deps = ExecutorDeps(embedding_gateway=gateway) if gateway is not None else None
    return run_action_spec(project, data, project_id=PROJECT_ID, deps=deps)


def _create(project, **kw):
    result = _run(project, _create_action(project._sheet_id, **kw))
    assert result.status == "completed", result.errors
    return result.outputs[0].ref["index_id"]


# --------------------------------------------------------------------------
# catalog + validation
# --------------------------------------------------------------------------


def test_catalog_entries_present_with_capabilities():
    catalog = root_action_catalog()
    by_kind = {e.kind: e for e in catalog.actions}
    assert by_kind["embedding.index_create"].required_capabilities == [
        "project:write",
        "model:embed",
    ]
    refresh = by_kind["embedding.index_refresh"]
    assert refresh.required_capabilities == ["project:write", "model:embed"]
    refresh_codes = {e.code for e in refresh.errors}
    assert {
        "embedding_index_not_found",
        "embedding_index_busy",
        "embedding_remote_confirmation_required",
        "embedding_space_mismatch",
    } <= refresh_codes

    create = by_kind["embedding.index_create"]
    assert create.input_schema["properties"]["source_columns"]["type"] == "array"
    assert create.cost_policy.kind == "model_metered"
    assert "paid" in create.description


def test_validation_rejects_caller_authored_capabilities():
    action = _refresh_action("embidx_x")
    action["capabilities"] = ["project:write"]  # missing model:embed
    result = validate_root_action(action)
    assert result.ok is False
    assert result.error.code == "invalid_action_request"


def test_validation_rejects_unknown_modality():
    action = _create_action(1)
    action["params"]["modality"] = "hologram"
    result = validate_root_action(action)
    assert result.ok is False
    assert result.error.code == "invalid_action_request"
    assert "modality" in result.error.message


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------


def test_create_writes_space_and_index_metadata_only(project):
    index_id = _create(project)
    store = EmbeddingStore(project)
    idx = store.get_index(index_id)
    assert idx is not None
    space = store.get_space(idx["space_id"])
    assert space["provider_id"] == "fastembed"
    assert space["modality"] == "text"
    assert space["dimension"] == 384
    # metadata only — no sidecar vectors yet
    backend = VectorBackend(project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {}
    backend.close()


def test_create_is_idempotent_by_key(project):
    a = _run(project, _create_action(project._sheet_id, key="same"))
    b = _run(project, _create_action(project._sheet_id, key="same"))
    assert a.outputs[0].ref["index_id"] == b.outputs[0].ref["index_id"]
    n = project.db.execute("SELECT COUNT(*) AS n FROM embedding_indexes").fetchone()[
        "n"
    ]
    assert n == 1


def test_distinct_creates_share_one_space(project):
    _create(project, key="k1")
    _create(project, key="k2")
    indexes = project.db.execute(
        "SELECT COUNT(*) AS n FROM embedding_indexes"
    ).fetchone()
    spaces = project.db.execute("SELECT COUNT(*) AS n FROM embedding_spaces").fetchone()
    assert indexes["n"] == 2
    assert spaces["n"] == 1  # same provider/model/modality → one space


def test_create_unknown_provider_fails(project):
    result = _run(project, _create_action(project._sheet_id, provider="nonexistent"))
    assert result.status == "failed"
    assert result.errors[0].code == "embedding_backend_unavailable"
    assert (
        project.db.execute("SELECT COUNT(*) AS n FROM embedding_indexes").fetchone()[
            "n"
        ]
        == 0
    )


# --------------------------------------------------------------------------
# refresh
# --------------------------------------------------------------------------


def test_refresh_embeds_rows_and_writes_sidecar_vectors(project):
    index_id = _create(project)
    gw = FakeGateway()
    result = _run(project, _refresh_action(index_id), gateway=gw)
    assert result.status == "completed", result.errors
    ref = result.outputs[0].ref
    assert ref["refreshed"] == 3
    assert ref["skipped_current"] == 0
    assert ref["ready_items"] == 3
    assert ref["backend_id"] == VectorBackend.BACKEND_ID
    backend = VectorBackend(project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {"ready": 3}
    backend.close()
    # one batched provider call for the three rows
    assert len(gw.calls) == 1 and len(gw.calls[0][0]) == 3
    # index counts persisted in project.db
    idx = EmbeddingStore(project).get_index(index_id)
    assert idx["ready_items"] == 3 and idx["status"] == "ready"
    assert idx["last_refresh_receipt_id"] == result.receipt_id


def test_incremental_skips_current_and_refreshes_stale(project):
    index_id = _create(project)
    _run(project, _refresh_action(index_id, key="r1"), gateway=FakeGateway())
    # second incremental with no changes → everything skipped
    gw = FakeGateway()
    again = _run(project, _refresh_action(index_id, key="r2"), gateway=gw)
    assert again.outputs[0].ref == {
        **again.outputs[0].ref,
        "refreshed": 0,
        "skipped_current": 3,
    }
    assert gw.calls == []  # nothing re-embedded

    # change one source cell → its source_hash changes → only that row refreshes
    sheet_id, col_id = project._sheet_id, project._cols["headline"]
    row_id = project.visible_row_ids(sheet_id)[0]
    replace_test_source_cell(
        project,
        row_id=row_id,
        column_id=col_id,
        value="rewritten story",
    )
    gw2 = FakeGateway()
    stale = _run(project, _refresh_action(index_id, key="r3"), gateway=gw2)
    assert stale.outputs[0].ref["refreshed"] == 1
    assert stale.outputs[0].ref["skipped_current"] == 2
    assert len(gw2.calls[0][0]) == 1


def test_full_mode_rebuilds_all(project):
    index_id = _create(project)
    _run(project, _refresh_action(index_id, key="r1"), gateway=FakeGateway())
    gw = FakeGateway()
    full = _run(project, _refresh_action(index_id, mode="full", key="r2"), gateway=gw)
    assert full.outputs[0].ref["refreshed"] == 3
    assert full.outputs[0].ref["skipped_current"] == 0
    assert len(gw.calls[0][0]) == 3


def test_refresh_unknown_index_fails_before_mutation(project):
    result = _run(project, _refresh_action("embidx_missing"), gateway=FakeGateway())
    assert result.status == "failed"
    assert result.errors[0].code == "embedding_index_not_found"


def test_refresh_dimension_mismatch_is_rejected(project):
    index_id = _create(project)
    # gateway returns 3-d vectors but the fastembed space is 384-d
    result = _run(project, _refresh_action(index_id), gateway=FakeGateway(dim=3))
    assert result.status == "failed"
    assert result.errors[0].code == "embedding_space_mismatch"
    backend = VectorBackend(project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {}  # nothing written
    backend.close()


def test_concurrent_refresh_is_busy(project):
    index_id = _create(project)
    store = EmbeddingStore(project)
    held = store.acquire_refresh_claim(index_id)  # simulate an in-flight refresh
    assert held is not None
    result = _run(project, _refresh_action(index_id), gateway=FakeGateway())
    assert result.status == "failed"
    assert result.errors[0].code == "embedding_index_busy"
    store.release_refresh_claim(index_id, held)


def test_refresh_claim_released_after_success(project):
    index_id = _create(project)
    _run(project, _refresh_action(index_id, key="r1"), gateway=FakeGateway())
    # claim released → a second refresh can acquire and run
    second = _run(project, _refresh_action(index_id, key="r2"), gateway=FakeGateway())
    assert second.status == "completed"


# --------------------------------------------------------------------------
# remote egress gate
# --------------------------------------------------------------------------


def test_remote_index_without_policy_does_not_egress(project):
    index_id = _create(project, provider="openai", policy={"allow_remote": False})
    gw = FakeGateway(dim=1536)
    result = _run(project, _refresh_action(index_id), gateway=gw)
    assert result.status == "failed"
    assert result.errors[0].code == "embedding_remote_confirmation_required"
    assert gw.calls == []  # the provider was never called — no data left
    backend = VectorBackend(project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {}
    backend.close()


def test_remote_index_with_policy_allows_refresh(project):
    index_id = _create(project, provider="openai", policy={"allow_remote": True})
    gw = FakeGateway(dim=1536)
    result = _run(project, _refresh_action(index_id), gateway=gw)
    assert result.status == "completed", result.errors
    assert result.outputs[0].ref["refreshed"] == 3
    assert len(gw.calls) == 1


def test_over_cap_project_key_blocks_remote_embedding_refresh_before_egress(project):
    """The project-key spend cap covers embeddings as well as chat recipes."""
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-project-openai"),
        hint="sk-p***",
        spend_cap_micro=500,
    )
    index_id = _create(
        project,
        provider="openai",
        policy={"allow_remote": True},
        key="emb_create@over-cap",
    )

    # Exhaust the key through the production accrual path.
    row_id = project.visible_row_ids(project._sheet_id)[0]
    column_id = project._cols["headline"]
    op_id = project.append_op(
        "map", {"recipe": "prior_provider_spend"}, label="prior spend"
    )
    prior_run_id = RunResultStore(project).start_run(
        op_id, project._sheet_id, "test.prior_provider_spend"
    )
    write_claimless_test_model_calls(
        project,
        prior_run_id,
        [
            {
                "row_id": row_id,
                "column_id": column_id,
                "model_calls": [
                    {
                        "fact_version": "frisket.model-call-fact.v1",
                        "capability": "llm.embed",
                        "engine": "openai/text-embedding-3-small",
                        "provider": "openai",
                        "provider_kind": "platform_api",
                        "credential_source": "project_key",
                        "provider_reported_cost_usd": 0.001,
                        "provider_cost_usd": 0.001,
                        "cost_source": "pricing_data",
                        "units": {"input_count": 3},
                    }
                ],
            }
        ],
    )
    project.db.commit()
    assert project.provider_spend_state("openai").over_cap

    class PaidRemoteGateway(FakeGateway):
        def __init__(self):
            super().__init__(dim=1536)

        def embed(self, texts, *, provider, model, modality):
            self.calls.append((list(texts), provider, model, modality))
            vectors = [[1.0] + [0.0] * 1535 for _ in texts]
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

    gateway = PaidRemoteGateway()
    result = _run(project, _refresh_action(index_id), gateway=gateway)

    assert result.status == "failed"
    assert result.errors[0].code == "provider_spend_cap_exceeded"
    assert gateway.calls == []


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------


def test_delete_removes_index_and_sidecar_vectors(project):
    index_id = _create(project)
    _run(project, _refresh_action(index_id), gateway=FakeGateway())
    result = _run(project, _delete_action(index_id))
    assert result.status == "completed"
    assert result.outputs[0].ref["deleted_items"] == 3
    assert result.outputs[0].ref["space_deleted"] is True
    assert EmbeddingStore(project).get_index(index_id) is None
    assert (
        project.db.execute("SELECT COUNT(*) AS n FROM embedding_spaces").fetchone()["n"]
        == 0
    )
    backend = VectorBackend(project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {}
    backend.close()


def test_delete_keeps_space_shared_by_another_index(project):
    a = _create(project, key="k1")
    b = _create(project, key="k2")  # shares the same space
    space_id = EmbeddingStore(project).get_index(a)["space_id"]
    result = _run(project, _delete_action(a))
    assert result.outputs[0].ref["space_deleted"] is False
    # the shared space survives because index b still references it
    assert EmbeddingStore(project).get_space(space_id) is not None
    assert EmbeddingStore(project).get_index(b) is not None


def test_delete_unknown_index_fails(project):
    result = _run(project, _delete_action("embidx_missing"))
    assert result.status == "failed"
    assert result.errors[0].code == "embedding_index_not_found"


# --------------------------------------------------------------------------
# Refresh honors stored source_query + params.row_scope
# --------------------------------------------------------------------------


def _sheet_filter(sheet_id, filter_spec):
    return {
        "schema_version": "frisket.query.v1",
        "kind": "sheet.filter",
        "scope": {"kind": "sheet", "sheet_id": sheet_id},
        "filter": filter_spec,
    }


def _create_with_query(project, source_query, key="emb_create@q"):
    action = _create_action(project._sheet_id, key=key)
    action["params"]["source_query"] = source_query
    result = _run(project, action)
    assert result.status == "completed", result.errors
    return result.outputs[0].ref["index_id"]


def _row_id_for(project, text):
    sheet_id, col = project._sheet_id, project._cols["headline"]
    values = project.get_values(sheet_id, col)
    return next(rid for rid, v in values.items() if v == text)


def test_refresh_honors_stored_source_query(project):
    # only the row whose headline contains "story 1" should be embedded
    index_id = _create_with_query(
        project, _sheet_filter(project._sheet_id, {"headline": {"contains": "story 1"}})
    )
    gw = FakeGateway()
    result = _run(project, _refresh_action(index_id), gateway=gw)
    assert result.status == "completed", result.errors
    assert result.outputs[0].ref["refreshed"] == 1
    assert len(gw.calls[0][0]) == 1
    backend = VectorBackend(project)
    backend.ensure_schema()
    assert set(backend.item_counts(index_id)) == {"ready"}
    assert backend.get_item(index_id, str(_row_id_for(project, "story 1"))) is not None
    backend.close()


def test_row_scope_narrows_stored_scope(project):
    # stored scope = all rows; row_scope narrows to "story 2" by intersection
    index_id = _create(project)
    action = _refresh_action(index_id)
    action["params"]["row_scope"] = _sheet_filter(
        project._sheet_id, {"headline": {"contains": "story 2"}}
    )
    gw = FakeGateway()
    result = _run(project, action, gateway=gw)
    assert result.status == "completed", result.errors
    assert result.outputs[0].ref["refreshed"] == 1
    backend = VectorBackend(project)
    backend.ensure_schema()
    assert backend.get_item(index_id, str(_row_id_for(project, "story 2"))) is not None
    backend.close()


def test_refresh_unsupported_query_kind_fails_before_claim(project):
    fts = {
        "schema_version": "frisket.query.v1",
        "kind": "search.fts",
        "scope": {"kind": "sheet", "sheet_id": project._sheet_id},
        "q": "story",
    }
    index_id = _create_with_query(project, fts)
    gw = FakeGateway()
    result = _run(project, _refresh_action(index_id), gateway=gw)
    assert result.status == "failed"
    assert result.errors[0].code == "embedding_source_unsupported"
    assert gw.calls == []  # no provider call
    # claim was never taken — the index is not stuck 'refreshing'
    assert EmbeddingStore(project).get_index(index_id)["status"] != "refreshing"
    backend = VectorBackend(project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {}
    backend.close()


def test_refresh_query_sheet_mismatch_fails_before_claim(project):
    index_id = _create_with_query(
        project,
        _sheet_filter(project._sheet_id + 1, {"headline": {"contains": "story"}}),
    )
    gw = FakeGateway()
    result = _run(project, _refresh_action(index_id), gateway=gw)
    assert result.status == "failed"
    assert result.errors[0].code == "invalid_query_spec"
    assert gw.calls == []
    assert EmbeddingStore(project).get_index(index_id)["status"] != "refreshing"


# --------------------------------------------------------------------------
# Create validates sheet + columns before any write
# --------------------------------------------------------------------------


def _assert_no_metadata(project):
    for table in ("embedding_spaces", "embedding_indexes", "receipts"):
        n = project.db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        assert n == 0, f"{table} should be empty, found {n}"


def test_create_unknown_sheet_fails_with_no_writes(project):
    action = _create_action(999999, key="bad-sheet")
    result = _run(project, action)
    assert result.status == "failed"
    assert result.errors[0].code == "invalid_input_ref"
    assert result.errors[0].field == "params.sheet_id"
    _assert_no_metadata(project)


def test_create_missing_source_column_fails_with_no_writes(project):
    action = _create_action(project._sheet_id, key="bad-col")
    action["params"]["source_columns"] = ["not_a_column"]
    result = _run(project, action)
    assert result.status == "failed"
    assert result.errors[0].code == "invalid_input_ref"
    assert result.errors[0].field == "params.source_columns"
    _assert_no_metadata(project)


def test_create_source_columns_are_names_not_numeric_ids(project):
    action = _create_action(project._sheet_id, key="numeric-id-is-not-a-name")
    action["params"]["source_columns"] = [str(project._cols["headline"])]

    result = _run(project, action)

    assert result.status == "failed"
    assert result.errors[0].code == "invalid_input_ref"
    assert result.errors[0].details == {"missing": [str(project._cols["headline"])]}
    _assert_no_metadata(project)


# --------------------------------------------------------------------------
# Metadata + receipt are one transaction
# --------------------------------------------------------------------------


def test_create_receipt_failure_rolls_back_metadata(project, monkeypatch):
    import frisket.engine.executor.action_families.embeddings as fam

    def boom(*_a, **_k):
        raise RuntimeError("simulated receipt insert failure")

    monkeypatch.setattr(fam, "_insert_receipt", boom)
    result = _run(project, _create_action(project._sheet_id, key="rollback"))
    assert result.status == "failed"
    assert result.errors[0].code == "project_write_failed"
    # space + index were inside the same transaction as the receipt → all undone
    _assert_no_metadata(project)


def test_refresh_does_not_set_receipt_id_when_receipt_fails(project, monkeypatch):
    import frisket.engine.executor.action_families.embeddings as fam

    index_id = _create(project)

    def boom(*_a, **_k):
        raise RuntimeError("simulated receipt insert failure")

    # Run-bearing refresh receipt insertion now belongs to the shared
    # terminal kernel; inject at the durable store seam it calls.
    monkeypatch.setattr(fam.ReceiptStore, "insert", boom)
    result = _run(project, _refresh_action(index_id), gateway=FakeGateway())
    assert result.status == "failed"
    assert result.errors[0].code == "project_write_failed"
    idx = EmbeddingStore(project).get_index(index_id)
    # last_refresh_receipt_id commits in the same txn as the receipt → never set
    assert idx["last_refresh_receipt_id"] is None
    # the claim is released, not stuck refreshing
    assert idx["status"] != "refreshing"


def test_refresh_repair_recounts_sidecar_without_marking_refreshed(project):
    from frisket.engine.executor.action_families.embeddings import (
        repair_unfinalized_refresh,
    )

    index_id = _create(project)
    idx = EmbeddingStore(project).get_index(index_id)
    assert idx["last_refresh_receipt_id"] is None
    assert idx["last_refreshed_at"] is None

    row_id = int(project.visible_row_ids(project._sheet_id)[0])
    backend = VectorBackend(project)
    backend.ensure_schema()
    backend.upsert_item(
        index_id=index_id,
        space_id=idx["space_id"],
        source_key=str(row_id),
        source_ref={"kind": "row", "row_id": row_id},
        source_hash="sha256:written-before-receipt",
        status="ready",
        vector=[1.0] + [0.0] * 383,
    )
    backend.close()

    assert repair_unfinalized_refresh(project, index_id) == {
        "total": 1,
        "ready": 1,
        "stale": 0,
        "error": 0,
    }
    repaired = EmbeddingStore(project).get_index(index_id)
    assert repaired["total_items"] == 1
    assert repaired["ready_items"] == 1
    assert repaired["last_refresh_receipt_id"] is None
    assert repaired["last_refreshed_at"] is None


# --------------------------------------------------------------------------
# Validate provider batch output before any sidecar write
# --------------------------------------------------------------------------


class _ShortGateway:
    """Returns fewer vectors than inputs."""

    def __init__(self):
        self.calls = []

    def embed(self, texts, *, provider, model, modality):
        self.calls.append(list(texts))
        vecs = [[1.0] + [0.0] * 383 for _ in texts][:-1]  # drop one
        return build_batch_result(
            vecs,
            provider_id="fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
            dimension=384,
        )


class _RaggedGateway:
    """Returns the right count but one wrong-width vector."""

    def embed(self, texts, *, provider, model, modality):
        vecs = [[1.0] + [0.0] * 383 for _ in texts]
        if vecs:
            vecs[-1] = [1.0, 0.0, 0.0]  # width 3, not 384
        return build_batch_result(
            vecs,
            provider_id="fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
        )


def test_refresh_short_batch_is_provider_error_no_writes(project):
    index_id = _create(project)
    result = _run(project, _refresh_action(index_id), gateway=_ShortGateway())
    assert result.status == "failed"
    assert result.errors[0].code == "embedding_provider_error"
    backend = VectorBackend(project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {}  # nothing written
    backend.close()


def test_refresh_wrong_vector_width_is_space_mismatch_no_writes(project):
    index_id = _create(project)
    result = _run(project, _refresh_action(index_id), gateway=_RaggedGateway())
    assert result.status == "failed"
    assert result.errors[0].code == "embedding_space_mismatch"
    backend = VectorBackend(project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {}
    backend.close()
