"""Runtime proofs for research limits and cancellation-safe accounting."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.types import LLMResponse
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.store.project_qa_research import ProjectQAResearchStore
from frisket.server.services.project_qa_research import ResearchSession
from frisket.server.services.project_qa_runner import run_turn


MODEL_ID = "gemini/gemini-3.5-flash"


class ScriptedAdapter:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.calls = 0

    async def complete(self, request, client) -> LLMResponse:
        response = self.responses[self.calls]
        self.calls += 1
        return response


def _response(
    *,
    tool_calls: int = 0,
    tool_start: int = 0,
    text: str | None = None,
    cost: float = 0.000_001,
) -> LLMResponse:
    return LLMResponse(
        content=text,
        data=None,
        model=MODEL_ID,
        tokens_in=10,
        tokens_out=10,
        cost=cost,
        tool_calls=[
            {"name": "inspect_sheets", "args": {}, "id": f"inspect-{index}"}
            for index in range(tool_start, tool_start + tool_calls)
        ],
    )


def _runtime(
    tmp_path, *, name: str, max_turns: int | None, responses: list[LLMResponse]
) -> tuple[
    Project,
    ProjectQAStore,
    ProjectQAResearchStore,
    dict[str, Any],
    ResearchSession,
    ModelRouter,
    ScriptedAdapter,
]:
    project = Project.create(tmp_path / f"{name}.frisket", name=name)
    qa = ProjectQAStore(project)
    thread = qa.create_thread(title=name, scope={"kind": "project"})
    turn = qa.submit_turn(
        thread["id"],
        request_id=name,
        question="Inspect until the investigation is complete.",
        scope={"kind": "project"},
        model=MODEL_ID,
        research={"budget_usd": "10", "max_turns": max_turns},
    )
    ledger = ProjectQAResearchStore(project)
    research = ledger.create(
        turn_id=turn["id"],
        actor="researcher",
        budget_micros=10_000_000,
        currency="USD",
        write_mode="ask_overwrite",
        max_turns=max_turns,
    )
    session = ResearchSession(ledger, research["id"], authorize=lambda: None)
    adapter = ScriptedAdapter(responses)
    router = ModelRouter(
        keys={"gemini": "test-key"},
        key_sources={"gemini": "platform_key"},
        cache=None,
        cache_mode="off",
        max_retries=0,
        use_env_keys=False,
    )
    router._adapters["gemini"] = adapter
    return project, qa, ledger, research, session, router, adapter


@pytest.mark.anyio
async def test_research_crosses_ordinary_caps_and_finite_turn_limit_pauses(
    tmp_path, monkeypatch
) -> None:
    from frisket.ai.llm.pricing import PRICES

    monkeypatch.setitem(PRICES, "gemini-3.5-flash", (1.0, 1.0))
    responses = [_response(tool_calls=25)]
    responses.extend(
        _response(tool_calls=1, tool_start=index) for index in range(25, 33)
    )
    responses.append(_response(text="Investigation complete."))
    project, qa, ledger, research, session, router, adapter = _runtime(
        tmp_path,
        name="unlimited",
        max_turns=None,
        responses=responses,
    )
    try:
        turn = qa.get_turn(research["turn_id"])
        result = await run_turn(project, router, turn, qa, research=session)
        assert result["text"] == "Investigation complete."
        assert adapter.calls == 10
        assert ledger.get(research["id"])["turn_count"] == 10
        completed_tools = [
            event
            for event in qa.events(turn["thread_id"], limit=200)["events"]
            if event["kind"] == "tool_completed"
        ]
        assert len(completed_tools) == 33
    finally:
        await router.aclose()
        project.close()

    project, qa, ledger, research, session, router, adapter = _runtime(
        tmp_path,
        name="finite",
        max_turns=1,
        responses=[_response(tool_calls=1), _response(text="must not be sent")],
    )
    published = asyncio.Event()
    original_update = ledger.update
    loop = asyncio.get_running_loop()

    def observe_pause(*args, **kwargs):
        updated = original_update(*args, **kwargs)
        if updated["state"] == "paused":
            loop.call_soon_threadsafe(published.set)
        return updated

    ledger.update = observe_pause
    task = asyncio.create_task(
        run_turn(
            project, router, qa.get_turn(research["turn_id"]), qa, research=session
        )
    )
    try:
        await asyncio.wait_for(published.wait(), 5)
        current = ledger.get(research["id"])
        assert current["pending_approval"]["kind"] == "turn_limit"
        assert current["turn_count"] == 1
        assert adapter.calls == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await router.aclose()
        project.close()


@pytest.mark.anyio
async def test_cancellation_during_usage_recording_still_settles_actual_cost(
    tmp_path, monkeypatch
) -> None:
    from frisket.ai.llm.pricing import PRICES

    monkeypatch.setitem(PRICES, "gemini-3.5-flash", (1.0, 1.0))
    response = _response(text="Investigation complete.", cost=0.000_321)
    project, qa, ledger, research, session, router, _adapter = _runtime(
        tmp_path,
        name="cancel-settlement",
        max_turns=None,
        responses=[response],
    )
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_record_usage = qa.record_usage

    def blocked_record_usage(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise TimeoutError("test did not release usage persistence")
        return original_record_usage(*args, **kwargs)

    qa.record_usage = blocked_record_usage
    task = asyncio.create_task(
        run_turn(
            project, router, qa.get_turn(research["turn_id"]), qa, research=session
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        [operation] = ledger.operations(research["id"])
        assert operation["actual_micros"] == 321
        assert ledger.budget_summary(research["id"])["reserved_micros"] == 0
    finally:
        release.set()
        await router.aclose()
        project.close()


@pytest.mark.anyio
async def test_action_tool_exposes_and_validates_the_canonical_draft(tmp_path) -> None:
    """A misplaced setting is corrected before an execution service sees it."""
    draft = {
        "action_id": "media.to_markdown",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {"source": "PDF", "engine": "markitdown"},
        "output_names": {"markdown": "Text"},
    }
    wrong = {**draft, "engine": "markitdown"}
    replies = []
    for index, args in enumerate((wrong, draft)):
        reply = _response()
        reply.tool_calls = [
            {
                "name": "prepare_action",
                "args": {"title": "Convert PDFs", "draft": args},
                "id": f"prepare-{index}",
            }
        ]
        replies.append(reply)
    replies.append(_response(text="Action prepared."))
    project, qa, ledger, research, session, router, adapter = _runtime(
        tmp_path, name="typed-action", max_turns=None, responses=replies
    )
    prepared = []

    class Execution:
        async def prepare_action(self, title, value):
            prepared.append(value)
            return {
                "proposal": {"title": title, "spec": value},
                "event_ref": {"event_seq": 5, "dispatch_id": "prepared-1"},
            }

    try:
        turn = qa.get_turn(research["turn_id"])
        result = await run_turn(
            project, router, turn, qa, research=session, execution=Execution()
        )
        assert result["text"] == "Action prepared."
        assert prepared == [draft]
        assert adapter.calls == 3
    finally:
        project.close()
