from __future__ import annotations

import asyncio

import pytest

from frisket.contracts.http.project_qa import AskThreadCreate, AskTurnRequest
from frisket.engine.store.project_qa import ProjectQAConflictError, ProjectQAStore
from frisket.server.services.project_qa import ProjectQAService
from frisket.server.workspace import Workspace


def test_replay_stop_reconnect_and_restart_do_not_repeat_paid_work(tmp_path):
    async def scenario():
        workspace = Workspace(tmp_path / "ws")
        pid = workspace.create("Ask")["id"]
        started = asyncio.Event()
        calls = []

        async def runner(project, router, turn, store):
            calls.append(turn["id"])
            store.append_event(
                turn["id"], kind="assistant", payload={"text": "Reading sources"}
            )
            started.set()
            await asyncio.Event().wait()

        service = ProjectQAService(workspace, runner=runner)
        thread = service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        request = AskTurnRequest(
            request_id="once", question="What changed?", scope={"kind": "project"}
        )
        first = service.submit(pid, thread["id"], request)
        await started.wait()
        assert service.submit(pid, thread["id"], request)["id"] == first["id"]
        assert service.detail(pid, thread["id"])["active_turn"]["id"] == first["id"]
        with pytest.raises(ProjectQAConflictError):
            service.delete(pid, thread["id"])
        service.stop(pid, thread["id"], first["id"])
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert service.detail(pid, thread["id"])["active_turn"] is None
        assert (
            ProjectQAStore(workspace.get(pid)).get_turn(first["id"])["status"]
            == "stopped"
        )
        assert calls == [first["id"]]
        service.submit(pid, thread["id"], request)
        await asyncio.sleep(0)
        assert calls == [first["id"]]
        await service.shutdown()

        store = ProjectQAStore(workspace.get(pid))
        abandoned = store.submit_turn(
            thread["id"], request_id="crashed", question="Again"
        )
        restarted = ProjectQAService(workspace, runner=runner)
        assert restarted.detail(pid, thread["id"])["active_turn"] is None
        assert store.get_turn(abandoned["id"])["status"] == "interrupted"
        assert calls == [first["id"]]
        await restarted.shutdown()

    asyncio.run(scenario())


def test_shutdown_is_interrupted_and_cross_thread_stop_is_refused(tmp_path):
    async def scenario():
        workspace = Workspace(tmp_path / "ws")
        pid = workspace.create("Ask")["id"]

        async def runner(*args):
            await asyncio.Event().wait()

        service = ProjectQAService(workspace, runner=runner)
        thread = service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        other = service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        turn = service.submit(
            pid,
            thread["id"],
            AskTurnRequest(
                request_id="one",
                question="Read",
                scope={"kind": "project"},
            ),
        )
        with pytest.raises(LookupError):
            service.stop(pid, other["id"], turn["id"])
        await service.shutdown()
        assert (
            ProjectQAStore(workspace.get(pid)).get_turn(turn["id"])["status"]
            == "interrupted"
        )

    asyncio.run(scenario())
