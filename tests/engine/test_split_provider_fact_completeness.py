from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from runner_test_helpers import run_action_with_exact_confirmation

pytestmark = pytest.mark.gap

_VERSION = re.compile(r"^[a-z][a-z0-9_.-]*v[1-9][0-9]*$")
_SECRETISH = re.compile(
    r"(?i)(?:api[_-]?key|secret|token|password|credential[_-]?value|key[_-]?hint)"
)


def _project(tmp_path: Path, name: str):
    from frisket.engine.store import Project

    project = Project.create(tmp_path / f"{name}.frisket", name=name)
    sheet_id = project.add_sheet("Stories")
    columns = {"story": project.add_column(sheet_id, "story", type="text")}
    project.add_rows(
        sheet_id, [{"story": "A city awarded a no-bid contract."}], columns
    )
    return project, sheet_id


def _map_action(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": "openai/gpt-5-mini",
            "context": "Classify this city-news story.",
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": ["accountability", "other"],
                    "description": "Editorial beat.",
                }
            ],
        },
        "idempotency_key": "provider-facts-structured@1",
    }


def _assert_no_secret_material(value: Any, *, forbidden: set[str]) -> None:
    rendered = json.dumps(value, sort_keys=True, default=str)
    if isinstance(value, dict):
        for key in value:
            assert not _SECRETISH.search(str(key)), (
                f"provider fact field {key!r} suggests credential material"
            )
    for secret in forbidden:
        assert secret not in rendered, (
            "provider facts must retain source enums, never keys"
        )


def _assert_fact_shape(fact: dict[str, Any], *, expected_source: str) -> None:
    assert fact.get("credential_source") == expected_source
    assert fact.get("provider") == "openai"
    assert fact.get("model_ids") or fact.get("model") or fact.get("actual_model_id")
    version = fact.get("fact_version", fact.get("accounting_version"))
    assert isinstance(version, str) and _VERSION.fullmatch(version), (
        "each persisted provider fact needs an explicit version token"
    )
    assert fact.get("units") or fact.get("provider_cost_usd") is not None, (
        "provider facts need units or provider-cost evidence"
    )


