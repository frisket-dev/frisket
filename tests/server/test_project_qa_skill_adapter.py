from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.contracts.http.project_qa import AskThreadCreate, AskTurnRequest
from frisket.server.services.project_qa import ProjectQAService
from frisket.server.services.project_qa_skills import (
    ProjectQASkillAdapter,
    ProjectQASkillConflict,
)
from frisket.server.services.skills import SkillLibrary, SkillLibraryError
from frisket.server.workspace import Workspace


def _content(name: str, instruction: str = "Inspect representative sources.") -> str:
    return (
        f"---\nname: {name}\ndescription: Check representative sources.\n---\n\n"
        f"# Source checks\n\n{instruction}\n"
    )


def test_bound_authoring_rechecks_management_and_keeps_disabled_update_disabled(
    tmp_path,
) -> None:
    async def scenario() -> None:
        project = Project.create(tmp_path / "ask.frisket", name="Ask")
        library = SkillLibrary(tmp_path / "library")
        installed = library.create(_content("source-check"), enabled=False)
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Ask")
        turn = store.submit_turn(thread["id"], request_id="draft", question="Revise it")
        allowed = True
        checks = 0

        def authorize(_project_id, _context) -> None:
            nonlocal checks
            checks += 1
            if not allowed:
                raise PermissionError("Skill manager access is required.")

        adapter = ProjectQASkillAdapter(
            library=lambda: library, authorize=authorize, scope="workspace"
        )
        bound = adapter.bind(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=turn["id"],
            context=object(),
        )
        listed = await bound.list_skills()
        assert listed["skills"] == [
            {
                "id": installed["id"],
                "name": installed["name"],
                "description": installed["description"],
                "enabled": False,
                "revision": 1,
            }
        ]
        assert "content" not in listed["skills"][0]
        assert (await bound.read_skill(installed["id"]))["content"] == installed[
            "content"
        ]

        revised = _content("source-check", "Compare every cited source.")
        proposal = await bound.propose_skill(
            revised, target_skill_id=installed["id"], expected_revision=1
        )
        store.finish_turn(turn["id"], status="completed")
        result = await adapter.save(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=turn["id"],
            context=object(),
            proposal_id=proposal["proposal_id"],
            request_id="save-1",
            content=revised,
            expected_revision=1,
        )
        assert result["status"] == "saved"
        assert result["skill"]["revision"] == 2
        assert result["skill"]["enabled"] is False

        allowed = False
        with pytest.raises(PermissionError, match="manager"):
            await bound.read_skill(installed["id"])
        assert checks >= 6
        project.close()

    asyncio.run(scenario())


