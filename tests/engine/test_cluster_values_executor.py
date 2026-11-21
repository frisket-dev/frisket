from __future__ import annotations
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

import copy
import json
import sqlite3
import threading
from array import array
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from action_test_helpers import counts
from executor_harness import CatalogEntry, ExecutorCase, Gate
from frisket.engine.store import Project
from frisket.server.services.action_runs import v1_action_result_http_status
from helpers import replace_test_source_cell
from frisket.engine.store.transactions import write_transaction


class _RemoteEmbeddingAdapter:
    COST_USD = 0.01

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.on_embed: Callable[[], None] | None = None

    @staticmethod
    def _vectors(texts: list[str]) -> list[list[float]]:
        return [[1.0, *([0.0] * 1535)] for _ in texts]

    async def embed(self, texts, model, client):  # noqa: ANN001
        del model, client
        batch = list(texts)
        self.calls.append(batch)
        return self._vectors(batch)

    async def embed_with_meta(self, texts, model, client):  # noqa: ANN001
        del client
        if self.on_embed is not None:
            self.on_embed()
        batch = list(texts)
        self.calls.append(batch)
        return self._vectors(batch), {
            "requested_model": model,
            "actual_model_id": model,
            "dimension": 1536,
            "usage": {"input_count": len(batch)},
            "provider_cost_usd": self.COST_USD,
        }


class _AmbiguousRemoteEmbeddingAdapter(_RemoteEmbeddingAdapter):
    async def embed_with_meta(self, texts, model, client):  # noqa: ANN001
        del model, client
        self.calls.append(list(texts))
        raise RuntimeError("injected ambiguous provider outcome")


def _remote_embedding_router():
    from frisket.ai.llm import ModelRouter

    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"openai": "sk-project-openai"},
        key_sources={"openai": "project_key"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    adapter = _RemoteEmbeddingAdapter()
    router._adapters["openai"] = adapter  # noqa: SLF001
    return router, adapter


def _ambiguous_remote_embedding_router():
    router, _adapter = _remote_embedding_router()
    adapter = _AmbiguousRemoteEmbeddingAdapter()
    router._adapters["openai"] = adapter  # noqa: SLF001
    return router, adapter


def _cluster_action(
    sheet_id: int,
    *,
    idempotency_key: str = "cluster_values@sha256:v1",
    input_column: str = "name",
    min_size: int = 2,
    source_hash: str | None = None,
    method: str = "fingerprint",
    confirmation: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "source": input_column,
        "method": method,
        "min_size": min_size,
    }
    if source_hash is not None:
        params["review"] = {"source_hash": source_hash}
    return {
        "action_id": "cluster.values",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "output_names": {"canonical": f"{input_column}_canonical"},
        "confirmation": confirmation,
        "params": params,
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("People")
    name_column_id = project.add_column(sheet_id, "name", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [
            {"name": "Jon Smith"},
            {"name": "Smith, Jon"},
            {"name": "Jon Smith"},
            {"name": "Jane Doe"},
            {"name": "Acme Corp"},
        ],
        {"name": name_column_id},
    )
    return {
        "sheet_id": sheet_id,
        "name_column_id": name_column_id,
        "row_ids": row_ids,
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _cluster_action(seeded["sheet_id"])


def _forged_capability_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _cluster_action(
        seeded["sheet_id"],
        idempotency_key="cluster_values@sha256:missing-capability",
    )
    action["capabilities"] = []
    return action


def _missing_idempotency_key_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _cluster_action(seeded["sheet_id"])
    del action["idempotency_key"]
    return action


def _invalid_method_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _cluster_action(
        seeded["sheet_id"], idempotency_key="cluster_values@sha256:bad-method"
    )
    action["params"]["method"] = "vibes"
    return action


def _missing_column_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _cluster_action(
        seeded["sheet_id"],
        idempotency_key="cluster_values@sha256:bad-column",
        input_column="missing",
    )


def _conflicting_min_size_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _cluster_action(seeded["sheet_id"], min_size=3)


def _wrong_value_hash_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # A wrong reviewed source hash fails the commit before any write.
    return _cluster_action(
        seeded["sheet_id"],
        idempotency_key="cluster_values@sha256:hash-gate",
        source_hash="sha256:" + "0" * 64,
    )


def _tamper_source_values(project: Project, seeded: dict[str, Any]) -> None:
    replace_test_source_cell(
        project,
        row_id=seeded["row_ids"][0],
        column_id=seeded["name_column_id"],
        value="Jonathan Smith",
    )


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    sheet_id = seeded["sheet_id"]
    row_ids = seeded["row_ids"]
    assert len(result.op_ids) == 1
    assert {output.kind for output in result.outputs} == {"column"}
    assert result.outputs[0].ref["kind"] == "map_result_column"
    column_output = result.outputs[0]
    assert column_output.name == "name_canonical"

    # the canonical column merges the two Jon Smith variants; singletons keep
    # their own value; receipt canonicals == the written column.
    canonical_column = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='name_canonical' "
        "AND hidden=0",
        (sheet_id,),
    ).fetchone()
    assert canonical_column is not None, "name_canonical column was not written"
    canonical = project.get_values(sheet_id, int(canonical_column["id"]))
    assert canonical[row_ids[0]] == "Jon Smith"
    assert canonical[row_ids[1]] == "Jon Smith"
    assert canonical[row_ids[2]] == "Jon Smith"
    assert canonical[row_ids[3]] == "Jane Doe"
    assert canonical[row_ids[4]] == "Acme Corp"

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "cluster.values"
    assert receipt.op_ids == list(result.op_ids)
    refs = [
        item.ref
        for section in (receipt.inputs, receipt.outputs, receipt.evidence)
        for item in section
    ]
    assert {"source_column", "map_result_column", "value_clusters"} <= {
        ref["kind"] for ref in refs
    }
    fact = next(ref for ref in refs if ref["kind"] == "value_clusters")
    assert len(fact["clusters"]) == 1
    assert fact["clusters"][0]["canonical"] == "Jon Smith"
    assert fact["clusters"][0]["row_ids"] == row_ids[:3]
    assert fact["source"]["value_hash"].startswith("sha256:")
    assert fact["source"]["sheet_id"] == sheet_id
    assert fact["source"]["column_id"] == seeded["name_column_id"]
    assert fact["source"]["row_ids"] == row_ids
    assert fact["canonical_values"] == [
        {"row_id": row, "value": canonical[row]} for row in row_ids
    ]


CASES = [
    ExecutorCase(
        kind="cluster.values",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="batch_deduped",
            async_mode="queued",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "read_input_rows",
                    "embed_distinct_values",
                    "create_column",
                    "write_cluster_groups",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_input_ref",
                    "invalid_action_request",
                    "stale_input",
                    "model_cost_requires_confirmation",
                    "stale_replay",
                    "idempotency_conflict",
                }
            ),
            cost_policy_kind="model_metered",
            cost_requires_confirmation=True,
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "missing_idempotency_key",
                _missing_idempotency_key_action,
                "invalid_action_request",
            ),
            Gate(
                "forged_capability_envelope",
                _forged_capability_action,
                "invalid_action_request",
            ),
            Gate("invalid_params", _invalid_method_action, "invalid_action_request"),
            Gate(
                "invalid_input_ref",
                _missing_column_action,
                "invalid_params",
            ),
            Gate(
                "reviewed_source_hash_mismatch",
                _wrong_value_hash_action,
                "stale_input",
            ),
            Gate(
                "idempotency_conflict",
                _conflicting_min_size_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
            # source values changed since the completed run -> replay is stale
            Gate(
                "stale_replay_source_changed",
                _make_action,
                "stale_replay",
                after_primary_run=True,
                prepare=_tamper_source_values,
            ),
        ),
        expect_counts={
            "sheets": 0,
            "columns": 1,
            "rows": 0,
            "ops": 1,
            "receipts": 1,
        },
        check_state=_check_state,
    )
]