def test_structured_map_persists_cache_and_live_attempt_facts_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real map persistence path must not relabel a cache/live total.

    The structured completer is deliberately stubbed at its wire-result seam,
    while ``run_action_spec`` and ``RunResultStore`` remain production code.
    This models a cache hit followed by a live schema repair without network IO.
    Implementations may persist one fact per attempt or source-partitioned facts;
    the observable invariant is that their units/cost are not collapsed beneath
    the last (live) source.
    """
    from frisket.ai.llm import LLMResponse, ModelRouter
    from frisket.ai.llm.structured import _sum_response
    from frisket.engine.store.runs import RunResultStore

    cache_secret = "cache-never-a-key"
    live_secret = "live-key-never-persisted"
    # ``provider`` is stamped by ModelRouter on every response leaving the wire
    # seam -- live, chaos-fabricated and cache replay alike -- so a double that
    # left it at its pre-stamp default would be a shape the stubbed seam cannot
    # actually emit.
    wire_calls = [
        LLMResponse(
            content='{"beat":"accountability"}',
            data={"beat": "accountability"},
            tokens_in=3,
            tokens_out=2,
            # A cache cassette retains the historical response cost.  It is
            # evidence about the original call, not spend by this replay.
            cost=0.0042,
            model="openai/gpt-5-mini",
            cached=True,
            provider="openai",
            credential_source="cache",
            raw={"opaque": cache_secret},
        ),
        LLMResponse(
            content='{"beat":"accountability"}',
            data={"beat": "accountability"},
            tokens_in=11,
            tokens_out=7,
            cost=0.0125,
            model="openai/gpt-5-mini",
            provider="openai",
            credential_source="platform_key",
            raw={"opaque": live_secret},
        ),
    ]

    class _StructuredCompleter:
        def __init__(self, _router: Any):
            pass

        async def complete(self, request: Any, **_kwargs: Any) -> Any:
            # This is the actual structured receipt summary currently handed to
            # MapRunner.  The explicit wire evidence is available to a corrected
            # producer without prescribing its representation or table layout.
            return SimpleNamespace(
                data={"beat": "accountability"},
                response=_sum_response(
                    request.model, wire_calls, {"beat": "accountability"}
                ),
                wire_calls=wire_calls,
            )

    monkeypatch.setattr(
        "frisket.engine.runner.row_execution.StructuredCompleter", _StructuredCompleter
    )
    project, sheet_id = _project(tmp_path, "structured-attempts")
    try:
        result = run_action_with_exact_confirmation(
            project,
            _map_action(sheet_id),
            project_id="provider-fact-contract",
            router=ModelRouter(keys={"openai": live_secret}),
        )
        assert result.status == "completed", result.errors
        rows = [dict(row) for row in RunResultStore(project).model_calls(result.run_id)]
        assert rows, "structured map calls must persist provider facts"

        cache_rows = [row for row in rows if row.get("credential_source") == "cache"]
        live_rows = [
            row for row in rows if row.get("credential_source") == "platform_key"
        ]
        assert cache_rows and live_rows, (
            "a mixed cache/live structured response must expose persisted facts at "
            "attempt or source-partition granularity, never one total labelled by "
            "the final live source"
        )
        for fact in cache_rows:
            _assert_fact_shape(fact, expected_source="cache")
            assert float(fact.get("provider_cost_usd") or 0.0) == 0.0
        for fact in live_rows:
            _assert_fact_shape(fact, expected_source="platform_key")
            assert float(fact.get("provider_cost_usd") or 0.0) == pytest.approx(0.0125)
        run = RunResultStore(project).get_run(result.run_id)
        persisted_provider_cost = sum(
            float(row.get("provider_cost_usd") or 0.0) for row in rows
        )
        assert run is not None
        assert float(run["cost_actual"]) == pytest.approx(persisted_provider_cost)
        _assert_no_secret_material(rows, forbidden={cache_secret, live_secret})
    finally:
        project.close()


def test_exhausted_structured_repair_still_persists_paid_wire_facts(
    tmp_path: Path,
) -> None:
    """Invalid output cannot erase successful, billable provider calls.

    All four HTTP calls below return successfully and carry provider cost; only
    their JSON fails the requested schema.  The failed row/run/receipt must
    therefore retain all four facts and accrue all four against the project key.
    Otherwise the next launch can pass a cap that the provider has already
    billed through.
    """
    from frisket.ai.llm import LLMResponse, ModelRouter
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.store.runs import RunResultStore
    from frisket.team.security.secrets import encrypt_secret, key_hint

    secret = "structured-exhaustion-project-key"

    class _InvalidStructuredAdapter:
        def __init__(self) -> None:
            self.seen: list[Any] = []

        async def complete(self, req: Any, _client: Any) -> LLMResponse:
            self.seen.append(req)
            return LLMResponse(
                content='{"not_beat":"wrong"}',
                data={"not_beat": "wrong"},
                tokens_in=10,
                tokens_out=4,
                cost=0.006,
                model=req.model,
                raw={"id": f"paid-invalid-{len(self.seen)}"},
            )

    adapter = _InvalidStructuredAdapter()
    router = ModelRouter(
        keys={"openai": secret},
        key_sources={"openai": "project_key"},
        cache_mode="off",
        max_retries=0,
        use_env_keys=False,
    )
    router._adapters["openai"] = adapter  # noqa: SLF001 - hermetic wire double

    project, sheet_id = _project(tmp_path, "structured-exhaustion")
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret(secret),
        hint=key_hint(secret),
        spend_cap_micro=1_000_000,
    )
    try:
        result = run_action_with_exact_confirmation(
            project,
            _map_action(sheet_id),
            project_id="structured-exhaustion-contract",
            router=router,
        )
        assert result.status == "failed"
        assert len(adapter.seen) == 4, "one initial call plus three schema repairs"

        store = RunResultStore(project)
        rows = [dict(row) for row in store.model_calls(result.run_id)]
        assert len(rows) == 4, "paid invalid responses remain accounting facts"
        assert {row["credential_source"] for row in rows} == {"project_key"}
        assert sum(float(row["provider_cost_usd"]) for row in rows) == pytest.approx(
            0.024
        )

        spend = project.provider_spend_state("openai")
        assert spend is not None
        assert spend.spent_micro == 24_000

        run = store.get_run(result.run_id)
        assert run is not None
        assert float(run["cost_actual"]) == pytest.approx(0.024)
        receipt = ReceiptStore(project).parsed_by_id(str(result.receipt_id))
        assert receipt is not None
        assert sum(
            float(item.get("cost_actual") or 0.0) for item in receipt.provider_use
        ) == pytest.approx(0.024)
    finally:
        project.close()


def test_output_limited_structured_response_persists_one_neutral_wire_fact(
    tmp_path: Path,
) -> None:
    """A valid-but-limited payload fails once without losing paid-call evidence."""
    from frisket.ai.llm import LLMResponse, ModelRouter
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.store.runs import RunResultStore
    from frisket.team.security.secrets import encrypt_secret, key_hint

    secret = "output-limit-project-key"
    provider_reason = "max_tokens"

    class _LimitedStructuredAdapter:
        def __init__(self) -> None:
            self.seen: list[Any] = []

        async def complete(self, req: Any, _client: Any) -> LLMResponse:
            self.seen.append(req)
            return LLMResponse(
                content='{"beat":"accountability"}',
                data={"beat": "accountability"},
                tokens_in=10,
                tokens_out=4,
                cost=0.006,
                model=req.model,
                raw={"stop_reason": provider_reason},
                output_limited=True,
            )

    adapter = _LimitedStructuredAdapter()
    router = ModelRouter(
        keys={"openai": secret},
        key_sources={"openai": "project_key"},
        cache_mode="off",
        max_retries=0,
        use_env_keys=False,
    )
    router._adapters["openai"] = adapter  # noqa: SLF001 - hermetic wire double

    project, sheet_id = _project(tmp_path, "structured-output-limit")
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret(secret),
        hint=key_hint(secret),
        spend_cap_micro=1_000_000,
    )
    try:
        result = run_action_with_exact_confirmation(
            project,
            _map_action(sheet_id),
            project_id="structured-output-limit-contract",
            router=router,
        )
        assert result.status == "failed"
        assert len(adapter.seen) == 1, "output limits must not enter schema repair"

        store = RunResultStore(project)
        [row] = [dict(item) for item in store.model_calls(result.run_id)]
        assert json.loads(row["units"]) == {
            "tokens_in": 10,
            "tokens_out": 4,
            "output_limited": True,
        }
        assert float(row["provider_cost_usd"]) == pytest.approx(0.006)

        receipt = ReceiptStore(project).parsed_by_id(str(result.receipt_id))
        assert receipt is not None
        durable = json.dumps(
            {"model_call": row, "receipt": receipt.model_dump(mode="json")},
            sort_keys=True,
        )
        assert provider_reason not in durable
        assert secret not in durable
    finally:
        project.close()


def test_paid_invalid_wire_survives_transport_failure_during_repair(
    tmp_path: Path,
) -> None:
    """A later transport failure cannot erase an earlier paid response.

    The first provider request completes and is billed, but its JSON violates
    the requested schema.  The corrective request then fails before producing
    a response.  The failed row/run/receipt must still account for the one
    successful wire call rather than treating the entire repair session as if
    no provider work occurred.
    """
    from frisket.ai.llm import LLMError, LLMResponse, ModelRouter
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.store.runs import RunResultStore
    from frisket.team.security.secrets import encrypt_secret, key_hint

    secret = "repair-transport-failure-project-key"

    class _InvalidThenTransportFailureAdapter:
        def __init__(self) -> None:
            self.seen: list[Any] = []

        async def complete(self, req: Any, _client: Any) -> LLMResponse:
            self.seen.append(req)
            if len(self.seen) == 1:
                return LLMResponse(
                    content='{"not_beat":"wrong"}',
                    data={"not_beat": "wrong"},
                    tokens_in=10,
                    tokens_out=4,
                    cost=0.006,
                    model=req.model,
                    raw={"id": "paid-invalid-before-transport-failure"},
                )
            raise LLMError("repair transport failed", retryable=False)

    adapter = _InvalidThenTransportFailureAdapter()
    router = ModelRouter(
        keys={"openai": secret},
        key_sources={"openai": "project_key"},
        cache_mode="off",
        max_retries=0,
        use_env_keys=False,
    )
    router._adapters["openai"] = adapter  # noqa: SLF001 - hermetic wire double

    project, sheet_id = _project(tmp_path, "repair-transport-failure")
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret(secret),
        hint=key_hint(secret),
        spend_cap_micro=1_000_000,
    )
    try:
        result = run_action_with_exact_confirmation(
            project,
            _map_action(sheet_id),
            project_id="repair-transport-failure-contract",
            router=router,
        )
        assert result.status == "failed"
        assert len(adapter.seen) == 2, "one paid response plus one failed repair"

        store = RunResultStore(project)
        rows = [dict(row) for row in store.model_calls(result.run_id)]
        assert len(rows) == 1, "the completed provider response remains a fact"
        assert rows[0]["credential_source"] == "project_key"
        assert float(rows[0]["provider_cost_usd"]) == pytest.approx(0.006)

        spend = project.provider_spend_state("openai")
        assert spend is not None
        assert spend.spent_micro == 6_000

        run = store.get_run(result.run_id)
        assert run is not None
        assert float(run["cost_actual"]) == pytest.approx(0.006)
        receipt = ReceiptStore(project).parsed_by_id(str(result.receipt_id))
        assert receipt is not None
        assert sum(
            float(item.get("cost_actual") or 0.0) for item in receipt.provider_use
        ) == pytest.approx(0.006)
    finally:
        project.close()


class _EmbeddingAdapter:
    async def embed_with_meta(self, texts: list[str], model: str, _client: Any):
        return (
            [[1.0] + [0.0] * 1535 for _ in texts],
            {
                "requested_model": model,
                "actual_model_id": "text-embedding-3-small",
                "usage": {"input_tokens": 17, "input_count": len(texts)},
                "provider_request_id": "embedding-stub-request",
                "provider_cost_usd": 0.00031,
            },
        )


def _embedding_create_action(
    sheet_id: int,
    *,
    provider: str = "openai",
    model: str = "text-embedding-3-small",
) -> dict[str, Any]:
    return {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "source_columns": ["story"],
            "modality": "text",
            "provider": provider,
            "model": model,
            "source_policy": {"kind": "text_cell"},
            "provider_policy": {"allow_remote": provider != "fastembed"},
        },
        "idempotency_key": "provider-facts-embedding-create@1",
    }


def _embedding_refresh_action(index_id: str) -> dict[str, Any]:
    return {
        "action_id": "embedding.index_refresh",
        "scope": {"kind": "project"},
        "params": {"index_id": index_id, "mode": "full"},
        "idempotency_key": "provider-facts-embedding-refresh@1",
    }


def _nested_provider_facts(value: Any) -> list[dict[str, Any]]:
    """Locate persisted facts by their semantic fields, not a receipt key name."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if "credential_source" in value and (
            "provider" in value or "provider_id" in value
        ):
            found.append(value)
        for child in value.values():
            found.extend(_nested_provider_facts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_nested_provider_facts(child))
    return found


