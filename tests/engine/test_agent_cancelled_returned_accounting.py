"""Capability-retained facts must not be reminted by outer row cancellation."""

import json
import asyncio

import ddgs
import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.contracts.action import ActionResult
from frisket.engine.store.runs import RunResultStore
from frisket.server.app import create_app
from http_test_helpers import drain_queue
from test_http_action_run_queue_boundary import (
    _post_after_exact_confirmation,
    _seed_web_search_project,
    _web_search_action,
)
from test_research_web_search_executor import _FakeDDGS


@pytest.mark.parametrize("kind", ["research", "mcp"])
@pytest.mark.parametrize("interrupt", ["stop", "halt", "postprocess", "owner_cancel"])
def test_queued_agent_interruption_during_schema_repair_keeps_each_paid_fact_once(
    tmp_path, monkeypatch, kind, interrupt
):
    monkeypatch.setattr(ddgs, "DDGS", _FakeDDGS)
    router = ModelRouter(keys={"anthropic": "test-key"}, cache=None, max_retries=0)
    calls = []
    project = None

    class Adapter:
        async def complete(self, request, client):
            calls.append(request)
            from frisket.ops.base import RecipeInvocationHalt

            expected_paid = 1 if kind == "research" else 2
            if len(calls) > expected_paid:
                raise RecipeInvocationHalt(
                    "network_disabled", "Network disabled during repair"
                )
            if interrupt == "stop" and len(calls) == expected_paid:
                run_id = project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0]
                assert RunResultStore(project).request_cancel(run_id)
            value = "Paid answer" if interrupt == "postprocess" else 123
            data = {"answer": value} if kind == "research" else {"checked": value}
            if interrupt == "owner_cancel" and len(calls) == expected_paid:
                owners = [
                    task
                    for task in asyncio.all_tasks()
                    if task.get_coro().__qualname__ == "MapRunner.run"
                ]
                assert len(owners) == 1
                owners[0].cancel()
            return LLMResponse(
                content=json.dumps(data),
                data=data,
                model=request.model,
                tokens_in=10,
                tokens_out=5,
                cost=0.001,
            )

    router._adapters["anthropic"] = Adapter()
    if interrupt == "postprocess":
        from frisket.engine.executor import research_read
        from frisket.ops.mcp_extract import McpExtractor

        target = research_read if kind == "research" else McpExtractor
        method = "run_quick_search" if kind == "research" else "extract"
        original = getattr(target, method)

        async def fail_after_return(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("Postprocessing failed after a paid response")

        monkeypatch.setattr(target, method, fail_after_return)
    with TestClient(
        create_app(tmp_path / "ws", router=router, run_status_grace_seconds=3600)
    ) as client:
        project_id, sheet_id = _seed_web_search_project(client)
        project = client.app.state.workspace.get(project_id)
        row_id = project.db.execute(
            "SELECT MIN(id) FROM rows WHERE sheet_id=?", (sheet_id,)
        ).fetchone()[0]
        action = _web_search_action(sheet_id, idempotency_key="stop-repair")
        if kind == "mcp":
            import sys
            from frisket.project_settings import write_project_mcp_servers
            from test_mcp_extract_runtime_bridge import FIXTURE_SERVER, _spec

            write_project_mcp_servers(
                project,
                [
                    {
                        "id": "fixture",
                        "name": "fixture",
                        "enabled": True,
                        # subprocess-boundary: real local MCP session opens and drains.
                        "command": sys.executable,
                        "args": [
                            str(FIXTURE_SERVER),
                            "--events",
                            str(tmp_path / "mcp.jsonl"),
                        ],
                        "cwd": str(tmp_path),
                        "env": {},
                        "revision": 1,
                        "lastTest": None,
                    }
                ],
            )
            action = _spec(sheet_id, ["fixture"], row_id)
            action["params"]["source"] = ["country"]
        action["scope"]["row_ids"] = [row_id]
        _, response = _post_after_exact_confirmation(
            client, f"/api/projects/{project_id}/actions/v1/run", action=action
        )
        queued = ActionResult.model_validate(response.json())
        assert queued.status == "queued", response.text
        if interrupt == "owner_cancel":
            with pytest.raises(asyncio.CancelledError):
                drain_queue(client)
        else:
            drain_queue(client)
        receipt = project.db.execute(
            "SELECT status,body FROM receipts WHERE id=?", (queued.receipt_id,)
        ).fetchone()
        if interrupt == "owner_cancel":
            assert receipt["status"] not in {"completed", "partial"}
        else:
            assert receipt["status"] == (
                "cancelled" if interrupt == "stop" else "failed"
            ), json.loads(receipt["body"])
        facts = RunResultStore(project).model_calls(queued.run_id)
        paid = [fact for fact in facts if fact["capability"] == "llm.complete"]
        expected_paid = 1 if kind == "research" else 2
        assert len(calls) == expected_paid + (interrupt == "halt")
        assert len(paid) == expected_paid, [
            (fact["id"], fact["provider_cost_usd"]) for fact in paid
        ]
        assert sum(fact["provider_cost_usd"] for fact in paid) == pytest.approx(
            0.001 * expected_paid
        )
        assert len({fact["id"] for fact in paid}) == expected_paid
        assert project.db.execute(
            "SELECT cost_actual FROM runs WHERE id=?", (queued.run_id,)
        ).fetchone()[0] == pytest.approx(0.001 * expected_paid)
        if interrupt != "postprocess":
            assert (
                project.db.execute(
                    "SELECT count(*) FROM cell_result_heads WHERE run_id=?",
                    (queued.run_id,),
                ).fetchone()[0]
                == 0
            )
        else:
            retained_tokens = project.db.execute(
                "SELECT sum(tokens_in) FROM results WHERE run_id=?", (queued.run_id,)
            ).fetchone()[0]
            assert retained_tokens == expected_paid * 10
            assert all(fact["column_id"] is not None for fact in paid)


@pytest.mark.parametrize("stop", [False, "invalid", "valid"])
def test_queued_vlm_failure_or_stop_records_each_purchase_once(
    tmp_path, monkeypatch, stop
):
    from test_ocr import _render_png, _spec

    router = ModelRouter(keys={"anthropic": "test-key"}, cache=None, max_retries=0)
    calls = []
    project = None

    class Adapter:
        async def complete(self, request, client):
            calls.append(request)
            if stop:
                run_id = project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0]
                assert RunResultStore(project).request_cancel(run_id)
            data = {"text": "HELLO" if stop == "valid" else 123}
            return LLMResponse(
                content=json.dumps(data),
                data=data,
                model=request.model,
                tokens_in=10,
                tokens_out=5,
                cost=0.001,
            )

    router._adapters["anthropic"] = Adapter()
    with TestClient(
        create_app(tmp_path / "ws", router=router, run_status_grace_seconds=3600)
    ) as client:
        project_id, sheet_id = _seed_web_search_project(client)
        project = client.app.state.workspace.get(project_id)
        path = _render_png(tmp_path / "scan.png", "HELLO")
        digest = project.add_blob(
            path.read_bytes(), filename=path.name, mime="image/png"
        )
        column = project.add_column(sheet_id, "page", type="image")
        [row_id] = project.add_rows(
            sheet_id,
            [{"page": {"blob": digest, "filename": path.name, "mime": "image/png"}}],
            {"page": column},
        )
        action = _spec(
            sheet_id, engine="anthropic/claude-haiku-4-5", keep_text_position=False
        )
        action["scope"]["row_ids"] = [row_id]
        action["output_names"].pop("blocks", None)
        _, response = _post_after_exact_confirmation(
            client, f"/api/projects/{project_id}/actions/v1/run", action=action
        )
        queued = ActionResult.model_validate(response.json())
        assert queued.status == "queued", response.text
        drain_queue(client)
        expected_requests = 1 if stop else 2
        expected_cost = 0.001 * expected_requests
        assert len(calls) == expected_requests
        facts = RunResultStore(project).model_calls(queued.run_id)
        assert sum(fact["provider_cost_usd"] for fact in facts) == pytest.approx(
            expected_cost
        ), [(fact["capability"], fact["provider_cost_usd"]) for fact in facts]
        assert (
            len(facts) == 1
        )  # OCR owns one document-level aggregate, including repair.
        assert facts[0]["capability"] == "ocr"
        assert project.db.execute(
            "SELECT cost_actual FROM runs WHERE id=?", (queued.run_id,)
        ).fetchone()[0] == pytest.approx(expected_cost)
        if stop:
            assert (
                project.db.execute(
                    "SELECT status FROM receipts WHERE id=?", (queued.receipt_id,)
                ).fetchone()[0]
                == "cancelled"
            )
            assert (
                project.db.execute(
                    "SELECT count(*) FROM cell_result_heads WHERE run_id=?",
                    (queued.run_id,),
                ).fetchone()[0]
                == 0
            )