def _run_remote_cluster_with_exact_confirmation(
    project: Project,
    action: dict[str, Any],
    *,
    project_id: str,
    router: Any,
) -> tuple[Any, Any]:
    from frisket.engine.executor.actions import run_action_spec

    estimate = run_action_spec(
        project,
        action,
        project_id=project_id,
        router=router,
    )
    assert estimate.status == "needs_confirmation", estimate.errors
    assert v1_action_result_http_status(estimate) == 402
    promise_set_hash = estimate.errors[0].details["promise_set_hash"]
    retry = copy.deepcopy(action)
    retry["confirmation"] = promise_set_hash
    return estimate, run_action_spec(
        project,
        retry,
        project_id=project_id,
        router=router,
    )


def _recover_abandoned_cluster(project, action, *, router, project_id):
    """Model a dead process through the existing stale-lease recovery path."""
    from frisket.engine.executor.actions import run_action_spec

    project.db.execute(
        "UPDATE receipts SET created_at='2000-01-01T00:00:00Z' WHERE status='running'"
    )
    project.db.commit()
    live = run_action_spec(project, action, project_id=project_id, router=router)
    assert live.errors[0].code == "idempotency_in_progress", live.model_dump()

    # Receipt age by itself does not prove its writer is dead. Expire the
    # attempt and its output leases before asking the normal retry path to
    # recover the checkpoint.
    project.db.execute(
        "UPDATE execution_attempts SET created_at='2000-01-01T00:00:00Z' WHERE state='dispatching'"
    )
    project.db.execute(
        "UPDATE output_column_claims SET lease_expires_at='2000-01-01T00:00:00Z' WHERE status='active'"
    )
    project.db.commit()
    recovered = run_action_spec(project, action, project_id=project_id, router=router)
    assert recovered.status in {"completed", "failed"}, recovered.model_dump()


@pytest.mark.parametrize("source", ["missing", "numeric"])
def test_source_descriptor_refusal_explains_supported_types_without_writes(
    tmp_path, make_project, source
):
    from frisket.engine.executor.actions import run_action_spec

    project = make_project("invalid-cluster-source")
    seeded = _seed(project, tmp_path)
    project.add_column(seeded["sheet_id"], "numeric", type="integer")
    before = counts(project)
    result = run_action_spec(
        project,
        _cluster_action(seeded["sheet_id"], input_column=source),
        project_id="source-types",
    )
    assert result.status == "failed", result.model_dump()
    assert any(
        "visible text, category, or link source column" in item["message"]
        for item in result.errors[0].details["errors"]
    )
    assert counts(project) == before


