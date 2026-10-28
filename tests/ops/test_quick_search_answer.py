from __future__ import annotations

import asyncio
import json

import pytest

from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.ai.research.quick_search import run_quick_search
from frisket.ai.research.recorder import ResearchRecorder
from frisket.ai.research.search import SearchHit, SearchResponse, SearchUsage


class AnswerAdapter:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.requests = []

    async def complete(self, request, client):
        self.requests.append(request)
        answer = {"answer": next(self.answers)}
        return LLMResponse(
            content=json.dumps(answer),
            data=answer,
            tokens_in=20,
            tokens_out=10,
            cost=0.001,
            model=request.model,
        )


def response(*snippets):
    return SearchResponse(
        provider="ddgs",
        results=tuple(
            SearchHit(
                title=f"Record {i}",
                url=f"https://example.test/{i}",
                excerpt=snippet,
                retrieved_at="2026-10-28T00:00:00Z",
                published_at=None,
                provider="ddgs",
            )
            for i, snippet in enumerate(snippets)
        ),
        usage=SearchUsage("ddgs", "ddgs.text", 1, None, 0.0, "free"),
    )


async def run(answers, searched, *, secret_values=()):
    adapter = AnswerAdapter(answers)
    router = ModelRouter(keys={"anthropic": "test-key"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter
    queries = []
    recorder = ResearchRecorder(secret_values=secret_values)

    async def search(query):
        queries.append(query)
        return searched

    result = await run_quick_search(
        query="When was Acme founded?",
        model_id="anthropic/claude-haiku-4-5",
        router=router,
        search=search,
        recorder=recorder,
        secret_values=secret_values,
    )
    return result, adapter.requests, queries, recorder.snapshot()


@pytest.mark.asyncio
async def test_one_search_one_completion_keeps_snippets_and_search_trace():
    result, requests, queries, trace = await run(
        ["Acme was founded in 1998 (https://example.test/0)."],
        response("Acme was founded in 1998."),
    )
    assert result["answer"].startswith("Acme was founded in 1998")
    assert result["search_results"] == [
        {
            "title": "Record 0",
            "url": "https://example.test/0",
            "snippet": "Acme was founded in 1998.",
        }
    ]
    assert queries == ["When was Acme founded?"]
    assert len(requests) == 1
    assert requests[0].tools is None
    assert "Acme was founded in 1998." in json.dumps(requests[0].messages)
    assert len(trace["steps"]) == 1
    assert trace["steps"][0]["tool"] == "search"
    assert trace["steps"][0]["provider"] == "ddgs"


@pytest.mark.asyncio
async def test_no_results_returns_null_without_model_call():
    result, requests, queries, trace = await run([], response())
    assert result == {"answer": None, "search_results": []}
    assert requests == []
    assert len(queries) == len(trace["steps"]) == 1


@pytest.mark.asyncio
async def test_unsupported_answer_can_be_null_without_losing_results():
    result, requests, _, _ = await run([None], response("Acme sells widgets."))
    assert result["answer"] is None
    assert result["search_results"][0]["snippet"] == "Acme sells widgets."
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_schema_repair_does_not_repeat_the_search():
    result, requests, queries, _ = await run(
        [1998, "1998"], response("Acme was founded in 1998.")
    )
    assert result["answer"] == "1998"
    assert len(requests) == 2
    assert len(queries) == 1


@pytest.mark.asyncio
async def test_selected_credentials_are_redacted_before_prompt_and_publication():
    secret = "provider-secret-which-must-not-leak"
    result, requests, _, trace = await run(
        [None], response(f"An error echoed {secret}"), secret_values=(secret,)
    )
    assert secret not in json.dumps(result)
    assert secret not in json.dumps(requests[0].messages)
    assert secret not in json.dumps(trace)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [RuntimeError("search unavailable"), asyncio.CancelledError()]
)
async def test_search_failure_or_cancellation_is_recorded_without_model_call(error):
    recorder = ResearchRecorder()

    async def search(query):
        raise error

    with pytest.raises(type(error)):
        await run_quick_search(
            query="Acme",
            model_id="anthropic/claude-haiku-4-5",
            router=object(),
            search=search,
            recorder=recorder,
        )
    tool = recorder.snapshot()["steps"][0]
    assert tool["status"] == (
        "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
    )
