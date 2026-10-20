from __future__ import annotations

import asyncio

import pytest

from frisket.ai.llm import ModelRouter
from frisket.ai.research.recorder import ResearchRecorder
from frisket.ai.research.row_answer import (
    ResearchFetchObservation,
    fetch_page_result,
    run_research,
    search_observation,
)
from frisket.ai.research.search import (
    SearchHit,
    SearchResponse,
    SearchService,
    SearchUsage,
)
from frisket.contracts.http.run_provenance import RunTraceRow
from frisket.operability.trace import RowTrace, TraceWriter, read_trace_row
from test_research_row_answer import ScriptedResearchAdapter


def _response(query: str, *, count: int = 6) -> SearchResponse:
    return SearchResponse(
        provider="ddgs",
        results=tuple(
            SearchHit(
                title=f"{query} {index}",
                url=f"https://example.test/{query}/{index}",
                excerpt=f"Evidence {index}",
                retrieved_at="2026-10-20T00:00:00Z",
                published_at=None,
                provider="ddgs",
            )
            for index in range(count)
        ),
        usage=SearchUsage("ddgs", "ddgs.text", 1, None, 0.0, "free"),
    )


async def _run(turns, search, fetch, recorder):
    adapter = ScriptedResearchAdapter(turns)
    router = ModelRouter(keys={"anthropic": "test-key"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter
    data, accounting = await run_research(
        {"company": "Acme"},
        goal="Find records",
        model_id="anthropic/claude-haiku-4-5",
        router=router,
        recipe_version="1",
        search=search,
        fetch=fetch,
        recorder=recorder,
    )
    return data, accounting, adapter


@pytest.mark.asyncio
async def test_tools_keep_invocation_order_actual_identity_and_all_encountered_urls():
    recorder = ResearchRecorder()
    second_started = asyncio.Event()
    completed = []

    async def search(query):
        if query == "first":
            await second_started.wait()
        else:
            second_started.set()
        completed.append(query)
        return search_observation(query, _response(query))

    async def fetch(url):
        return ResearchFetchObservation("fetch failed: this is literal page text")

    data, _, _ = await _run(
        [
            [
                {"name": "search", "args": {"query": "first"}, "id": "s1"},
                {"name": "search", "args": {"query": "second"}, "id": "s2"},
                {
                    "name": "fetch",
                    "args": {"url": "https://example.test/page"},
                    "id": "f1",
                },
            ],
            "Cited answer",
        ],
        search,
        fetch,
        recorder,
    )
    assert completed == ["second", "first"]
    steps = recorder.snapshot()["steps"]
    assert [step["tool"] for step in steps] == ["search", "search", "fetch"]
    assert [step["query"] for step in steps[:2]] == ["first", "second"]
    assert all(step["status"] == "ok" for step in steps)
    assert all(step["duration_ms"] >= 0 for step in steps)
    assert all(type(step["duration_ms"]) is int for step in steps)
    assert steps[0]["provider"] == "ddgs"
    assert steps[0]["service"] == "ddgs.text"
    assert steps[0]["results"][5] == {
        "title": "first 5",
        "url": "https://example.test/first/5",
        "snippet": "Evidence 5",
    }
    assert steps[2]["text"] == "fetch failed: this is literal page text"
    assert len(data["sources"]) == 13
    assert "https://example.test/page" in data["sources"]
    assert len(set(data["sources"])) == 13


@pytest.mark.asyncio
async def test_failed_fetch_keeps_model_text_and_existing_grounding_policy(monkeypatch):
    async def refused(*args, **kwargs):
        raise ValueError("blocked target")

    monkeypatch.setattr("frisket.ops.netguard.safe_request", refused)
    recorder = ResearchRecorder()

    async def search(query):
        pytest.fail("Failed fetch still counts as a source under the existing policy")

    data, _, adapter = await _run(
        [
            [
                {
                    "name": "fetch",
                    "args": {"url": "https://example.test/page"},
                    "id": "f1",
                }
            ],
            "Answer",
        ],
        search,
        lambda url: fetch_page_result(url, object()),
        recorder,
    )
    step = recorder.snapshot()["steps"][0]
    assert step["status"] == "error"
    assert step["error_code"] == "fetch_failed"
    assert step["text"] == "fetch failed: blocked target"
    assert "fetch failed: blocked target" in str(adapter.requests[-1].messages)
    assert data == {"answer": "Answer", "sources": ["https://example.test/page"]}
    assert len(adapter.requests) == 2


@pytest.mark.asyncio
async def test_diagnostic_failures_do_not_change_execution(monkeypatch):
    recorder = ResearchRecorder()

    def broken(*args, **kwargs):
        raise RuntimeError("diagnostics unavailable")

    monkeypatch.setattr(recorder, "start", broken)
    monkeypatch.setattr(recorder, "finish", broken)

    async def search(query):
        return search_observation(query, _response(query))

    async def fetch(url):
        pytest.fail("unused")

    data, _, _ = await _run(
        [[{"name": "search", "args": {"query": "record"}, "id": "s1"}], "Answer"],
        search,
        fetch,
        recorder,
    )
    assert data["answer"] == "Answer"
    assert len(data["sources"]) == 6


@pytest.mark.asyncio
async def test_tool_cancellation_is_recorded_and_reraised():
    recorder = ResearchRecorder()

    async def search(query):
        raise asyncio.CancelledError

    async def fetch(url):
        pytest.fail("unused")

    with pytest.raises(asyncio.CancelledError):
        await _run(
            [[{"name": "search", "args": {"query": "cancelled"}, "id": "s1"}]],
            search,
            fetch,
            recorder,
        )
    assert recorder.snapshot()["steps"][0]["status"] == "cancelled"


def test_recorder_bounds_are_independent_of_tool_execution():
    recorder = ResearchRecorder()
    token = recorder.start("search", query="q" * 501)
    response = _response("x" * 2100, count=7)
    recorder.finish(token, response=response)
    for index in range(25):
        token = recorder.start("fetch", url=f"https://example.test/{index}")
        recorder.finish(token, text="x" * 8001)
    snapshot = recorder.snapshot()
    assert snapshot["truncated"] is True
    assert len(snapshot["steps"]) == 24
    step = snapshot["steps"][0]
    assert len(step["query"]) == 500
    assert len(step["results"]) == 6
    assert len(step["results"][0]["title"]) == 300
    assert len(step["results"][0]["url"]) == 2000
    assert len(snapshot["steps"][-1]["text"]) == 8000
    assert all(step["truncated"] for step in snapshot["steps"])


@pytest.mark.parametrize(
    "provider,expected", [("exa", ("selected-key",)), ("ddgs", ())]
)
def test_only_selected_search_credential_reaches_redaction(provider, expected):
    service = SearchService(
        preference=provider,
        effective_keys={
            "exa": "selected-key",
            "tavily": "unselected-key",
            "anthropic": "model-key",
        },
    )
    assert service.secret_values() == expected


def test_recorded_integer_timings_survive_disk_and_strict_wire_normalization(
    tmp_path, monkeypatch
):
    recorder = ResearchRecorder()
    times = iter([10.0, 10.0016, 20.0, 19.9])
    with monkeypatch.context() as clock_patch:
        clock_patch.setattr(
            "frisket.ai.research.recorder.time.monotonic", lambda: next(times)
        )
        search = recorder.start("search", query="record")
        recorder.finish(search, response=_response("record"))
        fetch = recorder.start("fetch", url="https://example.test/page")
        recorder.finish(fetch, text="Page text")
    row = RowTrace(row_id=1, trace_id="research-timing")
    row.research = recorder.snapshot()
    writer = TraceWriter.open(tmp_path, 1)
    writer.write_row(row.record())
    persisted = read_trace_row(tmp_path, 1, 1)["row"]
    validated = RunTraceRow.model_validate(persisted)
    assert validated.research_incomplete is False
    assert validated.research is not None
    assert validated.research.truncated is False
    assert [step.duration_ms for step in validated.research.steps] == [2, 0]
    assert all(step.status == "ok" for step in validated.research.steps)
