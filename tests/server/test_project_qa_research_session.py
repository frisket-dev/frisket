"""Public research admission hooks used by normal and compaction requests."""

import asyncio

import pytest

from frisket.ai.llm.types import LLMRequest, LLMResponse
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.store.project_qa_research import ProjectQAResearchStore
from frisket.server.services.project_qa_research import ResearchSession


@pytest.mark.anyio
async def test_model_budget_settles_once_and_revocation_blocks_next_request(
    tmp_path, monkeypatch
):
    from frisket.ai.llm.pricing import PRICES

    monkeypatch.setitem(PRICES, "gpt-4o", (2.5, 10.0))
    project = Project.create(tmp_path / "research.frisket", name="Research")
    try:
        qa = ProjectQAStore(project)
        thread = qa.create_thread(title="Research")
        turn = qa.submit_turn(thread["id"], request_id="one", question="Analyze")
        ledger = ProjectQAResearchStore(project)
        run = ledger.create(
            turn_id=turn["id"],
            actor="reporter",
            budget_micros=1_000_000,
            currency="USD",
            write_mode="ask_overwrite",
            max_turns=None,
        )
        allowed = True

        def authorize():
            if not allowed:
                raise PermissionError("Project access revoked")

        session = ResearchSession(ledger, run["id"], authorize=authorize)
        request = LLMRequest(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "Analyze"}],
            max_tokens=100,
        )
        await session.before_model(request)
        response = LLMResponse(
            content="Done",
            data=None,
            model=request.model,
            tokens_in=10,
            tokens_out=10,
            cost=0.0001,
        )
        await session.after_model(response)
        await session.after_model(response)
        assert ledger.budget_summary(run["id"])["settled_micros"] == 100
        assert ledger.budget_summary(run["id"])["reserved_micros"] == 0
        assert ledger.get(run["id"])["turn_count"] == 1
        allowed = False
        with pytest.raises(PermissionError):
            await session.before_model(request)
        assert ledger.get(run["id"])["turn_count"] == 1
    finally:
        project.close()


@pytest.mark.anyio
async def test_unpriced_model_pauses_without_consuming_budget_or_turn(tmp_path):
    project = Project.create(tmp_path / "research.frisket", name="Research")
    try:
        qa = ProjectQAStore(project)
        thread = qa.create_thread(title="Research")
        turn = qa.submit_turn(thread["id"], request_id="one", question="Analyze")
        ledger = ProjectQAResearchStore(project)
        run = ledger.create(
            turn_id=turn["id"],
            actor=None,
            budget_micros=1_000_000,
            currency="USD",
            write_mode="ask_each",
            max_turns=None,
        )
        session = ResearchSession(ledger, run["id"], authorize=lambda: None)
        published = asyncio.Event()
        original_update = ledger.update

        def update(*args, **kwargs):
            result = original_update(*args, **kwargs)
            loop.call_soon_threadsafe(published.set)
            return result

        ledger.update = update
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(
            session.before_model(LLMRequest(model="openai/unpriced-model", messages=[]))
        )
        await asyncio.wait_for(published.wait(), 5)
        assert ledger.get(run["id"])["pending_approval"]["kind"] == "unknown_cost"
        assert ledger.operations(run["id"]) == []
        assert ledger.get(run["id"])["turn_count"] == 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        project.close()
