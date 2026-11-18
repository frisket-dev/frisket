"""User-named HTTP secrets belong to the project, not the server process."""

from __future__ import annotations
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

import asyncio

import httpx
import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.executor.actions import run_action_spec
from frisket.ops.integrations.mcp_project import project_mcp_launch_config
from frisket.team.security.secrets import encrypt_secret
from tests.ops.test_api_call import _allow_egress as _allow_egress


@pytest.fixture
def request_project(make_project, monkeypatch, _allow_egress):
    monkeypatch.setenv("FRISKET_SECRETS_MASTER_KEY", "fake-test-master-key")
    project = make_project("request")
    sheet = project.add_sheet("Rows")
    project.add_rows(sheet, [{}], {})
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))

    def run(request):
        body = {
            "action_id": "map.api_call",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"request": request},
            "idempotency_key": "secret-isolation",
        }
        result = run_action_spec(project, body, project_id="api", router=router)
        if result.status == "needs_confirmation":
            body["confirmation"] = result.errors[0].details["promise_set_hash"]
            result = run_action_spec(project, body, project_id="api", router=router)
        return result

    try:
        yield project, sent, run
    finally:
        asyncio.run(router._client.aclose())
        project.close()


@pytest.mark.parametrize("location", ["url", "headers", "query_params", "body"])
def test_process_only_secret_refuses_before_any_outbound_request(
    request_project, monkeypatch, location
):
    project, sent, run = request_project
    monkeypatch.setenv("FRISKET_FAKE_OPERATOR_SECRET", "fake-operator-only-value")
    token = "{{secret.FRISKET_FAKE_OPERATOR_SECRET}}"
    request = {"url": "https://api.test.example/collect"}
    if location == "url":
        request["url"] += "/" + token
    elif location == "body":
        request.update(method="POST", body_mode="raw", body=token)
    else:
        request[location] = [["Authorization", token]]
    result = run(request)
    assert sent == []
    assert result.status == "failed", result.errors
    errors = [row[0] for row in project.db.execute("SELECT error FROM results")]
    assert errors and all("missing_secret" in error for error in errors)
    assert all("fake-operator-only-value" not in error for error in errors)


@pytest.mark.parametrize("name", ["CUSTOM_TOKEN", "OPENAI_API_KEY"])
def test_project_secret_wins_over_same_named_operator_environment(
    request_project, monkeypatch, name
):
    project, sent, run = request_project
    monkeypatch.setenv(name, "fake-operator-value")
    project.set_secret(
        name=name, encrypted=encrypt_secret("fake-project-value"), hint="fake"
    )
    result = run(
        {
            "url": "https://api.test.example/collect",
            "headers": [["Authorization", "Bearer {{secret." + name + "}}"]],
        }
    )
    assert result.status == "completed", result.errors
    assert len(sent) == 1
    assert sent[0].headers["Authorization"] == "Bearer fake-project-value"


def test_other_project_secret_cannot_satisfy_request(request_project, make_project):
    project, sent, run = request_project
    other = make_project("other")
    other.set_secret(
        name="OTHER_TOKEN",
        encrypted=encrypt_secret("fake-other-value"),
        hint="fake",
    )
    result = run(
        {
            "url": "https://api.test.example/collect",
            "headers": [["Authorization", "{{secret.OTHER_TOKEN}}"]],
        }
    )
    assert result.status == "failed", result.errors
    assert sent == []
    error = project.db.execute("SELECT error FROM results").fetchone()[0]
    assert "missing_secret" in error


def test_provider_setup_key_is_not_a_user_template_secret(request_project):
    project, sent, run = request_project
    project.set_provider_key(
        provider="datalab",
        encrypted=encrypt_secret("fake-provider-key"),
        hint="fake",
        spend_cap_micro=None,
    )
    result = run(
        {
            "url": "https://api.test.example/collect",
            "headers": [["Authorization", "{{secret.DATALAB_API_KEY}}"]],
        }
    )
    assert sent == []
    assert result.status == "failed", result.errors
    error = project.db.execute("SELECT error FROM results").fetchone()[0]
    assert "missing_secret" in error


@pytest.mark.parametrize("has_project_secret", [False, True])
def test_mcp_project_secret_binding_never_uses_operator_environment(
    request_project, monkeypatch, has_project_secret
):
    project, _, _ = request_project
    monkeypatch.setenv("OPENAI_API_KEY", "fake-operator-value")
    if has_project_secret:
        project.set_secret(
            name="OPENAI_API_KEY",
            encrypted=encrypt_secret("fake-project-value"),
            hint="fake",
        )
    server = {
        "command": "unused-command-not-spawned",
        "env": {"TOOL_TOKEN": {"project_secret": "OPENAI_API_KEY"}},
    }
    if not has_project_secret:
        with pytest.raises(ValueError, match="missing_project_secret"):
            project_mcp_launch_config(project, server)
    else:
        config, secrets = project_mcp_launch_config(project, server)
        assert config.env["TOOL_TOKEN"] == "fake-project-value"
        assert "fake-operator-value" not in config.env.values()
        assert secrets == ("fake-project-value",)