def test_semantic_cluster_remote_cache_miss_requires_confirmation_before_egress(
    tmp_path: Path, make_project, monkeypatch
) -> None:
    from frisket.engine.executor.actions import run_action_spec

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = make_project("cluster-values-unconfirmed")
    seeded = _seed(project, tmp_path)
    router, adapter = _remote_embedding_router()
    action = _cluster_action(
        seeded["sheet_id"],
        idempotency_key="cluster_values@sha256:unconfirmed-remote",
        method="semantic",
    )
    before = counts(project)

    result = run_action_spec(
        project,
        action,
        project_id="cluster-values-unconfirmed",
        router=router,
    )

    assert result.status == "needs_confirmation"
    assert v1_action_result_http_status(result) == 402
    assert result.errors[0].code == "model_cost_requires_confirmation"
    details = result.errors[0].details
    assert details["estimate"]["rows"] == len(seeded["row_ids"])
    assert details["estimate"]["cost"] is None
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.cluster_action import prepare_cluster_action
    from frisket.engine.executor.value_cluster import cluster_embedding_inputs
    from frisket.actions.cluster_types import ClusterOptions

    plan = prepare_cluster_action(
        project, typed_action_for_request(action), router=router
    )
    state = plan.spec["cluster_values"]
    assert state["source"]["value_hash"].startswith("sha256:")
    assert state["source"]["row_ids"] == seeded["row_ids"]
    assert state["embedding_model"] == "openai/text-embedding-3-small"
    assert (
        len(
            cluster_embedding_inputs(
                project.get_values(seeded["sheet_id"], seeded["name_column_id"]),
                ClusterOptions.model_validate(state["options"]),
            )
        )
        == 4
    )
    assert len(details["promise_set_hash"]) == 64
    assert adapter.calls == []
    assert counts(project) == before


def test_semantic_cluster_wrong_exact_echo_repauses(
    tmp_path: Path, make_project, monkeypatch
) -> None:
    from frisket.engine.executor.actions import run_action_spec

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = make_project("cluster-values-bare-confirmed")
    seeded = _seed(project, tmp_path)
    router, adapter = _remote_embedding_router()
    action = _cluster_action(
        seeded["sheet_id"],
        idempotency_key="cluster_values@sha256:bare-confirmed-remote",
        method="semantic",
        confirmation="0" * 64,
    )
    before = counts(project)

    result = run_action_spec(
        project,
        action,
        project_id="cluster-values-bare-confirmed",
        router=router,
    )

    assert result.status == "needs_confirmation"
    assert v1_action_result_http_status(result) == 402
    assert len(result.errors[0].details["promise_set_hash"]) == 64
    assert adapter.calls == []
    assert counts(project) == before


def test_semantic_cluster_source_change_invalidates_old_confirmation_hash(
    tmp_path: Path, make_project, monkeypatch
) -> None:
    from frisket.engine.executor.actions import run_action_spec

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = make_project("cluster-values-stale-confirmation")
    seeded = _seed(project, tmp_path)
    router, adapter = _remote_embedding_router()
    action = _cluster_action(
        seeded["sheet_id"],
        idempotency_key="cluster_values@sha256:stale-confirmation",
        method="semantic",
    )
    estimate = run_action_spec(
        project,
        action,
        project_id="cluster-values-stale-confirmation",
        router=router,
    )
    old_hash = estimate.errors[0].details["promise_set_hash"]
    _tamper_source_values(project, seeded)
    before = counts(project)
    retry = copy.deepcopy(action)
    retry["confirmation"] = old_hash

    stale = run_action_spec(
        project,
        retry,
        project_id="cluster-values-stale-confirmation",
        router=router,
    )

    assert stale.status == "needs_confirmation"
    assert v1_action_result_http_status(stale) == 402
    assert stale.errors[0].details["promise_set_hash"] != old_hash
    assert adapter.calls == []
    assert counts(project) == before


def test_semantic_cluster_refuses_an_exhausted_project_key_before_embedding(
    tmp_path: Path, make_project, monkeypatch
) -> None:
    """cluster.values bypasses MapRunner, but its semantic warm can call the
    project's OpenAI embedding key.  An exhausted cap must stop that pre-txn
    warm before any text leaves the process."""
    from frisket.security.secrets import encrypt_secret, key_hint

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = make_project("cluster-values-exhausted")
    seeded = _seed(project, tmp_path)
    with write_transaction(project.db):
        project.set_provider_key(
            provider="openai",
            encrypted=encrypt_secret("sk-project-openai"),
            hint=key_hint("sk-project-openai"),
            spend_cap_micro=0,
        )
    router, adapter = _remote_embedding_router()
    action = _cluster_action(
        seeded["sheet_id"],
        idempotency_key="cluster_values@sha256:exhausted-project-key",
        method="semantic",
    )
    _, result = _run_remote_cluster_with_exact_confirmation(
        project,
        action,
        project_id="cluster-values-exhausted",
        router=router,
    )

    assert result.status == "failed"
    assert result.receipt_id is None
    assert result.errors[0].code == "provider_spend_cap_exceeded"
    assert adapter.calls == []


