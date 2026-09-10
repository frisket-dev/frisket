"""Actual operation arguments, canonical intent, and worker-only execution."""

from __future__ import annotations

from dataclasses import replace

import pytest

from frisket.actions.core import ActionCategory, RegisteredAction, action
from frisket.actions.system import BoundTypedActionRequest, typed_action_for_request
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    CreatedEmbeddingIndex,
    IndexCreator,
    IndexRefresher,
    RefreshedEmbeddingIndex,
)
from frisket.ai.embeddings import EmbeddingStore, build_batch_result
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.action_families.embeddings import (
    run_index_refresh_action_job,
    run_typed_embedding_action,
)
from frisket.engine.executor.action_jobs import (
    bind_typed_action_job,
    reserve_typed_action_job,
)
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


class Creation(ActionParams):
    owner: int
    column: str


def create_derived(p: Creation, indexes: IndexCreator) -> CreatedEmbeddingIndex:
    result = indexes.create(
        sheet_id=p.owner,
        source_columns=[p.column.removeprefix("prefix:")],
        provider="fastembed",
        name="derived",
    )
    return result.model_copy(update={"dimension": 999})


class Refresh(ActionParams):
    identifier: str
    origin: str


def refresh_derived(p: Refresh, indexes: IndexRefresher) -> RefreshedEmbeddingIndex:
    result = indexes.refresh(
        p.identifier.removeprefix("prefix:"),
        mode="full",
        trigger_ref={"trigger_kind": p.origin, "source": "actual"},
    )
    return result.model_copy(update={"refreshed": 999})


def bound_for(handler, params, key="custom"):
    registered = RegisteredAction(
        "test.lifecycle",
        action(
            name="lifecycle",
            title="Lifecycle",
            description="Test lifecycle",
            category=ActionCategory.SOURCES,
            run=handler,
        ),
    )
    return BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id="test.lifecycle",
            scope={"kind": "project"},
            params=params,
            idempotency_key=key,
        ),
    )


class Gateway:
    def __init__(self):
        self.calls = []

    def embed(self, texts, *, provider, model, modality):
        self.calls.append(list(texts))
        return build_batch_result(
            [[1.0] + [0.0] * 383 for _ in texts],
            provider_id=provider,
            provider_kind="local_process",
            actual_model_id=model,
            modality=modality,
        )


@pytest.fixture
def source(tmp_path):
    project = Project.create(tmp_path / "lifecycle.frisket")
    sheet = project.add_sheet("Input")
    column = project.add_column(sheet, "text")
    project.add_rows(sheet, [{"text": "one"}, {"text": "two"}], {"text": column})
    yield project, sheet
    project.close()


def test_derived_create_receipt_owns_arguments_and_result(source):
    project, sheet = source
    bound = bound_for(create_derived, {"owner": sheet, "column": "prefix:text"})
    result = run_typed_embedding_action(project, "p", bound)
    assert result.status == "completed", result.errors
    ref = result.outputs[0].ref
    assert ref["dimension"] == 384
    index = EmbeddingStore(project).get_index(ref["index_id"])
    assert index["name"] == "derived"
    receipt = ReceiptStore(project).find_by_id(result.receipt_id).parsed()
    assert receipt.params_hash == typed_request_hash(bound)
    assert receipt.inputs[0].ref["source_columns"] == ["text"]
    assert ref["operation_args_sha256"] != receipt.params_hash
    replay = run_typed_embedding_action(project, "p", bound)
    assert replay.receipt_id == result.receipt_id
    # Creation replay validates the persisted index definition and space, not
    # continuing source freshness. Exercise that same invariant for a reused
    # capability under a different action ID.
    assert EmbeddingStore(project).delete_index(ref["index_id"])
    stale = run_typed_embedding_action(project, "p", bound)
    assert stale.status == "failed"
    assert stale.errors[0].code == "stale_replay"


def test_derived_refresh_trigger_counts_and_request_identity(source):
    project, sheet = source
    created = run_typed_embedding_action(
        project,
        "p",
        bound_for(create_derived, {"owner": sheet, "column": "prefix:text"}, "create"),
    )
    index_id = created.outputs[0].ref["index_id"]
    bound = bound_for(
        refresh_derived, {"identifier": "prefix:" + index_id, "origin": "manual"}
    )
    gateway = Gateway()
    result = run_typed_embedding_action(project, "p", bound, embedding_gateway=gateway)
    assert result.status == "completed", result.errors
    assert len(gateway.calls) == 1
    assert result.outputs[0].ref["refreshed"] == 2
    assert result.outputs[0].ref["trigger_ref"] == {
        "trigger_kind": "manual",
        "source": "actual",
    }
    assert ReceiptStore(project).find_by_id(
        result.receipt_id
    ).params_hash == typed_request_hash(bound)


def test_queue_reserves_intent_without_index_lookup_or_handler(source, monkeypatch):
    project, _ = source
    bound = typed_action_for_request(
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": "missing"},
            "idempotency_key": "queued",
        }
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("enqueue must not resolve operation arguments")

    with monkeypatch.context() as patch:
        patch.setattr(EmbeddingStore, "get_index", forbidden)
        envelope = reserve_typed_action_job(project, "p", bound)
    assert envelope.action["action_id"] == "embedding.index_refresh"
    assert envelope.resolve_phase == "worker"
    assert envelope.params_hash == typed_request_hash(bound)
    result = run_index_refresh_action_job(project, envelope)
    assert result.status == "failed"
    assert result.errors[0].code == "embedding_index_not_found"
    assert (
        bind_typed_action_job(replace(envelope, idempotency_key="forged")).status
        == "failed"
    )
    changed = dict(envelope.action, params={"index_id": "another"})
    assert bind_typed_action_job(replace(envelope, action=changed)).status == "failed"


def test_plain_preview_and_legacy_wire_never_create_an_index(source):
    from frisket.engine.executor import resolve_map_preview

    project, sheet = source
    request = {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": {"sheet_id": sheet, "source_columns": ["text"]},
        "idempotency_key": "preview",
    }
    assert resolve_map_preview(project, request).code == "unsupported_action_kind"
    result = run_action_spec(
        project,
        {
            "schema_version": "frisket.action.v2",
            "kind": "embedding.index_create",
            "capabilities": ["project:write"],
            "params": request["params"],
            "idempotency_key": "retired",
        },
        project_id="p",
    )
    assert result.status == "failed"
    assert not project.db.execute("SELECT 1 FROM embedding_indexes").fetchall()


@pytest.mark.parametrize(
    "params",
    [
        {"source_columns": "text"},
        {"source_columns": [" "]},
        {"source_columns": ["text"], "sheet_id": "1"},
        {"source_columns": ["text"], "confirmed": True},
    ],
)
def test_structural_create_refusal_is_before_mutation(source, params):
    project, _ = source
    result = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": params,
            "idempotency_key": "invalid",
        },
        project_id="p",
    )
    assert result.status == "failed"
    assert result.errors[0].code == "invalid_action_request"
    assert not project.db.execute("SELECT 1 FROM embedding_indexes").fetchall()
    assert not project.db.execute("SELECT 1 FROM receipts").fetchall()
