from __future__ import annotations

import asyncio
import json

import pytest

from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.ai.research.row_answer import (
    MAX_STEPS,
    fetch_page,
    model_call_accounting,
    run_research,
)


class ScriptedResearchAdapter:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.requests = []

    async def complete(self, request, client):
        self.requests.append(request)
        turn = next(self.turns)
        return LLMResponse(
            content=turn if isinstance(turn, str) else None,
            tool_calls=turn if isinstance(turn, list) else None,
            data=None,
            tokens_in=10,
            tokens_out=5,
            cost=0.001,
            model=request.model,
        )


SEARCH = [{"name": "search", "args": {"query": "official record"}, "id": "q1"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("verify_first", [False, True])
@pytest.mark.parametrize(
    "goal", ["Find Acme records", "Find {{literal braces}} records"]
)
async def test_domain_loop_uses_actual_goal_and_accounts_for_every_turn(
    verify_first, goal
):
    adapter = ScriptedResearchAdapter(
        (["Memory answer"] if verify_first else []) + [SEARCH, "Cited answer"]
    )
    router = ModelRouter(keys={"anthropic": "test-key"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter
    searches = []

    async def search(query):
        searches.append(query)
        return "Public record", ["https://example.test/source"] * 2

    async def fetch(url):
        pytest.fail("This run should only search")

    result, accounting = await run_research(
        {"company": "Acme"},
        goal=goal,
        model_id="anthropic/claude-haiku-4-5",
        router=router,
        recipe_version="1",
        search=search,
        fetch=fetch,
    )

    assert result == {
        "answer": "Cited answer",
        "sources": ["https://example.test/source"],
    }
    assert searches == ["official record"]
    assert goal in json.dumps(adapter.requests[0].messages)
    count = 3 if verify_first else 2
    assert len(accounting["model_calls"]) == len(adapter.requests) == count
    assert accounting["tokens_in"] == count * 10
    assert accounting["tokens_out"] == count * 5
    assert accounting["cost"] == pytest.approx(count * 0.001)


@pytest.mark.asyncio
async def test_domain_budget_failure_preserves_spent_model_calls():
    adapter = ScriptedResearchAdapter([SEARCH] * MAX_STEPS)
    router = ModelRouter(keys={"anthropic": "test-key"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter

    async def search(query):
        return "Keep searching", ["https://example.test/source"]

    async def fetch(url):
        pytest.fail("This run should only search")

    result, accounting = await run_research(
        {"company": "Acme"},
        goal="Find records",
        model_id="anthropic/claude-haiku-4-5",
        router=router,
        recipe_version="1",
        search=search,
        fetch=fetch,
    )
    assert result["answer"] is None
    assert result["error_code"] == "research_incomplete_step_budget"
    assert len(accounting["model_calls"]) == MAX_STEPS
    assert accounting["cost"] == pytest.approx(MAX_STEPS * 0.001)


def test_unknown_live_cost_is_not_hidden_by_cached_call():
    def response(*, cached, cost):
        return LLMResponse(
            content="answer",
            data=None,
            tokens_in=10,
            tokens_out=5,
            cost=cost,
            model="example",
            provider="anthropic",
            cached=cached,
        )

    accounting = model_call_accounting(
        "anthropic/example",
        [response(cached=True, cost=0.1), response(cached=False, cost=None)],
    )
    assert accounting["cost"] is None
    assert accounting["tokens_in"] == 20
    assert len(accounting["model_calls"]) == 2


@pytest.mark.asyncio
async def test_fetch_cancellation_is_not_downgraded_to_tool_observation(monkeypatch):
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr("frisket.ops.netguard.safe_request", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await fetch_page("https://example.test/source", object())
