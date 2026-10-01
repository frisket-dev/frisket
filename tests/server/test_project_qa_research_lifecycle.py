"""A research pause belongs to the live server, not the browser connection."""

import asyncio

import pytest

from frisket.contracts.http.project_qa import (
    AskResearchResume,
    AskThreadCreate,
    AskTurnRequest,
)
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.store.project_qa_research import ProjectQAResearchStore
from frisket.server.services.project_qa import ProjectQAService
from frisket.server.workspace import Workspace
from frisket.server.services.skills import SkillLibrary


@pytest.mark.anyio
async def test_research_options_expose_manifest_only_and_allow_no_skills(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    pid = workspace.create("Research")["id"]
    library = SkillLibrary(tmp_path / "library")
    library.create(
        "---\nname: analyze-records\ndescription: Analyze project records\n---\nPrivate instructions here."
    )
    service = ProjectQAService(workspace)
    service.configure_research(
        authorize=lambda pid, context: None, skills=lambda: library
    )
    try:
        options = await service.research_options(pid)
        assert options["available"] is True
        assert options["skills"] == [
            {"name": "analyze-records", "description": "Analyze project records"}
        ]
        assert "Private instructions" not in str(options)
        base = {"budget_usd": "1", "write_mode": "ask_overwrite", "max_turns": None}
        project = workspace.get(pid)
        assert (
            service._research_setup(pid, project, {**base, "skills": []}, None)[
                "skills"
            ]
            == []
        )
        assert (
            len(
                service._research_setup(pid, project, {**base, "skills": None}, None)[
                    "skills"
                ]
            )
            == 1
        )
    finally:
        await service.shutdown()


@pytest.mark.anyio
async def test_exact_approval_survives_client_return_and_stop_interrupts_waiter(
    tmp_path,
):
    workspace = Workspace(tmp_path / "workspace")
    pid = workspace.create("Research")["id"]
    published = asyncio.Event()
    answers = []

    async def runner(project, router, turn, store):
        session = service._research_sessions[(project, turn["id"])]
        original = session.store.update
        loop = asyncio.get_running_loop()

        def publish(*args, **kwargs):
            result = original(*args, **kwargs)
            if result["state"] == "paused":
                loop.call_soon_threadsafe(published.set)
            return result

        session.store.update = publish
        answers.append(
            await session.pause({"kind": "action", "title": "Extract fields"})
        )
        return {"text": "Done"}

    service = ProjectQAService(workspace, runner=runner)
    service.configure_research(authorize=lambda pid, context: None)
    thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
    request = AskTurnRequest(
        scope={"kind": "project"},
        request_id="research",
        question="Investigate",
        research={"budget_usd": "1.00"},
    )
    try:
        turn = await service.submit(pid, thread["id"], request, actor="reporter")
        await asyncio.wait_for(published.wait(), 5)
        detail = await service.detail(pid, thread["id"])
        state = detail["active_turn"]["research_state"]
        assert state["state"] == "paused"
        assert answers == []
        approval = dict(
            expected_revision=state["revision"],
            approval_id=state["pending_approval"]["id"],
        )
        with pytest.raises(PermissionError):
            await service.resume_research(
                pid,
                thread["id"],
                turn["id"],
                AskResearchResume(**approval, decision="approve"),
                actor="another-reporter",
                request_context=None,
            )
        with pytest.raises(ValueError, match="Approve or skip"):
            await service.resume_research(
                pid,
                thread["id"],
                turn["id"],
                AskResearchResume(**approval, budget_usd="2.00"),
                actor="reporter",
                request_context=None,
            )
        await service.resume_research(
            pid,
            thread["id"],
            turn["id"],
            AskResearchResume(**approval, decision="approve"),
            actor="reporter",
            request_context=None,
        )
        await asyncio.gather(*list(service._tasks.values()))
        assert answers == ["approve"]
        assert (
            ProjectQAStore(workspace.get(pid)).get_turn(turn["id"])["status"]
            == "completed"
        )

        published.clear()
        second = await service.submit(
            pid,
            thread["id"],
            request.model_copy(update={"request_id": "second"}),
            actor="reporter",
        )
        await asyncio.wait_for(published.wait(), 5)
        await service.stop(pid, thread["id"], second["id"])
        await asyncio.gather(*list(service._tasks.values()))
        assert answers == ["approve"]
        assert (
            ProjectQAResearchStore(workspace.get(pid)).get_for_turn(second["id"])[
                "state"
            ]
            == "interrupted"
        )
    finally:
        await service.shutdown()
