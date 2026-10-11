from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.cloud_connection import CloudConnectionStore
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudSessionResponse,
    CloudUser,
)
from frisket.project_settings import ProjectSettings, write_project_settings
from frisket.server.app import create_app
from tests.deterministic_time import controlled_time


class _LocalAdapter:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def complete(self, request: Any, client: Any) -> LLMResponse:
        del client
        self.calls.append(request)
        data = {"value": "Local result"}
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            tokens_in=8,
            tokens_out=3,
            cost=0.00001,
            model=request.model,
        )


class _UnusedCloudClient:
    origin = "https://cloud.example.test"

    def capabilities(self, token: str, account_id: int) -> None:
        del token, account_id
        raise AssertionError("action composition must use stored Cloud state")

    def close(self) -> None:
        return None


def _session(*, user_id: int = 7, account_id: int = 41) -> CloudSessionResponse:
    return CloudSessionResponse(
        user=CloudUser(
            id=user_id,
            email=f"user-{user_id}@example.test",
            display_name=f"User {user_id}",
        ),
        expires_at="2099-01-01T00:00:00Z",
        accounts=[
            CloudAccount(
                id=account_id,
                name=f"Account {account_id}",
                kind="personal",
                balance_micro=1_000_000,
                available_micro=1_000_000,
                manage_billing=True,
            )
        ],
    )


def _client(
    tmp_path: Any, *, connection_state: str
) -> tuple[TestClient, _LocalAdapter]:
    store = CloudConnectionStore(tmp_path / "profile")
    if connection_state == "signed_out":
        store.set_preference("cloud_first")
    else:
        session = _session(
            user_id=8 if connection_state == "different_user" else 7,
            account_id=99 if connection_state == "different_user" else 41,
        )
        store.save_connection("installation-token", session)
        if connection_state == "unreadable":
            store.key_path.unlink()

    adapter = _LocalAdapter()
    router = ModelRouter(
        keys={"openai": "own-openai-key"},
        use_env_keys=False,
        cache_mode="off",
        max_retries=0,
    )
    router._adapters["openai"] = adapter  # noqa: SLF001 - hermetic boundary probe
    app = create_app(
        tmp_path / "workspace",
        router=router,
        cloud_connection_store=store,
        cloud_connection_client=_UnusedCloudClient(),
        serve_spa=False,
    )
    return TestClient(app), adapter


def _project_and_action(
    client: TestClient, *, preference: str
) -> tuple[str, dict[str, Any]]:
    project_id = client.post(
        "/api/projects", json={"name": "Cloud action routing"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Data")
    column_id = project.add_column(sheet_id, "text", type="text")
    [row_id] = project.add_rows(
        sheet_id, [{"text": "Extract this"}], {"text": column_id}
    )
    write_project_settings(
        project,
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference=preference,
        ),
    )
    return project_id, {
        "action_id": "map.extract",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            "row_ids": [row_id],
        },
        "params": {
            "source": ["text"],
            "model": "openai/gpt-5-mini",
            "instruction": "Extract the value",
            "fields": [{"name": "value", "type": "text"}],
            "grounding": {"enabled": False},
        },
        "idempotency_key": f"cloud-action-{preference}",
    }


@pytest.mark.parametrize(
    "connection_state", ["signed_out", "different_user", "unreadable"]
)
def test_cloud_first_map_action_and_preview_do_not_fall_back_to_own_key(
    tmp_path: Any, connection_state: str
) -> None:
    client, adapter = _client(tmp_path, connection_state=connection_state)
    with client:
        project_id, action = _project_and_action(client, preference="cloud_first")

        preview = client.post(
            f"/api/projects/{project_id}/actions/v1/preview", json=action
        )
        run = client.post(f"/api/projects/{project_id}/actions/v1/run", json=action)

    assert preview.status_code == 400, preview.text
    assert "Reconnect Frisket Cloud" in preview.text
    assert run.status_code == 400, run.text
    assert "Reconnect Frisket Cloud" in run.text
    assert adapter.calls == []


def test_explicit_local_first_map_preview_can_use_own_key(tmp_path: Any) -> None:
    client, adapter = _client(tmp_path, connection_state="signed_out")
    with client:
        project_id, action = _project_and_action(client, preference="local_first")
        challenge = client.post(
            f"/api/projects/{project_id}/actions/v1/preview", json=action
        )
        assert challenge.status_code == 202, challenge.text

        result: dict[str, Any] = {}

        def finished() -> bool:
            nonlocal result
            response = client.get(
                f"/api/projects/{project_id}/actions/v1/preview/"
                f"{challenge.json()['preview_id']}"
            )
            assert response.status_code == 200, response.text
            result = response.json()
            return result["status"] != "running"

        with controlled_time(timeout=10) as clock:
            clock.wait_until(finished, message="local preview did not finish")

    assert result["status"] == "done", result
    assert len(adapter.calls) == 1
