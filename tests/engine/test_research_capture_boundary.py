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
