"""Find's standalone worker uses the workspace's model-server configuration."""

import json

import httpx
import pytest

from frisket.actions.types import ActionRequest
from frisket.ai.llm import ModelRouter
from frisket.engine.jobs import (
    Worker,
    default_registry,
    open_queue,
    register_production_handlers,
)
from frisket.engine.jobs.queue import ACTION_RUN_KIND
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.execution.purchases import LocalPurchasePolicy
from frisket.engine.jobs.runs import resolve_run_local_endpoints
from tests.engine.test_map_find_runtime import _queued_find
from tests.server.test_model_server_runs import _store_server


@pytest.mark.parametrize("organization_id", [None, 7])
def test_confirmed_find_reaches_the_authorized_server_in_a_standalone_worker(
    tmp_path, monkeypatch, organization_id
):
    root = tmp_path / "workspace"
    root.mkdir()
    _store_server(root, "http://127.0.0.1:11434", None)
    origin = "http://127.0.0.1:11434"
    endpoint_id = "local-shared000001"
    purchases = []

    class CompositionPolicy(LocalPurchasePolicy):
        def admit_purchase(self, request):
            purchases.append(request)
            return super().admit_purchase(request)

    if organization_id is not None:
        origin = "http://127.0.0.1:19434"
        monkeypatch.setenv("OLLAMA_URL", origin)
        endpoint_id = resolve_run_local_endpoints(root, organization_id=7)[
            0
        ].endpoint_id
    policy = CompositionPolicy()
    project = Project.create(root / "find.frisket", "Find")
    sheet = project.add_sheet("Sources")
    column = project.add_column(sheet, "text", "text")
    project.add_rows(sheet, [{"text": "A contract awarded to Acme."}], {"text": column})
    request = ActionRequest(
        action_id="map.find",
        scope={"kind": "sheet_rows", "sheet_id": sheet},
        params={
            "source": "text",
            "instruction": "Find Acme contracts.",
            "model": f"ollama/@{endpoint_id}/listed-model",
        },
        sheet_name="Findings",
        idempotency_key="find-local-worker",
    )
    envelope = _queued_find(project, request, project_id="find")
    project.close()
    sent = []

    def server(request):
        sent.append(request)
        assert str(request.url) == f"{origin}/v1/chat/completions"
        assert json.loads(request.content)["model"] == "listed-model"
        return httpx.Response(
            200,
            json={
                "id": "find-completion",
                "choices": [
                    {
                        "message": {"content": '{"matches":[]}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
        )

    original_init = httpx.AsyncClient.__init__

    def client_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(server)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", client_init)
    queue = open_queue(workspace=root)
    registry = default_registry()
    register_production_handlers(
        registry,
        workspace_root=root,
        queue=queue,
        execution_router_factory=(
            (lambda: ModelRouter(model_call_policy=policy, use_env_keys=False))
            if organization_id is not None
            else None
        ),
    )
    execute_find = registry.action_executor("map.find")
    assert execute_find is not None

    def authorized_find(project, envelope, *, execution, deps=None):
        assert deps is not None
        assert [server.origin for server in deps.router.local_endpoints] == [origin]
        if organization_id is not None:
            assert deps.router.model_call_policy is policy
        return execute_find(project, envelope, execution=execution, deps=deps)

    registry.register_action_executor("map.find", authorized_find)
    job_id = queue.enqueue(
        ACTION_RUN_KIND,
        {
            "project_id": "find",
            "organization_id": organization_id,
            "action_job": envelope.to_json(),
        },
        max_attempts=1,
    )
    assert Worker(queue, registry).run_once()
    job = queue.get(job_id)
    assert job is not None
    assert job.status == "done", job.error
    assert len(sent) == 1
    if organization_id is not None:
        assert len(purchases) == 1
    finished = Project(root / "find.frisket")
    try:
        receipt = ReceiptStore(finished).parsed_by_id(envelope.receipt_id)
        assert receipt is not None
        assert receipt.status == "completed"
    finally:
        finished.close()
