from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from frisket.actions.types import RowError
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

    router = SimpleNamespace(set_research=attach, add_secret_values=secrets.extend)
    ctx = SimpleNamespace(extras={"router": router}, http=None)
    owner = SimpleNamespace(_model="test", accounting_by_row={})
    searcher = SimpleNamespace(secret_values=lambda: ("selected-search-secret",))
    bound = research_read._BoundResearcher(owner, object(), 1, ctx, searcher)

    async def interrupted_research(*args, recorder, **kwargs):
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
        assert [step["status"] for step in steps] == ["ok", "cancelled"]
        assert steps[0]["text"] == "Completed page text"
        assert all(step["status"] != "started" for step in steps)