def test_remote_embedding_batch_and_refresh_receipt_keep_versioned_fact(
    tmp_path: Path,
) -> None:
    """Remote embedding provenance survives the production refresh receipt path."""
    from frisket.ai.embeddings import EmbeddingGateway
    from frisket.engine.executor import ExecutorDeps, run_action_spec
    from frisket.ai.llm import ModelRouter
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.store.runs import RunResultStore

    secret = "embedding-key-never-persisted"
    router = ModelRouter(
        keys={"openai": secret}, key_sources={"openai": "org_byok"}, cache_mode="off"
    )
    router._adapters["openai"] = _EmbeddingAdapter()  # noqa: SLF001 - hermetic adapter
    batch = asyncio.run(
        router.embed_batch(
            ["A city awarded a no-bid contract."], model="openai/text-embedding-3-small"
        )
    )
    assert batch["provider_id"] == "openai"
    assert batch["actual_model_id"] == "text-embedding-3-small"
    assert batch["usage"]["input_tokens"] == 17

    project, sheet_id = _project(tmp_path, "embedding-facts")
    try:
        create = run_action_spec(
            project, _embedding_create_action(sheet_id), project_id="embedding-contract"
        )
        assert create.status == "completed", create.errors
        index_id = create.outputs[0].ref["index_id"]
        refresh = run_action_spec(
            project,
            _embedding_refresh_action(index_id),
            project_id="embedding-contract",
            deps=ExecutorDeps(
                router=router, embedding_gateway=EmbeddingGateway(router=router)
            ),
        )
        assert refresh.status == "completed", refresh.errors
        assert refresh.run_id is not None, (
            "a remote embedding refresh must own a run so its provider fact can "
            "enter the same durable model_calls ledger consumed by settlement"
        )
        receipt = ReceiptStore(project).parsed_by_id(refresh.receipt_id)
        assert receipt is not None, "embedding refresh must persist a receipt"
        assert receipt.run_id == refresh.run_id
        facts = _nested_provider_facts(receipt.model_dump(mode="json"))
        assert facts, (
            "remote embedding refresh must persist an inspectable provider fact"
        )
        fact = next(
            (item for item in facts if item.get("credential_source") == "org_byok"),
            None,
        )
        assert fact is not None, (
            "refresh fact lost the remote embedding credential source"
        )
        assert fact.get("provider", fact.get("provider_id")) == "openai"
        assert fact.get("model") or fact.get("model_ids") or fact.get("actual_model_id")
        version = fact.get("fact_version", fact.get("accounting_version"))
        assert isinstance(version, str) and _VERSION.fullmatch(version)
        assert fact.get("units") or fact.get("provider_cost_usd") is not None
        _assert_no_secret_material(fact, forbidden={secret})

        persisted = [
            dict(row) for row in RunResultStore(project).model_calls(refresh.run_id)
        ]
        stored = next(
            (item for item in persisted if item.get("credential_source") == "org_byok"),
            None,
        )
        assert stored is not None, (
            "the embedding receipt is not sufficient: its provider fact must be "
            "persisted in RunResultStore.model_calls for the refresh run"
        )
        _assert_fact_shape(stored, expected_source="org_byok")
        # This is model usage, but not completion usage.  C2's private
        # fact-pricer must explicitly support the truthful model-call token;
        # do not disguise embedding traffic as ``llm.complete``.
        assert stored["capability"] == "llm.embed"
        _assert_no_secret_material(stored, forbidden={secret})

        assert batch.get("credential_source") == "org_byok", (
            "embed_batch must preserve the selected non-secret credential source"
        )
    finally:
        project.close()


