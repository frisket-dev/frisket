import asyncio
import threading
import traceback
from types import SimpleNamespace

import ddgs
import pytest

from frisket.actions.types import RowError
from frisket.engine.executor.web_search_read import AdmittedWebSearcher
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
        def text(self, query, max_results):
            calls.append(query)
            return []

    def failed_record(self, attempt, succeeded):
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
        def text(self, query, max_results):
            started.set()
            assert finish.wait(5)
            return []

    monkeypatch.setattr(ddgs, "DDGS", Provider)
    monkeypatch.setattr(
        AdmittedWebSearcher,
        "_record",
        lambda self, attempt, succeeded: observed.append((attempt, succeeded)),
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
