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
from frisket.server.services.project_qa_research import ResearchSkillsUnavailable


@pytest.mark.anyio
async def test_research_does_not_treat_hosted_billing_as_provider_cost(
    tmp_path, monkeypatch
):
    from frisket.execution import pricing_policy

    workspace = Workspace(tmp_path / "workspace")
    pid = workspace.create("Research")["id"]
    service = ProjectQAService(workspace)
    service.configure_research(authorize=lambda pid, context: None)
    monkeypatch.setattr(pricing_policy, "default_pricing_policy", lambda: object())
    try:
        options = await service.research_options(pid)
        assert options["available"] is False
        assert "billing" in options["reason"]
        with pytest.raises(PermissionError, match="billing"):
            service._research_setup(pid, workspace.get(pid), {"budget_usd": "1"}, None)
    finally:
        await service.shutdown()


@pytest.mark.anyio
async def test_runtime_admission_failure_interrupts_research(tmp_path):
    class FailingPort:
        async def prepare_turn(self, **kwargs):
            raise ValueError("Runtime unavailable")

    workspace = Workspace(tmp_path / "workspace", project_qa_runtime_port=FailingPort())
    pid = workspace.create("Research")["id"]
    service = ProjectQAService(workspace)
    service.configure_research(authorize=lambda pid, context: None)
    thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
    try:
        with pytest.raises(ValueError, match="Runtime unavailable"):
            await service.submit(
                pid,
                thread["id"],
                AskTurnRequest(
                    scope={"kind": "project"},
                    request_id="failed-runtime",
                    question="Investigate",
                    research={"budget_usd": "1"},
                ),
            )
        detail = await service.detail(pid, thread["id"])
        question = next(
            event
            for event in detail["history"]["events"]
            if event["kind"] == "question"
        )
        project = workspace.get(pid)
        turn = ProjectQAStore(project).get_turn(question["turn_id"])
        assert turn["status"] == "failed"
        assert (
            ProjectQAResearchStore(project).get_for_turn(turn["id"])["state"]
            == "interrupted"
        )
    finally:
        await service.shutdown()


@pytest.mark.anyio
async def test_replaying_interrupted_parent_does_not_create_live_research(tmp_path):
    from frisket.engine.store.project_qa_research import ProjectQAResearchNotFound

    workspace = Workspace(tmp_path / "workspace")
    pid = workspace.create("Research")["id"]
    service = ProjectQAService(workspace)
    service.configure_research(authorize=lambda pid, context: None)
    thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
    body = AskTurnRequest(
        scope={"kind": "project"},
        request_id="interrupted",
        question="Investigate",
        research={"budget_usd": "1"},
    )
    store = await service.store(pid)
    values = body.model_dump(mode="json")
    values["scope"] = body.scope.model_dump(exclude_none=True)
    parent = store.submit_turn(thread["id"], **values)
    store.finalize_turn(parent["id"], status="interrupted")
    try:
        replay = await service.submit(pid, thread["id"], body)
        assert replay["status"] == "interrupted"
        with pytest.raises(ProjectQAResearchNotFound):
            ProjectQAResearchStore(workspace.get(pid)).get_for_turn(parent["id"])
    finally:
        await service.shutdown()


@pytest.mark.anyio
async def test_research_options_expose_manifest_only_and_allow_no_skills(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    pid = workspace.create("Research")["id"]
    library = SkillLibrary(tmp_path / "library")
    skill = library.create(
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
        snapshots = service._research_setup(
            pid, project, {**base, "skills": None}, None
        )["skills"]
        library.set_enabled(
            skill["id"], expected_revision=skill["revision"], enabled=False
        )
        with pytest.raises(ResearchSkillsUnavailable):
            service._authorize_run(pid, None, snapshots)
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