def test_semantic_cluster_persists_remote_fact_and_receipts_the_provider_cost(
    tmp_path: Path, make_project, monkeypatch
) -> None:
    """The semantic warm is a paid provider effect, not a local clustering
    detail.  Its durable fact, project-key accrual, and completed receipt must
    all describe the same remote call and amount."""
    from frisket.security.secrets import encrypt_secret, key_hint

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = make_project("cluster-values-provider-fact")
    seeded = _seed(project, tmp_path)
    with write_transaction(project.db):
        project.set_provider_key(
            provider="openai",
            encrypted=encrypt_secret("sk-project-openai"),
            hint=key_hint("sk-project-openai"),
            spend_cap_micro=1_000_000,
        )
    router, adapter = _remote_embedding_router()
    action = _cluster_action(
        seeded["sheet_id"],
        idempotency_key="cluster_values@sha256:remote-provider-fact",
        method="semantic",
    )
    _, result = _run_remote_cluster_with_exact_confirmation(
        project,
        action,
        project_id="cluster-values-provider-fact",
        router=router,
    )

    assert result.status == "completed", result.errors
    assert len(adapter.calls) == 1
    calls = project.db.execute(
        "SELECT * FROM model_calls WHERE provider='openai' ORDER BY created_at"
    ).fetchall()
    assert len(calls) == len(adapter.calls)
    assert calls[0]["run_id"] == result.run_id
    assert calls[0]["capability"] == "llm.embed"
    assert calls[0]["credential_source"] == "project_key"
    assert calls[0]["provider_cost_usd"] == _RemoteEmbeddingAdapter.COST_USD

    expected_cost = len(adapter.calls) * _RemoteEmbeddingAdapter.COST_USD
    spend = project.provider_spend_state("openai")
    assert spend is not None
    assert spend.spent_micro == round(expected_cost * 1_000_000)
    assert spend.unmetered_calls == 0

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    receipt = json.loads(receipt_row["body"])
    provider_use = receipt["provider_use"]
    assert len(provider_use) == 1
    assert provider_use[0]["provider"] == "openai"
    assert provider_use[0]["credential_source"] == "project_key"
    assert provider_use[0]["external_api"] is True
    assert provider_use[0]["model_call_count"] == len(adapter.calls)
    assert provider_use[0]["cost_actual"] == expected_cost


def test_cluster_replay_does_not_finalize_a_live_completed_writer(
    tmp_path: Path, make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import cluster_action
    from frisket.engine.executor.actions import run_action_spec

    project = make_project("cluster-values-live-replay")
    seeded = _seed(project, tmp_path)
    action = _cluster_action(
        seeded["sheet_id"],
        idempotency_key="cluster_values@sha256:live-completed-writer",
        method="fingerprint",
    )
    original_finalize = cluster_action.finalize_cluster_action
    replay_results = []
    replayed = False

    def replay_during_live_finalize(current_project, *args, **kwargs):
        nonlocal replayed
        if not replayed:
            replayed = True
            replay_results.append(
                run_action_spec(
                    current_project,
                    action,
                    project_id="cluster-values-live-replay",
                )
            )
            owner = current_project.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (kwargs["writer_attempt_id"],),
            ).fetchone()
            assert owner is not None and owner["state"] == "dispatching"
        return original_finalize(current_project, *args, **kwargs)

    monkeypatch.setattr(
        cluster_action, "finalize_cluster_action", replay_during_live_finalize
    )
    result = run_action_spec(
        project,
        action,
        project_id="cluster-values-live-replay",
    )

    assert replayed
    assert result.status == "completed", result.model_dump()
    assert len(replay_results) == 1
    assert replay_results[0].status == "failed"
    assert replay_results[0].errors[0].code == "idempotency_in_progress"
    run = project.db.execute(
        "SELECT status,current_attempt_id FROM runs WHERE id=?",
        (result.run_id,),
    ).fetchone()
    assert tuple(run) == ("completed", None)


def test_semantic_cluster_replay_and_vector_cache_do_not_double_spend(
    tmp_path: Path, make_project, monkeypatch
) -> None:
    """An idempotency replay reuses the original receipt, while a distinct
    action over the same content uses the vector cache. Neither is a new
    provider effect, even after the first call has exhausted the key's cap."""
    from frisket.engine.executor.actions import run_action_spec
    from frisket.security.secrets import encrypt_secret, key_hint

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = make_project("cluster-values-cache-accounting")
    seeded = _seed(project, tmp_path)
    with write_transaction(project.db):
        project.set_provider_key(
            provider="openai",
            encrypted=encrypt_secret("sk-project-openai"),
            hint=key_hint("sk-project-openai"),
            spend_cap_micro=1_000_000,
        )
    router, adapter = _remote_embedding_router()
    first_action = _cluster_action(
        seeded["sheet_id"],
        idempotency_key="cluster_values@sha256:remote-cache-source",
        method="semantic",
    )
    _, first = _run_remote_cluster_with_exact_confirmation(
        project,
        first_action,
        project_id="cluster-values-cache-accounting",
        router=router,
    )
    replay = run_action_spec(
        project,
        first_action,
        project_id="cluster-values-cache-accounting",
        router=router,
    )
    assert first.status == replay.status == "completed"
    assert first.receipt_id == replay.receipt_id
    assert len(adapter.calls) == 1
    spend_after_first = project.provider_spend_state("openai")
    assert spend_after_first is not None

    # Set the cap exactly at accrued spend. A real second egress would now
    # refuse, but the content-addressed vectors make this action cache-only.
    with write_transaction(project.db):
        project.set_provider_key(
            provider="openai",
            encrypted=encrypt_secret("sk-project-openai"),
            hint=key_hint("sk-project-openai"),
            spend_cap_micro=spend_after_first.spent_micro,
        )
    cached_action = _cluster_action(
        seeded["sheet_id"],
        idempotency_key="cluster_values@sha256:remote-cache-consumer",
    )
    cached_action["params"]["method"] = "semantic"
    cached_action["output_names"]["canonical"] = "name_cached_canonical"
    cached = run_action_spec(
        project,
        cached_action,
        project_id="cluster-values-cache-accounting",
        router=router,
    )

    assert cached.status == "completed", cached.errors
    assert len(adapter.calls) == 1
    assert project.provider_spend_state("openai") == spend_after_first.__class__(
        provider="openai",
        cap_micro=spend_after_first.spent_micro,
        spent_micro=spend_after_first.spent_micro,
        unmetered_calls=0,
    )
    assert len(project.db.execute("SELECT id FROM model_calls").fetchall()) == 1
    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (cached.receipt_id,)
    ).fetchone()
    provider_use = json.loads(receipt_row["body"])["provider_use"]
    assert provider_use == [
        {
            "provider": "openai",
            "model": "openai/text-embedding-3-small",
            "service": "frisket.cluster.semantic.embedding_cache",
            "credential_source": "cache",
            "external_api": False,
            "model_call_count": 0,
            "cost_actual": 0.0,
        }
    ]


