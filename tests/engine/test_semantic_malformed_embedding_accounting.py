"""Returned paid batches remain accounted for even without usable vectors."""

import pytest

from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.retrieval.store import RetrievalStore
from test_semantic_join_publication import case  # noqa: F401


@pytest.mark.parametrize("vectors", [{}, {"vectors": None}, {"vectors": "bad"}])
@pytest.mark.parametrize("cost", [0.01, None])
def test_malformed_paid_batch_is_durable_without_caching_vectors(
    case,  # noqa: F811 - shared pytest fixture
    monkeypatch,
    vectors,
    cost,
):
    calls = []

    def embed(texts):
        calls.append(list(texts))
        return {
            **vectors,
            "provider_id": "openai",
            "actual_model_id": "text-embedding-3-small",
            "provider_cost_usd": cost,
            "provider_reported_cost_usd": cost,
            "cost_source": "provider_reported" if cost is not None else "unknown",
            "usage": {"requests": 1, "input_count": len(texts)},
        }

    monkeypatch.setattr(
        "frisket.ai.embeddings.backends.resolve_embedder",
        lambda *args, **kwargs: (embed, "openai/text-embedding-3-small"),
    )
    gated = case.run()
    assert gated.status == "needs_confirmation", gated
    confirmation = gated.errors[0].details["promise_set_hash"]
    result = case.run(confirmation=confirmation)
    assert result.status == "failed", result
    assert len(calls) == 1
    facts = RunResultStore(case.project).model_calls(result.run_id)
    assert len(facts) == 1
    assert facts[0]["capability"] == "llm.embed"
    assert facts[0]["provider_cost_usd"] == cost
    run = case.project.db.execute(
        "SELECT cost_actual FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert run["cost_actual"] == cost
    receipt = ReceiptStore(case.project).parsed_by_id(result.receipt_id)
    assert receipt.provider_use[0]["cost_actual"] == cost
    with RetrievalStore(case.project) as retrieval:
        assert retrieval.db.execute(
            "SELECT COUNT(*) FROM representations"
        ).fetchone()[0] == 0

    replay = case.run(confirmation=confirmation)
    assert replay.receipt_id == result.receipt_id
    assert len(calls) == 1
    assert len(RunResultStore(case.project).model_calls(result.run_id)) == 1
