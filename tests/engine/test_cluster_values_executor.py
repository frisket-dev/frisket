from __future__ import annotations

import copy
import json
import sqlite3
from array import array
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from action_test_helpers import counts
from executor_harness import CatalogEntry, ExecutorCase, Gate
from frisket.engine.store import Project
from frisket.server.services.action_runs import v1_action_result_http_status
from helpers import replace_test_source_cell


class _RemoteEmbeddingAdapter:
    COST_USD = 0.01

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.on_embed: Callable[[], None] | None = None

    @staticmethod
    def _vectors(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

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
            "dimension": 3,
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

    quote = run_action_spec(
        project,
        action,
        project_id=project_id,
        router=router,
    )
    assert quote.status == "needs_confirmation", quote.errors
    assert v1_action_result_http_status(quote) == 402
    promise_set_hash = quote.errors[0].details["promise_set_hash"]
    retry = copy.deepcopy(action)
    retry["confirmation"] = promise_set_hash
    return quote, run_action_spec(
        project,
        retry,
        project_id=project_id,
        router=router,
    )


def _recover_abandoned_cluster(project, action, *, router, project_id):
    """Model a dead process through the existing stale-lease recovery path."""
    from frisket.engine.executor.actions import run_action_spec
    from frisket.engine.store.output_claims import OutputColumnClaimStore
    from frisket.execution.attempt import abandon_stale_dispatching_attempts

    project.db.execute(
        "UPDATE receipts SET created_at='2000-01-01T00:00:00Z' WHERE status='running'"
    )
    project.db.commit()
    cleared = run_action_spec(project, action, project_id=project_id, router=router)
    assert cleared.errors[0].code == "idempotency_stale_running", cleared.model_dump()
    live = run_action_spec(project, action, project_id=project_id, router=router)
    assert live.errors[0].code == "output_column_busy", live.model_dump()
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
            claim_id=claim["id"], reason="test: expired abandoned writer"
        )


@pytest.mark.parametrize("source", ["missing", "numeric"])
def test_source_descriptor_refusal_explains_supported_types_without_writes(
    tmp_path, source
):
    from frisket.engine.executor.actions import run_action_spec

    project = Project.create(tmp_path / "invalid-cluster-source.frisket")
    try:
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
    finally:
        project.close()


def test_semantic_cluster_remote_cache_miss_requires_confirmation_before_egress(
    tmp_path: Path, monkeypatch
) -> None:
    from frisket.engine.executor.actions import run_action_spec

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = Project.create(tmp_path / "cluster-values-unconfirmed.frisket")
    try:
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
    finally:
        project.close()


def test_semantic_cluster_wrong_exact_echo_repauses(
    tmp_path: Path, monkeypatch
) -> None:
    from frisket.engine.executor.actions import run_action_spec

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = Project.create(tmp_path / "cluster-values-bare-confirmed.frisket")
    try:
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
    finally:
        project.close()


def test_semantic_cluster_source_change_invalidates_old_confirmation_hash(
    tmp_path: Path, monkeypatch
) -> None:
    from frisket.engine.executor.actions import run_action_spec

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = Project.create(tmp_path / "cluster-values-stale-confirmation.frisket")
    try:
        seeded = _seed(project, tmp_path)
        router, adapter = _remote_embedding_router()
        action = _cluster_action(
            seeded["sheet_id"],
            idempotency_key="cluster_values@sha256:stale-confirmation",
            method="semantic",
        )
        quote = run_action_spec(
            project,
            action,
            project_id="cluster-values-stale-confirmation",
            router=router,
        )
        old_hash = quote.errors[0].details["promise_set_hash"]
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
    finally:
        project.close()


