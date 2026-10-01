import asyncio
import threading
import traceback
from types import SimpleNamespace

import ddgs
import httpx
import pytest

from frisket.actions.types import RowError
from frisket.ai.research.search import SearchService
from frisket.engine.executor.action_inventory import ExecutorDeps
from frisket.engine.executor.actions import _executor_deps_with_defaults
from frisket.engine.executor.web_search_read import (
    AdmittedWebSearcher,
    search_provider_use,
)
from frisket.ops.base import OpContext, RecipeInvocationHalt


def _context(**extras):
    return OpContext(project=None, http=None, extras=extras)


@pytest.mark.parametrize("mode", ["preview", "network_off", "missing_writer"])
def test_search_refuses_unadmitted_calls_before_provider(monkeypatch, mode):
    def forbidden():
        pytest.fail("DDGS must not be constructed before admission")

    monkeypatch.setattr(ddgs, "DDGS", forbidden)
    ctx = _context(preview=mode == "preview")
    if mode != "preview":
        ctx.project = SimpleNamespace(
            effective_network_policy=lambda: "off" if mode == "network_off" else "on"
        )

    async def run():
        reader = AdmittedWebSearcher(ctx)
        await reader.search("private query", max_results=1)

    with pytest.raises((RecipeInvocationHalt, RuntimeError)):
        asyncio.run(run())


def test_search_retains_four_attempts_backoff_without_provider_error_text(monkeypatch):
    calls, delays = [], []
    canary = "private-query-provider-error-canary"

    class Failing:
        def __init__(self, *, timeout):
            pass

        def text(self, query, max_results):
            calls.append((query, max_results))
            raise RuntimeError(canary)

    async def sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(ddgs, "DDGS", Failing)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(RowError, match="bounded retries") as failure:
        asyncio.run(AdmittedWebSearcher(_context()).search(canary, max_results=2))
    assert calls == [(canary, 2)] * 4
    assert delays == [1.5, 3.0, 4.5, 6.0]
    assert canary not in str(failure.value)
    assert canary not in "".join(traceback.format_exception(failure.value))


def test_receipt_write_failure_does_not_retry_provider(monkeypatch):
    calls = []

    class Provider:
        def __init__(self, *, timeout):
            pass

        def text(self, query, max_results):
            calls.append(query)
            return []

    def failed_record(self, attempt, *, response, error, quote):
        raise RuntimeError("receipt write failed")

    monkeypatch.setattr(ddgs, "DDGS", Provider)
    monkeypatch.setattr(AdmittedWebSearcher, "_record", failed_record)
    with pytest.raises(RuntimeError, match="receipt write failed"):
        asyncio.run(AdmittedWebSearcher(_context()).search("query", max_results=1))
    assert calls == ["query"]


def test_cancelled_search_settles_and_records_the_returned_provider_call(monkeypatch):
    started, finish = threading.Event(), threading.Event()
    observed = []

    class Provider:
        def __init__(self, *, timeout):
            pass

        def text(self, query, max_results):
            started.set()
            assert finish.wait(5)
            return []

    monkeypatch.setattr(ddgs, "DDGS", Provider)
    monkeypatch.setattr(
        AdmittedWebSearcher,
        "_record",
        lambda self, attempt, *, response, error, quote: observed.append(
            (attempt, response is not None)
        ),
    )

    async def run():
        reader = AdmittedWebSearcher(_context())
        task = asyncio.create_task(reader.search("query", max_results=1))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await reader.aclose()
        assert not reader._calls

    asyncio.run(run())
    assert observed == [(1, True)]


def test_paid_provider_failure_is_visible_and_never_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"detail": "quota"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            service = SearchService(
                preference="tavily",
                effective_keys={"tavily": "secret"},
                http=http,
            )
            with pytest.raises(RowError, match="quota or billing limit") as failure:
                await AdmittedWebSearcher(_context(), search_service=service).search(
                    "query", max_results=1
                )
            assert failure.value.code == "search_quota"

    asyncio.run(run())
    assert calls == 1


def test_paid_provider_use_preserves_credit_and_captured_rate_identity() -> None:
    recorded = [
        SimpleNamespace(
            ref={
                "provider": "tavily",
                "service": "tavily.search",
                "max_attempts_per_row": 1,
                "cost_actual": 0.008,
                "units": {"credits": 1},
                "pricing_key": "tavily.search.credit",
                "pricing_unit": "credit",
                "unit_price_usd": "0.008",
            }
        )
    ]
    facts = SimpleNamespace(total_rows=1, completed_rows=1, failed_rows=0)
    assert search_provider_use(recorded, facts) == [
        {
            "provider": "tavily",
            "service": "tavily.search",
            "external_api": True,
            "selected_row_count": 1,
            "successful_row_count": 1,
            "failed_row_count": 0,
            "max_attempts_per_row": 1,
            "operation_call_count": 1,
            "cost_actual": 0.008,
            "units": {"credits": 1},
            "pricing_key": "tavily.search.credit",
            "pricing_unit": "credit",
            "unit_price_usd": "0.008",
        }
    ]


def test_closing_invocation_revokes_bound_searchers(monkeypatch):
    def forbidden():
        pytest.fail("closed handle must not call DDGS")

    monkeypatch.setattr(ddgs, "DDGS", forbidden)

    async def run():
        owner = AdmittedWebSearcher(_context())
        bound = owner.bind_row(None, sheet_id=1, row_id=1, sources=None, ctx=_context())
        await owner.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            await bound.search("query", max_results=1)

    asyncio.run(run())


def test_search_service_dependency_reaches_map_runner_context() -> None:
    service = object()

    def factory():
        return service

    browser = object()
    runner = SimpleNamespace(op_context_extras={"existing": object()})
    deps = _executor_deps_with_defaults(
        deps=ExecutorDeps(
            search_service_factory=factory,
            map_runner_factory=lambda _project, _router: runner,
        ),
        router=None,
        rss_fetcher=None,
        enclosure_fetcher=None,
        url_capture_fetcher=None,
        url_capture_browser=browser,
    )

    assert deps.search_service_factory is factory
    assert deps.map_runner_factory(object(), None) is runner
    assert runner.op_context_extras["search_service_factory"] is factory
    assert runner.op_context_extras["url_capture_browser"] is browser
    assert "existing" in runner.op_context_extras