def test_solo_http_save_projects_current_authority_and_typed_conflict(tmp_path) -> None:
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    from frisket.server.app import create_app

    with TestClient(create_app(tmp_path / "workspace")) as client:
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        thread = client.post(
            f"/api/projects/{pid}/qa/threads",
            json={"scope": {"kind": "project"}},
        ).json()
        detail_path = f"/api/projects/{pid}/qa/threads/{thread['id']}"
        detail = client.get(detail_path)
        assert detail.status_code == 200, detail.text
        assert detail.json()["can_manage_skills"] is True
        assert detail.json()["skill_scope"] == "workspace"

        store = ProjectQAStore(client.app.state.workspace.get(pid))
        turn = store.submit_turn(
            thread["id"], request_id="draft", question="Create the skill"
        )
        content = _content("source-check")
        proposal_id = "proposal-http"
        store.append_skill_proposal(
            turn["id"],
            payload={
                "proposal_id": proposal_id,
                "target_skill_id": None,
                "base_revision": None,
                "name": "source-check",
                "description": "Check representative sources.",
                "content": content,
                "enabled": True,
                "scope": "workspace",
            },
        )
        store.finish_turn(turn["id"], status="completed")
        path = detail_path + f"/turns/{turn['id']}/skill-proposals/{proposal_id}/save"
        saved = client.post(
            path,
            json={
                "request_id": "save-http",
                "content": content,
                "expected_revision": None,
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["status"] == "saved"
        assert saved.json()["skill"]["name"] == "source-check"

        edited_content = _content(
            "source-check", "Use the edited instruction without leaving this dialog."
        )
        edited = client.post(
            path,
            json={
                "request_id": "save-edit",
                "content": edited_content,
                "expected_revision": 1,
            },
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["skill"]["id"] == saved.json()["skill"]["id"]
        assert edited.json()["skill"]["revision"] == 2
        installed = client.get("/api/skills").json()["skills"]
        assert len(installed) == 1
        assert installed[0]["content"] == edited_content

        collision = client.post(
            path,
            json={
                "request_id": "save-http",
                "content": _content(
                    "source-check", "Use a changed instruction on the same request."
                ),
                "expected_revision": None,
            },
        )
        assert collision.status_code == 409
        assert collision.json()["detail"]["code"] == "request_conflict"

        saved_events = [
            event
            for event in client.get(detail_path + "/events").json()["events"]
            if event["kind"] == "skill_saved"
        ]
        assert [event["payload"]["status"] for event in saved_events] == [
            "changed",
            "saved",
        ]
        assert [event["payload"]["current_revision"] for event in saved_events] == [
            2,
            2,
        ]
        assert all("content" not in event["payload"] for event in saved_events)

        skill_id = edited.json()["skill"]["id"]
        disabled = client.patch(
            f"/api/skills/{skill_id}/enabled",
            json={"expectedRevision": 2, "enabled": False},
        )
        assert disabled.status_code == 200, disabled.text
        changed_events = [
            event
            for event in client.get(detail_path + "/events").json()["events"]
            if event["kind"] == "skill_saved"
        ]
        assert all(event["payload"]["status"] == "changed" for event in changed_events)
        assert all(
            event["payload"]["current_revision"] == 3 for event in changed_events
        )

        deleted = client.request(
            "DELETE",
            f"/api/skills/{skill_id}",
            json={"expectedRevision": 3},
        )
        assert deleted.status_code == 204, deleted.text
        deleted_events = [
            event
            for event in client.get(detail_path + "/events").json()["events"]
            if event["kind"] == "skill_saved"
        ]
        assert all(event["payload"]["status"] == "deleted" for event in deleted_events)
        assert all(
            event["payload"]["current_revision"] is None for event in deleted_events
        )


def test_save_conflict_rebase_retry_and_explicit_selection(tmp_path) -> None:
    async def scenario() -> None:
        project = Project.create(tmp_path / "ask.frisket", name="Ask")
        library = SkillLibrary(tmp_path / "library")
        store = ProjectQAStore(project)
        research = {
            "write_mode": "ask_overwrite",
            "budget_usd": None,
            "max_turns": None,
            "skills": ["existing"],
        }
        thread = store.create_thread(title="Ask", research=research)
        turn = store.submit_turn(
            thread["id"], request_id="draft", question="Create it", research=research
        )
        adapter = ProjectQASkillAdapter(
            library=lambda: library,
            authorize=lambda _project_id, _context: None,
            scope="workspace",
        )
        bound = adapter.bind(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=turn["id"],
            context=object(),
        )
        content = _content("source-check")
        proposal = await bound.propose_skill(content)
        store.finish_turn(turn["id"], status="completed")

        saved = await adapter.save(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=turn["id"],
            context=object(),
            proposal_id=proposal["proposal_id"],
            request_id="save-create",
            content=content,
            expected_revision=None,
        )
        assert saved["skill"]["revision"] == 1
        assert store.get_thread(thread["id"])["research"]["skills"] == [
            "existing",
            "source-check",
        ]
        assert (
            await adapter.save(
                store=store,
                project_id="project-1",
                thread_id=thread["id"],
                turn_id=turn["id"],
                context=object(),
                proposal_id=proposal["proposal_id"],
                request_id="save-create",
                content=content,
                expected_revision=None,
            )
        )["skill"]["id"] == saved["skill"]["id"]

        update_turn = store.submit_turn(
            thread["id"],
            request_id="update",
            question="Update it",
            research=store.get_thread(thread["id"])["research"],
        )
        update = adapter.bind(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=update_turn["id"],
            context=object(),
        )
        revised = _content("source-check", "Read the newest source first.")
        update_proposal = await update.propose_skill(
            revised,
            target_skill_id=saved["skill"]["id"],
            expected_revision=1,
        )
        store.finish_turn(update_turn["id"], status="completed")
        library.set_enabled(saved["skill"]["id"], expected_revision=1, enabled=False)
        with pytest.raises(ProjectQASkillConflict) as stale:
            await adapter.save(
                store=store,
                project_id="project-1",
                thread_id=thread["id"],
                turn_id=update_turn["id"],
                context=object(),
                proposal_id=update_proposal["proposal_id"],
                request_id="save-stale",
                content=revised,
                expected_revision=1,
            )
        assert stale.value.detail()["current_revision"] == 2

        rebased = await adapter.save(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=update_turn["id"],
            context=object(),
            proposal_id=update_proposal["proposal_id"],
            request_id="save-rebased",
            content=revised,
            expected_revision=2,
        )
        assert rebased["skill"]["revision"] == 3
        assert rebased["skill"]["enabled"] is False

        renamed = revised.replace("name: source-check", "name: renamed")
        with pytest.raises(SkillLibraryError, match="keep the installed skill name"):
            await adapter.save(
                store=store,
                project_id="project-1",
                thread_id=thread["id"],
                turn_id=update_turn["id"],
                context=object(),
                proposal_id=update_proposal["proposal_id"],
                request_id="save-renamed",
                content=renamed,
                expected_revision=3,
            )
        project.close()

    asyncio.run(scenario())


def test_save_retry_reconciles_library_write_after_lost_result(tmp_path) -> None:
    async def scenario() -> None:
        project = Project.create(tmp_path / "ask.frisket", name="Ask")
        library = SkillLibrary(tmp_path / "library")
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Ask")
        turn = store.submit_turn(thread["id"], request_id="draft", question="Create it")
        adapter = ProjectQASkillAdapter(
            library=lambda: library,
            authorize=lambda _project_id, _context: None,
            scope="workspace",
        )
        bound = adapter.bind(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=turn["id"],
            context=object(),
        )
        content = _content("source-check")
        proposal = await bound.propose_skill(content)
        store.finish_turn(turn["id"], status="completed")
        store.record_skill_save_attempt(
            turn["id"],
            proposal_id=proposal["proposal_id"],
            request_id="lost-response",
            payload={
                "target_skill_id": None,
                "expected_revision": None,
                "content": content,
                "name": "source-check",
                "description": "Check representative sources.",
                "enabled": True,
            },
        )
        written = library.create(content, enabled=True)

        recovered = await adapter.save(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=turn["id"],
            context=object(),
            proposal_id=proposal["proposal_id"],
            request_id="lost-response",
            content=content,
            expected_revision=None,
        )
        assert recovered["status"] == "saved"
        assert recovered["skill"]["id"] == written["id"]
        assert len(library.list()["skills"]) == 1
        assert [
            event["kind"]
            for event in store.events(thread["id"])["events"]
            if event["kind"] in {"skill_save_attempt", "skill_saved"}
        ] == ["skill_save_attempt", "skill_saved"]
        project.close()

    asyncio.run(scenario())


def test_skill_snapshot_failure_terminalizes_durable_admission(tmp_path) -> None:
    async def scenario() -> None:
        workspace = Workspace(tmp_path / "workspace")
        pid = workspace.create("Ask")["id"]

        class BrokenLibrary:
            @staticmethod
            def enabled():
                raise SkillLibraryError("Stored skills cannot be read safely.")

        async def runner(*_args):
            raise AssertionError("runner must not start after snapshot failure")

        service = ProjectQAService(workspace, runner=runner)
        service.configure_research(
            authorize=lambda _project_id, _context: None,
            skills=lambda: BrokenLibrary(),
        )
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        with pytest.raises(SkillLibraryError, match="cannot be read"):
            await service.submit(
                pid,
                thread["id"],
                AskTurnRequest(
                    request_id="snapshot-fails",
                    question="Use a skill",
                    scope={"kind": "project"},
                    model="anthropic/test",
                ),
            )
        store = ProjectQAStore(workspace.get(pid))
        [turn] = [
            event
            for event in store.events(thread["id"])["events"]
            if event["kind"] == "question"
        ]
        saved = store.get_turn(turn["turn_id"])
        assert saved["status"] == "failed"
        assert store.get_active_turn(thread["id"]) is None
        await service.shutdown()

    asyncio.run(scenario())