def test_semantic_cluster_refuses_an_exhausted_project_key_before_embedding(
    tmp_path: Path, monkeypatch
) -> None:
    """cluster.values bypasses MapRunner, but its semantic warm can call the
    project's OpenAI embedding key.  An exhausted cap must stop that pre-txn
    warm before any text leaves the process."""
    from frisket.team.security.secrets import encrypt_secret, key_hint

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = Project.create(tmp_path / "cluster-values-exhausted.frisket")
    try:
        seeded = _seed(project, tmp_path)
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
    finally:
        project.close()


def test_semantic_cluster_persists_remote_fact_and_receipts_the_provider_cost(
    tmp_path: Path, monkeypatch
) -> None:
    """The semantic warm is a paid provider effect, not a local clustering
    detail.  Its durable fact, project-key accrual, and completed receipt must
    all describe the same remote call and amount."""
    from frisket.team.security.secrets import encrypt_secret, key_hint

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = Project.create(tmp_path / "cluster-values-provider-fact.frisket")
    try:
        seeded = _seed(project, tmp_path)
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
    finally:
        project.close()


def test_semantic_cluster_replay_and_vector_cache_do_not_double_spend(
    tmp_path: Path, monkeypatch
) -> None:
    """An idempotency replay reuses the original receipt, while a distinct
    action over the same content uses the vector cache. Neither is a new
    provider effect, even after the first call has exhausted the key's cap."""
    from frisket.engine.executor.actions import run_action_spec
    from frisket.team.security.secrets import encrypt_secret, key_hint

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = Project.create(tmp_path / "cluster-values-cache-accounting.frisket")
    try:
        seeded = _seed(project, tmp_path)
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
    finally:
        project.close()


