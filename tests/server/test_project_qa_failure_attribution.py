"""Failure diagnostics must not blame another tool that finished successfully."""

import asyncio

import pytest

from frisket.contracts.http.project_qa import AskThreadCreate, AskTurnRequest
from frisket.server.services.project_qa import ProjectQAService
from frisket.server.workspace import Workspace


@pytest.mark.parametrize(
    ("completed", "expected_tool"),
    [
        (["inspect_sheets"], "search_actions"),
        ([], None),
        (["inspect_sheets", "search_actions"], None),
    ],
)
def test_failure_attributes_only_one_unfinished_tool(
    tmp_path, completed, expected_tool
):
    async def scenario():
        workspace = Workspace(tmp_path / "ws")
        project_id = workspace.create("Tool diagnostics")["id"]

        async def runner(project, router, turn, store):
            for tool in ("inspect_sheets", "search_actions"):
                store.append_event(
                    turn["id"], kind="tool_started", payload={"tool": tool}
                )
            for tool in completed:
                store.append_event(
                    turn["id"], kind="tool_completed", payload={"tool": tool}
                )
            raise RuntimeError("Internal catalog failure")

        service = ProjectQAService(workspace, runner=runner)
        try:
            thread = await service.create(
                project_id, AskThreadCreate(scope={"kind": "project"})
            )
            await service.submit(
                project_id,
                thread["id"],
                AskTurnRequest(
                    request_id="parallel-tools",
                    question="Which action should I use?",
                    scope={"kind": "project"},
                    model="openai/test",
                ),
            )
            await asyncio.gather(*list(service._tasks.values()))
            detail = await service.detail(project_id, thread["id"])
            failure = detail["history"]["events"][-1]["payload"]
            assert failure["status"] == "failed"
            assert failure["diagnostic"]["code"] == "internal_error"
            assert failure["diagnostic"].get("tool") == expected_tool
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_internal_error_reporter_failure_does_not_prevent_terminal_turn(tmp_path):
    async def scenario():
        workspace = Workspace(tmp_path / "ws")
        project_id = workspace.create("Reporter failure")["id"]
        reports = []

        async def runner(project, router, turn, store):
            raise RuntimeError("sensitive prompt must not reach reporter")

        def report(*args):
            reports.append(args)
            raise RuntimeError("reporter unavailable")

        service = ProjectQAService(
            workspace, runner=runner, internal_error_reporter=report
        )
        try:
            thread = await service.create(
                project_id, AskThreadCreate(scope={"kind": "project"})
            )
            turn = await service.submit(
                project_id,
                thread["id"],
                AskTurnRequest(
                    request_id="failed-reporter",
                    question="Question",
                    scope={"kind": "project"},
                    model="openai/test",
                ),
            )
            await asyncio.gather(*list(service._tasks.values()))
            detail = await service.detail(project_id, thread["id"])
            failure = detail["history"]["events"][-1]["payload"]
            assert failure["status"] == "failed"
            assert failure["diagnostic"]["code"] == "internal_error"
            assert reports == [(project_id, thread["id"], turn["id"], "RuntimeError")]
        finally:
            await service.shutdown()

    asyncio.run(scenario())
