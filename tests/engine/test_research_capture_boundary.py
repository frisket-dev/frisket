from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from frisket.actions.types import RowError
from frisket.ai.llm import LLMResponse
from frisket.ai.research.row_answer import ResearchFetchObservation
from frisket.ai.research.search import SearchHit, SearchResponse, SearchUsage
from frisket.cloud_execution import CloudExecutionRefused
from frisket.engine.executor import research_read
from frisket.ops.base import RecipeInvocationHalt


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "row", "hosted", "halt", "ordinary"])
@pytest.mark.parametrize("trace_broken", [False, True])
async def test_capability_finally_attaches_tools_and_preserves_failure_identity(
    monkeypatch, failure, trace_broken
):
    errors = {
        "cancel": asyncio.CancelledError(),
        "row": RowError("search_quota", "Search quota reached"),
        "hosted": CloudExecutionRefused("cloud_refused", "Cloud refused"),
        "halt": RecipeInvocationHalt("network_disabled", "Network disabled"),
        "ordinary": RuntimeError("Ordinary failure"),
    }
    primary = errors[failure]
    snapshots, secrets = [], []

    def attach(snapshot):
        if trace_broken:
            raise RuntimeError("Diagnostics unavailable")
        snapshots.append(snapshot)

    model_secret = "selected-model-secret-abcdefghijk"
    router = SimpleNamespace(
        set_research=attach,
        add_secret_values=secrets.extend,
        secret_values_for_model=lambda model: (model_secret,),
    )
    ctx = SimpleNamespace(extras={"router": router}, http=None)
    owner = SimpleNamespace(_model="test", accounting_by_row={})
    searcher = SimpleNamespace(secret_values=lambda: ("selected-search-secret",))
    bound = research_read._BoundResearcher(owner, object(), 1, ctx, searcher)

    async def interrupted_research(*args, recorder, **kwargs):
        assert kwargs["secret_values"] == ("selected-search-secret",)
        search = recorder.start("search", query="q" * 490 + model_secret)
        recorder.finish(
            search, error=RuntimeError("e" * 490 + "selected-search-secret")
        )
        completed = recorder.start("fetch", url="https://example.test/done")
        recorder.finish(completed, text="Completed page text")
        recorder.start("fetch", url="https://example.test/interrupted")
        raise primary

    monkeypatch.setattr(research_read, "run_research", interrupted_research)
    with pytest.raises(BaseException) as caught:
        await bound._answer("Research", {})
    if failure == "ordinary":
        assert isinstance(caught.value, RowError)
        assert caught.value.code == "research_failed"
        assert caught.value.__cause__ is primary
    else:
        assert caught.value is primary
    assert secrets == ["selected-search-secret"]
    if not trace_broken:
        steps = snapshots[0]["steps"]
        assert [step["status"] for step in steps] == ["error", "ok", "cancelled"]
        assert "selected-m" not in steps[0]["query"]
        assert "[REDACTED]" in steps[0]["query"]
        assert "selected-s" not in steps[0]["error"]
        assert "[REDACTED]" in steps[0]["error"]
        assert steps[1]["text"] == "Completed page text"
        assert all(step["status"] != "started" for step in steps)


@pytest.mark.asyncio
async def test_tokenless_ollama_research_preserves_ordinary_tool_text_and_source_urls(
    monkeypatch,
):
    url = "https://github.com/ollama/ollama"
    text = f"Run ollama locally; see ollama.com and {url}; max 2x faster."
    requests, snapshots = [], []
    turns = iter(
        [
            [{"name": "search", "args": {"query": "ollama"}, "id": "s1"}],
            [{"name": "fetch", "args": {"url": url}, "id": "f1"}],
            "Ollama answer",
        ]
    )

    async def complete_transport(request, **kwargs):
        requests.append(request)
        turn = next(turns)
        return LLMResponse(
            content=turn if isinstance(turn, str) else None,
            tool_calls=turn if isinstance(turn, list) else None,
            data=None,
            tokens_in=1,
            tokens_out=1,
            cost=0,
            model=request.model,
        )

    async def search_response(query, *, max_results):
        return SearchResponse(
            provider="ddgs",
            results=(
                SearchHit("Ollama", url, text, "2026-10-20T00:00:00Z", None, "ddgs"),
            ),
            usage=SearchUsage("ddgs", "ddgs.text", 1, None, 0, "free"),
        )

    async def fetch_page_result(url, http):
        return ResearchFetchObservation(text)

    router = SimpleNamespace(
        complete_transport=complete_transport,
        secret_values_for_model=lambda model: ("ollama", "x"),
        add_secret_values=lambda values: None,
        set_research=snapshots.append,
    )
    owner = SimpleNamespace(
        _model="ollama/@local/llama3.2", accounting_by_row={}, calls_by_row={}
    )
    searcher = SimpleNamespace(
        secret_values=lambda: (), search_response=search_response
    )
    ctx = SimpleNamespace(extras={"router": router}, http=None)
    bound = research_read._BoundResearcher(owner, object(), 1, ctx, searcher)
    monkeypatch.setattr(
        research_read, "AccountedAgentRouter", lambda inner, *args: inner
    )
    monkeypatch.setattr(research_read, "fetch_page_result", fetch_page_result)
    result = await bound._answer("Find Ollama", {})
    assert result.answer == "Ollama answer"
    assert result.sources == [url]
    assert result.unverified_memory is False
    assert text in str(requests[1].messages)
    assert text in str(requests[2].messages)
    assert snapshots[0]["steps"][1]["text"] == text
    assert snapshots[0]["steps"][1]["url"] == url