@pytest.mark.parametrize("sidecar_commit_landed", [False, True])
def test_semantic_cluster_sidecar_commit_failure_reconciles_without_second_call(
    tmp_path: Path, monkeypatch, sidecar_commit_landed: bool
) -> None:
    """A paid batch can return before the rebuildable vector sidecar commits.

    The exact confirmed retry must reconcile that returned batch from durable
    project state, not buy it again. The failed materialization has no completed
    receipt or canonical output to make the first attempt look successful.
    """
    from frisket import semantic
    from frisket.engine.executor.actions import run_action_spec
    from frisket.team.security.secrets import encrypt_secret, key_hint

    class _FailFirstCommit:
        def __init__(self, db: sqlite3.Connection) -> None:
            self._db = db

        def __getattr__(self, name: str) -> Any:
            return getattr(self._db, name)

        def commit(self) -> None:
            if sidecar_commit_landed:
                self._db.commit()
            else:
                self._db.rollback()
            self._db.close()
            raise sqlite3.OperationalError("injected vector sidecar commit failure")

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = Project.create(
        tmp_path / f"cluster-values-sidecar-failure-{sidecar_commit_landed}.frisket"
    )
    try:
        seeded = _seed(project, tmp_path)
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
        quote = run_action_spec(
            project,
            action,
            project_id="cluster-values-sidecar-failure",
            router=router,
        )
        confirmed = copy.deepcopy(action)
        confirmed["confirmation"] = quote.errors[0].details["promise_set_hash"]

        real_sidecar = semantic._sidecar  # noqa: SLF001
        fail_next = True

        def _sidecar_with_one_failed_commit(project_: Project):
            nonlocal fail_next
            db = real_sidecar(project_)
            if not fail_next:
                return db
            fail_next = False
            return _FailFirstCommit(db)

        monkeypatch.setattr(semantic, "_sidecar", _sidecar_with_one_failed_commit)
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial cache warming must not renumber paid embedding checkpoints."""
    from frisket import semantic
    from frisket.engine.executor.actions import run_action_spec
    from frisket.semantic import _vec_key
    from frisket.team.security.secrets import encrypt_secret, key_hint

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = Project.create(tmp_path / "cluster-values-stable-batches.frisket")
    try:
        sheet_id = project.add_sheet("Organizations")
        column_id = project.add_column(sheet_id, "name", type="text")
        values = [f"organization-{index:03d}" for index in range(501)]
        project.add_rows(
            sheet_id,
            [{"name": value} for value in values],
            {"name": column_id},
        )
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
        quote = run_action_spec(
            project,
            action,
            project_id="cluster-values-stable-batches",
            router=router,
        )
        confirmed = copy.deepcopy(action)
        confirmed["confirmation"] = quote.errors[0].details["promise_set_hash"]

        real_sidecar = semantic._sidecar  # noqa: SLF001
        fail_next = True

        class _FailAfterBothBatches:
            def __init__(self, db: sqlite3.Connection) -> None:
                self._db = db

            def __getattr__(self, name: str) -> Any:
                return getattr(self._db, name)

            def commit(self) -> None:
                nonlocal fail_next
                if fail_next and len(adapter.calls) >= 2:
                    fail_next = False
                    self._db.rollback()
                    self._db.close()
                    raise sqlite3.OperationalError(
                        "injected second semantic batch sidecar failure"
                    )
                self._db.commit()

        monkeypatch.setattr(
            semantic,
            "_sidecar",
            lambda project_: _FailAfterBothBatches(real_sidecar(project_)),
        )
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
        monkeypatch.setattr(semantic, "_sidecar", real_sidecar)
        sidecar = real_sidecar(project)
        sidecar.execute("DELETE FROM cell_vec")
        sidecar.executemany(
            "INSERT INTO cell_vec (key, vec) VALUES (?, ?)",
            [
                (
                    _vec_key("openai/text-embedding-3-small", value),
                    array("f", [1.0, 0.0, 0.0]).tobytes(),
                )
                for value in values[:250]
            ],
        )
        sidecar.commit()
        sidecar.close()

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
    finally:
        project.close()


def test_semantic_cluster_terminal_receipt_and_context_transfer_are_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor.actions import run_action_spec
    from frisket.engine.executor import cluster_action as host
    from frisket.team.security.secrets import encrypt_secret, key_hint

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = Project.create(tmp_path / "cluster-values-receipt-atomic.frisket")
    try:
        seeded = _seed(project, tmp_path)
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
        quote = run_action_spec(
            project,
            action,
            project_id="cluster-values-receipt-atomic",
            router=router,
        )
        confirmed = copy.deepcopy(action)
        confirmed["confirmation"] = quote.errors[0].details["promise_set_hash"]
        original_context = {
            "funding_account_id": 17,
            "reservation_id": "atomic-transfer",
        }
        original_finalize = host._finalize_reserved_action_receipt
        fail_next = True

        def fail_first_receipt_update(*args, **kwargs):
            nonlocal fail_next
            result = original_finalize(*args, **kwargs)
            if fail_next:
                fail_next = False
                raise sqlite3.OperationalError("injected receipt update failure")
            return result

        monkeypatch.setattr(
            host, "_finalize_reserved_action_receipt", fail_first_receipt_update
        )

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
            "SELECT status,edition_run_context FROM receipts"
        ).fetchone()
        assert receipt["status"] == "running"
        assert json.loads(receipt["edition_run_context"]) == original_context
        assert (
            project.db.execute("SELECT COUNT(*) FROM cell_result_heads").fetchone()[0]
            == 0
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

        replay = run_action_spec(
            project,
            confirmed,
            project_id="cluster-values-receipt-atomic",
            router=router,
            edition_run_context={"reservation_id": "replacement-must-not-win"},
        )

        assert replay.status == "completed", replay.errors
        assert len(adapter.calls) == 1
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM effect_checkpoints WHERE family='model_call'"
            ).fetchone()[0]
            == 0
        )
        receipt = project.db.execute(
            "SELECT edition_run_context FROM receipts WHERE id=?",
            (replay.receipt_id,),
        ).fetchone()
        assert json.loads(receipt["edition_run_context"]) == original_context
    finally:
        project.close()


def test_semantic_cluster_ambiguous_provider_outcome_refuses_exact_retry(
    tmp_path: Path, monkeypatch
) -> None:
    """Once egress starts, an exception without returned metadata is ambiguous.

    The durable reservation must block the exact retry rather than guessing
    that the first provider attempt was free and buying a second batch.
    """
    from frisket.engine.executor.actions import run_action_spec

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = Project.create(tmp_path / "cluster-values-ambiguous-provider.frisket")
    try:
        seeded = _seed(project, tmp_path)
        router, adapter = _ambiguous_remote_embedding_router()
        action = _cluster_action(
            seeded["sheet_id"],
            idempotency_key="cluster_values@sha256:ambiguous-provider",
            method="semantic",
        )
        quote = run_action_spec(
            project,
            action,
            project_id="cluster-values-ambiguous-provider",
            router=router,
        )
        confirmed = copy.deepcopy(action)
        confirmed["confirmation"] = quote.errors[0].details["promise_set_hash"]

        first = run_action_spec(
            project,
            confirmed,
            project_id="cluster-values-ambiguous-provider",
            router=router,
        )
        assert first.status == "failed", first.model_dump()
        assert first.errors[0].code == "idempotency_checkpoint_ambiguous"
        assert (
            project.db.execute("SELECT status FROM receipts").fetchone()[0] == "running"
        )

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
    finally:
        project.close()


def test_semantic_cluster_operator_attestation_closes_without_repeating_call(
    tmp_path: Path, monkeypatch
) -> None:
    """An operator resolves ambiguous spend, not permission to repeat the call."""
    from frisket.engine.executor.actions import run_action_spec
    from frisket.engine.store.effect_checkpoints import EffectCheckpointStore

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = Project.create(tmp_path / "cluster-values-operator-decided.frisket")
    try:
        seeded = _seed(project, tmp_path)
        router, adapter = _ambiguous_remote_embedding_router()
        action = _cluster_action(
            seeded["sheet_id"],
            idempotency_key="cluster_values@sha256:operator-decided",
            method="semantic",
        )
        quote = run_action_spec(
            project,
            action,
            project_id="cluster-values-operator-decided",
            router=router,
        )
        confirmed = copy.deepcopy(action)
        confirmed["confirmation"] = quote.errors[0].details["promise_set_hash"]
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
        assert (
            project.db.execute("SELECT status FROM receipts").fetchone()[0] == "failed"
        )
        assert "name_canonical" not in [
            c["name"] for c in project.columns(seeded["sheet_id"])
        ]
        assert (
            EffectCheckpointStore(project.db).get(checkpoint["id"])["state"]
            == "consumed"
        )
        assert len(adapter.calls) == 1
    finally:
        project.close()


@pytest.mark.parametrize(
    "tamper", [None, "identity", "action_kind", "group_key", "vectors"]
)
def test_recovery_binds_returned_vectors_to_actual_admitted_operation(
    tmp_path, monkeypatch, tamper
):
    """A different action/run or malformed response cannot authorize recovery."""
    from frisket import semantic
    from frisket.engine.executor.actions import run_action_spec

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project = Project.create(tmp_path / "cluster-checkpoint-binding.frisket")
    try:
        seeded = _seed(project, tmp_path)
        router, adapter = _remote_embedding_router()
        action = _cluster_action(seeded["sheet_id"], method="semantic")
        quote = run_action_spec(project, action, project_id="binding", router=router)
        action["confirmation"] = quote.errors[0].details["promise_set_hash"]
        original_sidecar = semantic._sidecar

        class FailedCommit:
            def __init__(self, db):
                self.db = db

            def __getattr__(self, name):
                return getattr(self.db, name)

            def commit(self):
                self.db.rollback()
                self.db.close()
                raise sqlite3.OperationalError("interrupted vector-cache commit")

        monkeypatch.setattr(
            semantic, "_sidecar", lambda p: FailedCommit(original_sidecar(p))
        )
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
        monkeypatch.setattr(semantic, "_sidecar", original_sidecar)
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
    finally:
        project.close()
