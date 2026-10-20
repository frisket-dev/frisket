from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from frisket.ai.llm import ModelRouter
from frisket.ai.research.recorder import ResearchRecorder, redact_research_text
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


async def _run(turns, search, fetch, recorder, *, secret_values=()):
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
        secret_values=secret_values,
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


@pytest.mark.parametrize(
    "field,limit",
    [
        ("query", 500),
        ("url", 2000),
        ("title", 300),
        ("snippet", 1000),
        ("error", 500),
        ("error_code", 500),
        ("text", 8000),
    ],
)
def test_credentials_crossing_every_recording_bound_are_redacted_before_clipping(
    tmp_path, field, limit
):
    secret = "opaque-credential-abcdefghijklmno"
    value = "q" * (limit - 12) + secret + " ending"
    recorder = ResearchRecorder(secret_values=(secret,))
    token = recorder.start("search", query=value if field == "query" else "record")
    response = _response("record", count=1)
    if field in {"title", "snippet"}:
        hit = replace(
            response.results[0], **{"excerpt" if field == "snippet" else field: value}
        )
        response = replace(response, results=(hit,))
    recorder.finish(
        token,
        response=response,
        error_detail=value if field == "error" else None,
        error_code=value if field == "error_code" else None,
    )
    if field in {"url", "text"}:
        token = recorder.start(
            "fetch", url=value if field == "url" else "https://example.test/"
        )
        recorder.finish(token, text=value if field == "text" else "Page text")
    snapshot = recorder.snapshot()
    assert secret[:10] not in json.dumps(snapshot)
    assert "[REDACTED]" in json.dumps(snapshot)
    row = RowTrace(row_id=1, trace_id="redact-before-clip")
    row.research = snapshot
    writer = TraceWriter.open(tmp_path, 1)
    writer.write_row(row.record(secret_values=(secret,)))
    persisted = read_trace_row(tmp_path, 1, 1)["row"]
    assert secret[:10] not in json.dumps(persisted)


@pytest.mark.asyncio
async def test_fetch_credential_echo_is_redacted_before_model_and_capture_clipping():
    secret = "opaque-credential-abcdefghijklmno"
    page = "q" * 7988 + secret + " remainder"
    recorder = ResearchRecorder(secret_values=(secret,))

    async def search(query):
        pytest.fail("unused")

    async def fetch(url):
        return ResearchFetchObservation(page)

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
        fetch,
        recorder,
        secret_values=(secret,),
    )
    messages = str(adapter.requests[-1].messages)
    assert secret[:10] not in messages
    assert "q" * 7988 + "[REDACTED]" in messages
    step = recorder.snapshot()["steps"][0]
    assert secret[:10] not in step["text"]
    assert "[REDACTED]" in step["text"]
    assert step["text"] == ("q" * 7988 + "[REDACTED] remainder")[:8000]
    assert step["truncated"] is True
    assert step["status"] == "ok"
    assert len(step["text"]) <= 8000
    assert data["answer"] == "Answer"


@pytest.mark.asyncio
async def test_search_credential_echo_is_removed_from_model_observation():
    secret = "opaque-credential-abcdefghijklmno"
    recorder = ResearchRecorder(secret_values=(secret,))

    async def search(query):
        response = _response("record", count=1)
        hit = replace(response.results[0], title=secret, excerpt=f"Evidence {secret}")
        return search_observation(query, replace(response, results=(hit,)))

    async def fetch(url):
        pytest.fail("unused")

    _, _, adapter = await _run(
        [[{"name": "search", "args": {"query": "record"}, "id": "s1"}], "Answer"],
        search,
        fetch,
        recorder,
        secret_values=(secret,),
    )
    assert secret not in str(adapter.requests[-1].messages)
    assert "Evidence [REDACTED]" in str(adapter.requests[-1].messages)


def test_scrub_preserves_tokenless_ollama_and_short_token_substrings():
    text = "Run ollama locally; https://github.com/ollama/ollama; max 2x faster; contest test"
    assert redact_research_text(text, ("ollama", "x", "abc", "test")) == (
        "Run ollama locally; https://github.com/ollama/ollama; max 2x faster; contest [REDACTED]"
    )
    recorder = ResearchRecorder(secret_values=("ollama", "x", "abc"))
    token = recorder.start("fetch", url="https://github.com/ollama/ollama")
    recorder.finish(token, text=text)
    step = recorder.snapshot()["steps"][0]
    assert step["url"] == "https://github.com/ollama/ollama"
    assert step["text"] == text


@pytest.mark.asyncio
async def test_search_and_fetch_sources_strip_selected_search_key_before_publication():
    secret = "opaque-search-credential-abcdefgh"
    search_url = f"https://example.test/search?q={secret}"
    fetch_url = f"https://example.test/page?q={secret}"
    fetched_urls = []

    async def search(query):
        response = _response("record", count=1)
        hit = replace(response.results[0], url=search_url)
        return search_observation(query, replace(response, results=(hit,)))

    async def fetch(url):
        fetched_urls.append(url)
        return ResearchFetchObservation("Page text")

    data, _, adapter = await _run(
        [
            [{"name": "search", "args": {"query": "record"}, "id": "s1"}],
            [{"name": "fetch", "args": {"url": fetch_url}, "id": "f1"}],
            "Grounded answer",
        ],
        search,
        fetch,
        ResearchRecorder(secret_values=(secret,)),
        secret_values=(secret,),
    )
    assert fetched_urls == [fetch_url]
    assert data == {
        "answer": "Grounded answer",
        "sources": [
            "https://example.test/search?q=[REDACTED]",
            "https://example.test/page?q=[REDACTED]",
        ],
    }
    assert secret not in json.dumps(data)
    assert len(adapter.requests) == 3