@pytest.mark.parametrize("sidecar_commit_landed", [False, True])
def test_semantic_cluster_sidecar_commit_failure_reconciles_without_second_call(
    tmp_path: Path, monkeypatch, sidecar_commit_landed: bool
) -> None:
    """A paid batch can return before the rebuildable vector sidecar commits.

    The exact confirmed retry must reconcile that returned batch from durable
    project state, not buy it again. The failed materialization has no completed
    receipt or canonical output to make the first attempt look successful.
    """
    from frisket.engine.executor.actions import run_action_spec
    from frisket.retrieval.store import RetrievalStore
    from frisket.security.secrets import encrypt_secret, key_hint

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = Project.create(
        tmp_path / f"cluster-values-sidecar-failure-{sidecar_commit_landed}.frisket"
    )
    try:
        seeded = _seed(project, tmp_path)
        with write_transaction(project.db):
            project.set_provider_key(
                provider="openai",
                encrypted=encrypt_secret("sk-project-openai"),
                hint=key_hint("sk-project-openai"),
                spend_cap_micro=1_000_000,
            )
        router, adapter = _remote_embedding_router()
        action = _cluster_action(
            seeded["sheet_id"],
            idempotency_key="cluster_values@sha256:sidecar-failure-reconcile",
            method="semantic",
        )
        estimate = run_action_spec(
            project,
            action,
            project_id="cluster-values-sidecar-failure",
            router=router,
        )
        confirmed = copy.deepcopy(action)
        confirmed["confirmation"] = estimate.errors[0].details["promise_set_hash"]

        real_put = RetrievalStore.put_representations
        fail_next = True

        def fail_first_put(self, representations):
            nonlocal fail_next
            if not fail_next:
                return real_put(self, representations)
            fail_next = False
            if sidecar_commit_landed:
                real_put(self, representations)
            raise sqlite3.OperationalError("injected vector sidecar commit failure")

        monkeypatch.setattr(RetrievalStore, "put_representations", fail_first_put)
        original_context = {
            "funding_account_id": 17,
            "reservation_id": "embedding-reservation",
        }

        def assert_context_precedes_provider_egress() -> None:
            checkpoint = project.db.execute(
                "SELECT state, payload FROM effect_checkpoints "
                "WHERE family='model_call'"
            ).fetchone()
            assert checkpoint is not None and checkpoint["state"] == "reserved"
            receipt = project.db.execute(
                "SELECT edition_run_context FROM receipts WHERE status='running'"
            ).fetchone()
            assert receipt is not None
            assert json.loads(receipt["edition_run_context"]) == original_context

        adapter.on_embed = assert_context_precedes_provider_egress

        with pytest.raises(
            sqlite3.OperationalError,
            match="injected vector sidecar commit failure",
        ):
            run_action_spec(
                project,
                confirmed,
                project_id="cluster-values-sidecar-failure",
                router=router,
                edition_run_context=original_context,
            )

        assert len(adapter.calls) == 1
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE status='completed'"
            ).fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT 1 FROM columns WHERE sheet_id=? AND name='name_canonical' "
                "AND hidden=0",
                (seeded["sheet_id"],),
            ).fetchone()
            is None
        )
        checkpoint = project.db.execute(
            "SELECT state, payload FROM effect_checkpoints WHERE family='model_call'"
        ).fetchone()
        assert checkpoint is not None
        assert checkpoint["state"] == "returned"
        checkpoint_payload = json.loads(checkpoint["payload"])
        assert len(checkpoint_payload["vectors"]) == 4
        assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1

        _recover_abandoned_cluster(
            project,
            confirmed,
            router=router,
            project_id="cluster-values-sidecar-failure",
        )
        retried = run_action_spec(
            project,
            confirmed,
            project_id="cluster-values-sidecar-failure",
            router=router,
            edition_run_context={"reservation_id": "replacement-must-not-win"},
        )

        assert retried.status == "completed", retried.errors
        assert len(adapter.calls) == 1
        assert len(project.db.execute("SELECT id FROM model_calls").fetchall()) == 1
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM effect_checkpoints WHERE family='model_call'"
            ).fetchone()[0]
            == 0
        )
        spend = project.provider_spend_state("openai")
        assert spend is not None
        assert spend.spent_micro == round(_RemoteEmbeddingAdapter.COST_USD * 1_000_000)
        receipt_row = project.db.execute(
            "SELECT body, edition_run_context FROM receipts WHERE id=?",
            (retried.receipt_id,),
        ).fetchone()
        receipt_body = json.loads(receipt_row["body"])
        provider_use = receipt_body["provider_use"]
        assert json.loads(receipt_row["edition_run_context"]) == original_context
        assert "edition_run_context" not in receipt_body
        assert provider_use[0]["external_api"] is True
        assert provider_use[0]["model_call_count"] == 1
        assert provider_use[0]["cost_actual"] == _RemoteEmbeddingAdapter.COST_USD
    finally:
        project.close()


