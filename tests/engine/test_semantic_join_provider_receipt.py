from __future__ import annotations

import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor.actions import _default_map_runner_factory
from frisket.engine.executor.semantic_join_action import run_typed_semantic_join_action
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore


@pytest.mark.parametrize(
    ("model", "provider_kind", "cost"),
    [
        ("fastembed/test", "local_process", 0.0),
        ("openai/text-embedding-3-small", "platform_api", 0.0007),
        ("openai/text-embedding-3-small", "platform_api", None),
        ("ollama/@test/test", "local_http", 0.0),
    ],
)
def test_semantic_receipt_projects_only_current_run_embedding_facts(
    tmp_path, monkeypatch, model, provider_kind, cost
):
    calls = []
    provider, actual_model = model.split("/", 1)

    def embed(texts):
        calls.append(list(texts))
        vectors = [[1.0, 0.0] for _ in texts]
        if provider == "fastembed":
            return vectors
        return {
            "vectors": vectors,
            "provider_id": provider,
            "provider_kind": provider_kind,
            "actual_model_id": actual_model,
            "credential_source": "none",
            "usage": {"input_count": len(texts), "requests": 1},
            "provider_cost_usd": cost,
            "provider_reported_cost_usd": cost,
            "cost_source": "provider_reported" if cost is not None else "unknown",
        }

    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder", lambda *a, **kw: (embed, model)
    )
    project = Project.create(tmp_path / "provider-receipt.frisket")
    try:
        source = project.add_sheet("Donors")
        donor = project.add_column(source, "donor")
        project.add_rows(source, [{"donor": "ACME"}], {"donor": donor})
        target = project.add_sheet("Registry")
        company = project.add_column(target, "company")
        project.add_rows(target, [{"company": "Acme"}], {"company": company})

        for index in range(2):
            body = {
                "action_id": "join.semantic",
                "scope": {"kind": "sheet_rows", "sheet_id": source},
                "params": {
                    "source": "donor",
                    "target": {"sheet_id": target, "column": "company"},
                },
                "sheet_name": f"Matches {index}",
                "output_names": {
                    name: f"{name}_{index}"
                    for name in ("match_value", "match_score", "matched_row_id")
                },
                "idempotency_key": f"provider-receipt-{index}",
            }

            def run():
                bound = BoundTypedActionRequest.bind(
                    ACTION_REGISTRY.get("join.semantic"),
                    ActionRequest.model_validate(body),
                )
                return run_typed_semantic_join_action(
                    project, "test", bound, None, _default_map_runner_factory
                )

            result = run()
            if result.status == "needs_confirmation":
                body["confirmation"] = result.errors[0].details["promise_set_hash"]
                result = run()
            assert result.status == "completed", [
                error.message for error in result.errors
            ]
            receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
            facts = RunResultStore(project).model_calls(result.run_id)
            # The second action uses cached source and target vectors. It must
            # neither call the provider nor inherit the first run's paid facts.
            assert len(calls) == 2
            if index == 1 or provider == "fastembed":
                assert facts == []
                assert receipt.provider_use == []
                continue
            assert len(facts) == 2
            assert {fact["capability"] for fact in facts} == {"llm.embed"}
            assert {fact["provider"] for fact in facts} == {provider}
            assert {fact["provider_kind"] for fact in facts} == {provider_kind}
            assert receipt.provider_use == [
                {
                    "provider": provider,
                    "engine": model,
                    "service": "llm.embed",
                    "external_api": provider_kind == "platform_api",
                    "credential_source": "none",
                    "request_count": len(facts),
                    "model_call_count": len(facts),
                    "cost_actual": None if cost is None else cost * len(facts),
                }
            ]
    finally:
        project.close()
