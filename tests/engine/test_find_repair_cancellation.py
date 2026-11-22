"""Cancelling Find's repair turn retains the first returned purchase."""

from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

import asyncio

from frisket.actions.types import ActionRequest
from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.engine.executor.action_bindings import action_job_bindings
from frisket.engine.executor.action_inventory import ExecutorDeps
from frisket.engine.executor.action_jobs import run_action_run_job
from tests.engine.test_map_find_runtime import _queued_find


def test_cancel_before_find_repair_preserves_returned_usage(project):
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"anthropic": "test"},
        cache_mode="off",
        use_env_keys=False,
    )
    try:
        sheet = project.add_sheet("Sources")
        column = project.add_column(sheet, "text", "text")
        project.add_rows(
            sheet, [{"text": "A contract awarded to Acme."}], {"text": column}
        )
        request = ActionRequest(
            action_id="map.find",
            scope={"kind": "sheet_rows", "sheet_id": sheet},
            params={
                "source": "text",
                "instruction": "Find Acme contracts.",
                "model": "anthropic/claude-haiku-4-5",
            },
            sheet_name="Findings",
            output_names={"match": "Match"},
            idempotency_key="find-cancel-repair",
        )
        envelope = _queued_find(project, request, project_id="find-cancel")
        cancelled = False
        requests = []

        class Adapter:
            async def complete(self, req, client):
                nonlocal cancelled
                requests.append(req)
                assert len(requests) == 1, "repair must not make a second purchase"
                cancelled = True
                return LLMResponse(
                    content='{"wrong":true}',
                    data={"wrong": True},
                    tokens_in=20,
                    tokens_out=5,
                    cost=0.03,
                    model=req.model,
                    cost_source="provider_reported",
                )

        router._adapters["anthropic"] = Adapter()
        bindings = action_job_bindings(
            executor_deps_factory=lambda _project_id, _run_id: ExecutorDeps(
                router=router
            )
        )
        result = run_action_run_job(
            project,
            {"action_job": envelope.to_json(), "job_id": 7},
            executor_lookup=bindings.get,
            should_cancel=lambda: cancelled,
        )
        assert result.status == "cancelled"
        assert len(requests) == 1
        calls = project.db.execute(
            "SELECT json_extract(units, '$.tokens_in'), "
            "json_extract(units, '$.tokens_out'), provider_cost_usd FROM model_calls"
        ).fetchall()
        assert [tuple(call) for call in calls] == [(20, 5, 0.03)]
        assert (
            project.db.execute(
                "SELECT count(*) FROM sheets WHERE name='Findings' AND hidden=0"
            ).fetchone()[0]
            == 0
        )
    finally:
        asyncio.run(router.aclose())
