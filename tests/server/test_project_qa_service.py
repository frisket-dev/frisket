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
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        request = AskTurnRequest(
            request_id="once", question="What changed?", scope={"kind": "project"}
        )
        first = await service.submit(pid, thread["id"], request)
        await started.wait()
        assert (await service.submit(pid, thread["id"], request))["id"] == first["id"]
        assert (await service.detail(pid, thread["id"]))["active_turn"]["id"] == first[
            "id"
        ]
        with pytest.raises(ProjectQAConflictError):
            await service.delete(pid, thread["id"])
        await service.stop(pid, thread["id"], first["id"])
        await asyncio.gather(*list(service._tasks.values()))
        assert (await service.detail(pid, thread["id"]))["active_turn"] is None
        assert (
            ProjectQAStore(workspace.get(pid)).get_turn(first["id"])["status"]
            == "stopped"
        )
        assert calls == [first["id"]]
        await service.submit(pid, thread["id"], request)
        await asyncio.sleep(0)
        assert calls == [first["id"]]
        await service.shutdown()

        store = ProjectQAStore(workspace.get(pid))
        abandoned = store.submit_turn(
            thread["id"], request_id="crashed", question="Again"
        )
        restarted = ProjectQAService(workspace, runner=runner)
        assert (await restarted.detail(pid, thread["id"]))["active_turn"] is None
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
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        other = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        turn = await service.submit(
            pid,
            thread["id"],
            AskTurnRequest(
                request_id="one",
                question="Read",
                scope={"kind": "project"},
            ),
        )
        with pytest.raises(LookupError):
            await service.stop(pid, other["id"], turn["id"])
        await service.shutdown()
        assert (
            ProjectQAStore(workspace.get(pid)).get_turn(turn["id"])["status"]
            == "interrupted"
        )

    asyncio.run(scenario())


def test_storage_runs_off_event_loop_and_immediate_stop_drains(tmp_path, monkeypatch):
    import threading

    async def scenario():
        loop_thread = threading.get_ident()
        original = ProjectQAStore.create_thread

        def checked_create(self, *args, **kwargs):
            assert threading.get_ident() != loop_thread
            return original(self, *args, **kwargs)

        monkeypatch.setattr(ProjectQAStore, "create_thread", checked_create)
        workspace = Workspace(tmp_path / "ws")
        pid = workspace.create("Ask")["id"]
        started = asyncio.Event()

        async def runner(*args):
            started.set()
            await asyncio.Event().wait()

        service = ProjectQAService(workspace, runner=runner)
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        turn = await service.submit(
            pid,
            thread["id"],
            AskTurnRequest(
                request_id="one", question="Read", scope={"kind": "project"}
            ),
        )
        await service.stop(pid, thread["id"], turn["id"])
        await asyncio.gather(*list(service._tasks.values()))
        detail = await service.detail(pid, thread["id"])
        assert detail["active_turn"] is None
        assert detail["history"]["events"][-1]["payload"]["status"] == "stopped"
        await service.shutdown()

    asyncio.run(scenario())


def test_cancelled_submit_still_owns_admitted_turn_and_runtime(tmp_path):
    from contextlib import nullcontext

    async def scenario():
        workspace = Workspace(tmp_path / "ws")
        pid = workspace.create("Ask")["id"]
        prepared = asyncio.Event()
        release_prepare = asyncio.Event()
        closed = asyncio.Event()
        calls = []

        class Runtime:
            def call_scope(self, router):
                return nullcontext()

            def settle_call(self, **kwargs):
                raise AssertionError("custom runner has no model calls")

            async def aclose(self):
                closed.set()

        class Port:
            async def prepare_turn(self, **kwargs):
                calls.append(kwargs["turn_id"])
                prepared.set()
                await release_prepare.wait()
                return Runtime()

        workspace.project_qa_runtime_port = Port()

        async def runner(*args):
            await asyncio.Event().wait()

        service = ProjectQAService(workspace, runner=runner)
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        request = AskTurnRequest(
            request_id="once", question="Read", scope={"kind": "project"}
        )
        submit = asyncio.create_task(service.submit(pid, thread["id"], request))
        try:
            await asyncio.wait_for(prepared.wait(), 2)
            submit.cancel()
            release_prepare.set()
            with pytest.raises(asyncio.CancelledError):
                await submit
            replay = await service.submit(pid, thread["id"], request)
            assert calls == [replay["id"]]
            await service.shutdown()
            assert closed.is_set()
            assert (
                ProjectQAStore(workspace.get(pid)).get_turn(replay["id"])["status"]
                == "interrupted"
            )
        finally:
            release_prepare.set()
            if not submit.done():
                submit.cancel()
            await asyncio.gather(submit, return_exceptions=True)
            await service.shutdown()

    asyncio.run(scenario())
