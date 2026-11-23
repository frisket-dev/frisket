"""Find's standalone worker uses the workspace's model-server configuration."""

import json

import httpx

from frisket.actions.types import ActionRequest
from frisket.engine.jobs import (
    Worker,
    default_registry,
    open_queue,
    register_production_handlers,
)
from frisket.engine.jobs.queue import ACTION_RUN_KIND
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from tests.engine.test_map_find_runtime import _queued_find
from tests.server.test_model_server_runs import _store_server


def test_confirmed_find_reaches_the_workspace_server_in_a_standalone_worker(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    root.mkdir()
    _store_server(root, "http://127.0.0.1:11434", None)
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
            "model": "ollama/@local-shared000001/listed-model",
        },
        sheet_name="Findings",
        idempotency_key="find-local-worker",
    )
    envelope = _queued_find(project, request, project_id="find")
    project.close()
    sent = []

    def server(request):
        sent.append(request)
        assert str(request.url) == "http://127.0.0.1:11434/v1/chat/completions"
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
    register_production_handlers(registry, workspace_root=root, queue=queue)
    job_id = queue.enqueue(
        ACTION_RUN_KIND,
        {"project_id": "find", "action_job": envelope.to_json()},
        max_attempts=1,
    )
    assert Worker(queue, registry).run_once()
    job = queue.get(job_id)
    assert job is not None
    assert job.status == "done", job.error
    assert len(sent) == 1
    finished = Project(root / "find.frisket")
    try:
        receipt = ReceiptStore(finished).parsed_by_id(envelope.receipt_id)
        assert receipt is not None
        assert receipt.status == "completed"
    finally:
        finished.close()