def test_semantic_cluster_retry_matches_checkpoints_to_stable_batches(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial cache warming must not renumber paid embedding checkpoints."""
    from frisket.ai.embeddings.analytics import ensure_legacy_analytical_space
    from frisket.ai.embeddings.backends import REMOTE_EMBED_MODEL
    from frisket.engine.executor.actions import run_action_spec
    from frisket.retrieval.sources import input_hash, representation_id
    from frisket.retrieval.store import CachedRepresentation, RetrievalStore
    from frisket.security.secrets import encrypt_secret, key_hint

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = make_project("cluster-values-stable-batches")
    sheet_id = project.add_sheet("Organizations")
    column_id = project.add_column(sheet_id, "name", type="text")
    values = [f"organization-{index:03d}" for index in range(501)]
    project.add_rows(
        sheet_id,
        [{"name": value} for value in values],
        {"name": column_id},
    )
    with write_transaction(project.db):
        project.set_provider_key(
            provider="openai",
            encrypted=encrypt_secret("sk-project-openai"),
            hint=key_hint("sk-project-openai"),
            spend_cap_micro=1_000_000,
        )
    router, adapter = _remote_embedding_router()
    action = _cluster_action(
        sheet_id,
        idempotency_key="cluster_values@sha256:stable-batch-retry",
        method="semantic",
    )
    estimate = run_action_spec(
        project,
        action,
        project_id="cluster-values-stable-batches",
        router=router,
    )
    confirmed = copy.deepcopy(action)
    confirmed["confirmation"] = estimate.errors[0].details["promise_set_hash"]

    real_put = RetrievalStore.put_representations
    fail_next = True

    def fail_second_put(self, representations):
        nonlocal fail_next
        if fail_next and len(adapter.calls) >= 2:
            fail_next = False
            raise sqlite3.OperationalError(
                "injected second semantic batch sidecar failure"
            )
        return real_put(self, representations)

    monkeypatch.setattr(RetrievalStore, "put_representations", fail_second_put)
    with pytest.raises(
        sqlite3.OperationalError,
        match="injected second semantic batch sidecar failure",
    ):
        run_action_spec(
            project,
            confirmed,
            project_id="cluster-values-stable-batches",
            router=router,
        )
    assert [len(batch) for batch in adapter.calls] == [500, 1]
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM effect_checkpoints WHERE family='model_call'"
        ).fetchone()[0]
        == 2
    )

    # Reproduce an arbitrary partial sidecar landing: only the first 250
    # values from planned batch 0 are cached before the exact retry.
    monkeypatch.setattr(RetrievalStore, "put_representations", real_put)
    space = ensure_legacy_analytical_space(project, REMOTE_EMBED_MODEL)
    with RetrievalStore(project) as sidecar:
        sidecar.db.execute(
            "DELETE FROM representations WHERE space_id=?", (space.identity,)
        )
        sidecar.put_representations(
            [
                CachedRepresentation(
                    representation_id=representation_id(
                        space=space.identity, input_hash=input_hash(value)
                    ),
                    space_id=space.identity,
                    input_hash=input_hash(value),
                    vector=array("f", [1.0, *([0.0] * 1535)]).tobytes(),
                    dimension=1536,
                )
                for value in values[:250]
            ]
        )

    _recover_abandoned_cluster(
        project,
        confirmed,
        router=router,
        project_id="cluster-values-stable-batches",
    )
    retried = run_action_spec(
        project,
        confirmed,
        project_id="cluster-values-stable-batches",
        router=router,
    )

    assert retried.status == "completed", retried.errors
    assert [len(batch) for batch in adapter.calls] == [500, 1]
    assert len(project.db.execute("SELECT id FROM model_calls").fetchall()) == 2
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM effect_checkpoints WHERE family='model_call'"
        ).fetchone()[0]
        == 0
    )


def test_semantic_cluster_terminal_receipt_and_context_transfer_are_atomic(
    tmp_path: Path, make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor.actions import run_action_spec
    from frisket.engine.store.output_claims import OutputColumnClaimStore
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.security.secrets import encrypt_secret, key_hint

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = make_project("cluster-values-receipt-atomic")
    seeded = _seed(project, tmp_path)
    with write_transaction(project.db):
        project.set_provider_key(
            provider="openai",
            encrypted=encrypt_secret("sk-project-openai"),
            hint=key_hint("sk-project-openai"),
            spend_cap_micro=1_000_000,
        )
    router, adapter = _remote_embedding_router()
    action = _cluster_action(
        seeded["sheet_id"],
        idempotency_key="cluster_values@sha256:receipt-context-atomic",
        method="semantic",
    )
    estimate = run_action_spec(
        project,
        action,
        project_id="cluster-values-receipt-atomic",
        router=router,
    )
    confirmed = copy.deepcopy(action)
    confirmed["confirmation"] = estimate.errors[0].details["promise_set_hash"]
    original_context = {
        "funding_account_id": 17,
        "reservation_id": "atomic-transfer",
    }
    original_update = ReceiptStore.update_if_status_in
    fail_next = True

    def fail_first_receipt_update(self, *args, **kwargs):
        nonlocal fail_next
        result = original_update(self, *args, **kwargs)
        if fail_next:
            fail_next = False
            raise sqlite3.OperationalError("injected receipt update failure")
        return result

    monkeypatch.setattr(ReceiptStore, "update_if_status_in", fail_first_receipt_update)

    with pytest.raises(
        sqlite3.OperationalError, match="injected receipt update failure"
    ):
        run_action_spec(
            project,
            confirmed,
            project_id="cluster-values-receipt-atomic",
            router=router,
            edition_run_context=original_context,
        )
    assert len(adapter.calls) == 1
    receipt = project.db.execute(
        "SELECT id,run_id,status,edition_run_context FROM receipts"
    ).fetchone()
    assert receipt["status"] == "running"
    original_receipt_id = str(receipt["id"])
    original_run_id = int(receipt["run_id"])
    assert json.loads(receipt["edition_run_context"]) == original_context
    assert (
        project.db.execute("SELECT COUNT(*) FROM cell_result_heads").fetchone()[0] == 0
    )
    checkpoint = project.db.execute(
        "SELECT state, payload FROM effect_checkpoints WHERE family='model_call'"
    ).fetchone()
    assert checkpoint is not None and checkpoint["state"] == "returned"
    assert (
        project.db.execute(
            "SELECT 1 FROM columns WHERE sheet_id=? AND name='name_canonical' AND hidden=0",
            (seeded["sheet_id"],),
        ).fetchone()
        is None
    )
    claims = OutputColumnClaimStore(project)
    _, conflict = claims.acquire(
        sheet_id=seeded["sheet_id"],
        output_names=["name_canonical"],
        action_kind="test.foreign_writer",
        claim_token="test:foreign-cluster-output",
    )
    assert conflict is None
    refused = run_action_spec(
        project,
        confirmed,
        project_id="cluster-values-receipt-atomic",
        router=router,
    )
    assert refused.status == "failed"
    assert refused.errors[0].code == "output_column_busy"
    stored_receipt = project.db.execute(
        "SELECT id,run_id,status FROM receipts WHERE id=?",
        (original_receipt_id,),
    ).fetchone()
    assert stored_receipt is not None
    assert stored_receipt["run_id"] == original_run_id
    assert stored_receipt["status"] == "running"
    claims.release(claim_token="test:foreign-cluster-output", status="released")

    # Both retries pass the idle receipt check before either reaches the
    # output claim CAS. Hold the winner's claim until the competing retry
    # has observed the conflict, proving it cannot delete the reused receipt.
    acquire_barrier = threading.Barrier(2)
    competing_refusal = threading.Event()
    original_acquire = OutputColumnClaimStore.acquire

    def synchronize_resume_claim(self, **kwargs):
        if (
            kwargs.get("receipt_id") == original_receipt_id
            and kwargs.get("claim_token") == f"output-claim:{original_receipt_id}"
        ):
            acquire_barrier.wait(timeout=10)
            result = original_acquire(self, **kwargs)
            if result[1] is None:
                assert competing_refusal.wait(timeout=10)
            return result
        return original_acquire(self, **kwargs)

    monkeypatch.setattr(OutputColumnClaimStore, "acquire", synchronize_resume_claim)

    def resume():
        result = run_action_spec(
            project,
            confirmed,
            project_id="cluster-values-receipt-atomic",
            router=router,
            edition_run_context={"reservation_id": "replacement-must-not-win"},
        )
        if result.status == "failed":
            competing_refusal.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        resumptions = [pool.submit(resume) for _ in range(2)]
        results = [future.result(timeout=30) for future in resumptions]

    completed = [result for result in results if result.status == "completed"]
    refused = [result for result in results if result.status == "failed"]
    assert len(completed) == 1
    assert len(refused) == 1
    assert refused[0].errors[0].code == "output_column_busy"
    assert completed[0].receipt_id == original_receipt_id
    assert len(adapter.calls) == 1
    replay = run_action_spec(
        project,
        confirmed,
        project_id="cluster-values-receipt-atomic",
        router=router,
        edition_run_context={"reservation_id": "replacement-must-not-win"},
    )
    assert replay.status == "completed", replay.errors
    assert replay.receipt_id == original_receipt_id
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM effect_checkpoints WHERE family='model_call'"
        ).fetchone()[0]
        == 0
    )
    receipt = project.db.execute(
        "SELECT edition_run_context FROM receipts WHERE id=?",
        (original_receipt_id,),
    ).fetchone()
    assert json.loads(receipt["edition_run_context"]) == original_context


def test_semantic_cluster_ambiguous_provider_outcome_refuses_exact_retry(
    tmp_path: Path, make_project, monkeypatch
) -> None:
    """Once egress starts, an exception without returned metadata is ambiguous.

    The durable reservation must block the exact retry rather than guessing
    that the first provider attempt was free and buying a second batch.
    """
    from frisket.engine.executor.actions import run_action_spec

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = make_project("cluster-values-ambiguous-provider")
    seeded = _seed(project, tmp_path)
    router, adapter = _ambiguous_remote_embedding_router()
    action = _cluster_action(
        seeded["sheet_id"],
        idempotency_key="cluster_values@sha256:ambiguous-provider",
        method="semantic",
    )
    estimate = run_action_spec(
        project,
        action,
        project_id="cluster-values-ambiguous-provider",
        router=router,
    )
    confirmed = copy.deepcopy(action)
    confirmed["confirmation"] = estimate.errors[0].details["promise_set_hash"]

    first = run_action_spec(
        project,
        confirmed,
        project_id="cluster-values-ambiguous-provider",
        router=router,
    )
    assert first.status == "failed", first.model_dump()
    assert first.errors[0].code == "idempotency_checkpoint_ambiguous"
    assert project.db.execute("SELECT status FROM receipts").fetchone()[0] == "running"

    checkpoint = project.db.execute(
        "SELECT state, payload FROM effect_checkpoints WHERE family='model_call'"
    ).fetchone()
    assert checkpoint is not None
    assert checkpoint["state"] == "reserved"
    assert "vectors" not in json.loads(checkpoint["payload"])
    assert project.db.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0

    retried = run_action_spec(
        project,
        confirmed,
        project_id="cluster-values-ambiguous-provider",
        router=router,
    )

    assert retried.status == "failed"
    assert retried.receipt_id == first.receipt_id
    assert retried.errors == first.errors
    assert len(adapter.calls) == 1
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM receipts WHERE status='completed'"
        ).fetchone()[0]
        == 0
    )


def test_semantic_cluster_operator_attestation_closes_without_repeating_call(
    tmp_path: Path, make_project, monkeypatch
) -> None:
    """An operator resolves ambiguous spend, not permission to repeat the call."""
    from frisket.engine.executor.actions import run_action_spec
    from frisket.engine.store.effect_checkpoints import EffectCheckpointStore

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = make_project("cluster-values-operator-decided")
    seeded = _seed(project, tmp_path)
    router, adapter = _ambiguous_remote_embedding_router()
    action = _cluster_action(
        seeded["sheet_id"],
        idempotency_key="cluster_values@sha256:operator-decided",
        method="semantic",
    )
    estimate = run_action_spec(
        project,
        action,
        project_id="cluster-values-operator-decided",
        router=router,
    )
    confirmed = copy.deepcopy(action)
    confirmed["confirmation"] = estimate.errors[0].details["promise_set_hash"]
    first = run_action_spec(
        project,
        confirmed,
        project_id="cluster-values-operator-decided",
        router=router,
    )
    assert first.status == "failed", first.model_dump()
    assert first.errors[0].code == "idempotency_checkpoint_ambiguous"
    checkpoint = project.db.execute(
        "SELECT id FROM effect_checkpoints WHERE family='model_call'"
    ).fetchone()
    assert checkpoint is not None
    EffectCheckpointStore(project.db).operator_accept_charged(checkpoint["id"])

    retried = run_action_spec(
        project,
        confirmed,
        project_id="cluster-values-operator-decided",
        router=router,
    )

    assert retried.status == "failed"
    assert retried.errors[0].code == "idempotency_checkpoint_operator_decided"
    assert "operator" in retried.errors[0].message.lower()
    assert project.db.execute("SELECT status FROM receipts").fetchone()[0] == "failed"
    assert "name_canonical" not in [
        c["name"] for c in project.columns(seeded["sheet_id"])
    ]
    assert (
        EffectCheckpointStore(project.db).get(checkpoint["id"])["state"] == "consumed"
    )
    assert len(adapter.calls) == 1


@pytest.mark.parametrize(
    "tamper", [None, "identity", "action_kind", "group_key", "vectors"]
)
def test_recovery_binds_returned_vectors_to_actual_admitted_operation(
    tmp_path, make_project, monkeypatch, tamper
):
    """A different action/run or malformed response cannot authorize recovery."""
    from frisket.engine.executor.actions import run_action_spec
    from frisket.retrieval.store import RetrievalStore

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = make_project("cluster-checkpoint-binding")
    seeded = _seed(project, tmp_path)
    router, adapter = _remote_embedding_router()
    action = _cluster_action(seeded["sheet_id"], method="semantic")
    estimate = run_action_spec(project, action, project_id="binding", router=router)
    action["confirmation"] = estimate.errors[0].details["promise_set_hash"]
    original_put = RetrievalStore.put_representations

    def fail_put(self, representations):
        del self, representations
        raise sqlite3.OperationalError("interrupted vector-cache commit")

    monkeypatch.setattr(RetrievalStore, "put_representations", fail_put)
    with pytest.raises(sqlite3.OperationalError, match="interrupted vector-cache"):
        run_action_spec(project, action, project_id="binding", router=router)
    assert len(adapter.calls) == 1
    checkpoint = project.db.execute(
        "SELECT * FROM effect_checkpoints WHERE family='model_call'"
    ).fetchone()
    assert checkpoint["state"] == "returned"
    if tamper in {"identity", "action_kind", "group_key"}:
        foreign = {
            "identity": '{"different_admission":true}',
            "action_kind": "derive.join",
            "group_key": "99999",
        }[tamper]
        project.db.execute(
            f"UPDATE effect_checkpoints SET {tamper}=? WHERE id=?",
            (foreign, checkpoint["id"]),
        )
    elif tamper == "vectors":
        payload = json.loads(checkpoint["payload"])
        payload["vectors"] = []
        project.db.execute(
            "UPDATE effect_checkpoints SET payload=? WHERE id=?",
            (json.dumps(payload), checkpoint["id"]),
        )
    project.db.commit()
    monkeypatch.setattr(RetrievalStore, "put_representations", original_put)
    _recover_abandoned_cluster(project, action, router=router, project_id="binding")
    retried = run_action_spec(project, action, project_id="binding", router=router)
    assert len(adapter.calls) == 1
    assert project.db.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 1
    if tamper is None:
        assert retried.status == "completed", retried.errors
        assert "name_canonical" in [
            c["name"] for c in project.columns(seeded["sheet_id"])
        ]
    else:
        assert retried.status == "failed", retried.model_dump()
        assert "name_canonical" not in [
            c["name"] for c in project.columns(seeded["sheet_id"])
        ]
        assert (
            project.db.execute(
                "SELECT count(*) FROM receipts WHERE status='completed'"
            ).fetchone()[0]
            == 0
        )
