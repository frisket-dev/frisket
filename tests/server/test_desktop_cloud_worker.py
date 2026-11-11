"""Desktop must retain Cloud routing when its separate worker drains a job."""

from __future__ import annotations

import json

import pytest

import tests.server.test_cloud_action_preview_execution as cloudmod
from tests.engine.test_http_action_run_queue_boundary import _stub_router


SONNET = "anthropic/claude-sonnet-5-5"


@pytest.fixture
def queued(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "frisket.ai.llm.pricing_refresh.start_pricing_refresh", lambda: None
    )
    stub_router, stub_adapter = _stub_router({"translation": "Hello world"})
    monkeypatch.setattr(cloudmod, "ModelRouter", lambda **kw: stub_router)
    original_route = cloudmod._route

    def sonnet_route(capability):
        route = original_route(capability)
        if capability == "llm":
            return route.model_copy(update={"engine": SONNET, "provider": "anthropic"})
        return route

    monkeypatch.setattr(cloudmod, "_route", sonnet_route)
    original = cloudmod._Transport.execute_llm

    async def translate(self, *args, **kwargs):
        response = await original(self, *args, **kwargs)
        data = {"translation": "Hello world"}
        return response.model_copy(
            update={
                "receipt": response.receipt.model_copy(
                    update={"provider": "anthropic", "engine": SONNET}
                ),
                "result": response.result.model_copy(
                    update={
                        "content": json.dumps(data),
                        "data": data,
                        "model": SONNET,
                        "provider": "anthropic",
                    }
                ),
            }
        )

    monkeypatch.setattr(cloudmod._Transport, "execute_llm", translate)
    client, transport = cloudmod._client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Repro"}).json()["id"]
    cloudmod._select_cloud(client, project_id)
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Sheet 1")
    col = project.add_column(sheet_id, "transcript", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [{"transcript": "Bonjour le monde"}, {"transcript": "Hola mundo"}],
        {"transcript": col},
    )
    action = {
        "action_id": "map.translate",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "params": {
            "source": ["transcript"],
            "engine": "llm",
            "model": SONNET,
            "target_language": "English",
            "context": "",
            "language": [],
            "save_detected_language": False,
        },
        "idempotency_key": "repro-translate",
        "output_names": {"translation": "translation_3"},
    }
    path = f"/api/projects/{project_id}/actions/v1/run"
    first = client.post(path, json=action)
    assert first.status_code == 402, first.text
    action["confirmation"] = first.json()["errors"][0]["details"]["promise_set_hash"]
    first = client.post(path, json=action)
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["status"] == "queued", result
    return client, transport, stub_adapter, project_id, result


@pytest.mark.parametrize("signed_out", [False, True])
def test_desktop_cli_worker_honors_cloud_approval(
    queued, tmp_path, monkeypatch, signed_out
):
    from frisket import cli, cloud_connection, cloud_execution

    client, transport, stub_adapter, project_id, result = queued
    ws = client.app.state.workspace
    monkeypatch.setenv("FRISKET_CLOUD_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("FRISKET_CLOUD_ORIGIN", cloudmod._CloudClient.origin)
    if signed_out:
        cloud_connection.CloudConnectionStore(tmp_path / "profile").clear_connection()
    closed = []

    class CloudClient(cloudmod._CloudClient):
        def close(self):
            closed.append(True)

    monkeypatch.setattr(cloud_connection, "CloudConnectionClient", CloudClient)
    monkeypatch.setattr(
        cloud_execution, "HttpCloudExecutionTransport", lambda store, origin: transport
    )
    assert (
        cli.worker(
            [
                str(ws.root),
                "--drain",
                "--schedule-sources-interval",
                "0",
                "--schedule-notification-digests-interval",
                "0",
            ]
        )
        == 0
    )
    job = ws.queue.get(result["job_id"])
    assert closed == [True]
    assert stub_adapter.requests == []
    if signed_out:
        assert job.status == "failed"
        assert transport.llm_calls == []
        return
    assert job.status == "done", job.error
    assert len(transport.llm_calls) == 2
    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result['run_id']}/status"
    ).json()
    assert status["run"]["public_status"]["status"] == "completed"