def test_local_embedding_refresh_persists_known_free_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local producer fact remains known-free through every durable consumer."""
    from frisket.ai.embeddings import EmbeddingGateway
    from frisket.engine.executor import ExecutorDeps, run_action_spec
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.store.runs import RunResultStore
    from frisket.server.services.spend import _unpriced_models_with_usage

    model = "BAAI/bge-small-en-v1.5"

    def local_embedder(requested_model=None, *, env=None):
        del env
        actual_model = requested_model or model
        return (
            lambda texts: [[1.0] + [0.0] * 383 for _ in texts],
            actual_model,
        )

    monkeypatch.setattr("frisket.semantic.local_embedder", local_embedder)
    project, sheet_id = _project(tmp_path, "local-embedding-facts")
    try:
        create = run_action_spec(
            project,
            _embedding_create_action(sheet_id, provider="fastembed", model=model),
            project_id="local-embedding-contract",
        )
        assert create.status == "completed", create.errors
        index_id = create.outputs[0].ref["index_id"]
        refresh = run_action_spec(
            project,
            _embedding_refresh_action(index_id),
            project_id="local-embedding-contract",
            deps=ExecutorDeps(embedding_gateway=EmbeddingGateway()),
        )
        assert refresh.status == "completed", refresh.errors
        assert refresh.run_id is not None

        receipt = ReceiptStore(project).parsed_by_id(refresh.receipt_id)
        assert receipt is not None
        facts = _nested_provider_facts(receipt.model_dump(mode="json"))
        fact = next(item for item in facts if item.get("provider") == "fastembed")
        assert fact["provider_kind"] == "local_process"
        assert fact["credential_source"] == "local"
        assert fact["provider_reported_cost_usd"] is None
        assert fact["provider_cost_usd"] == 0.0
        assert fact["cost_source"] == "free_local"
        assert receipt.provider_use == [
            {
                "provider": "fastembed",
                "service": "embedding.index_refresh",
                "external_api": False,
                "cost_actual": 0.0,
            }
        ]

        store = RunResultStore(project)
        [stored] = [dict(row) for row in store.model_calls(refresh.run_id)]
        assert stored["provider"] == "fastembed"
        assert stored["engine"] == f"fastembed/{model}"
        assert stored["provider_reported_cost_usd"] is None
        assert stored["provider_cost_usd"] == 0.0
        assert stored["cost_source"] == "free_local"
        run = store.get_run(refresh.run_id)
        assert run is not None
        assert float(run["cost_actual"]) == 0.0
        assert _unpriced_models_with_usage(project) == set()
    finally:
        project.close()
