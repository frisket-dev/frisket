"""Selector reads are part of the actual app, including incomplete action drafts."""

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_application_serves_project_selector_for_an_incomplete_draft(tmp_path):
    app = create_app(tmp_path / "workspace", serve_spa=False)
    project = app.state.workspace.create("Choices", project_id="choices")
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project['id']}/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "action",
                    "action_id": "map.translate",
                    "field": "engine",
                    "params": {},
                },
            },
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema_version"] == "frisket.selector_choices.v1"
    assert payload["project_id"] == project["id"]
    assert payload["groups"]


def test_saved_gateway_reaches_selector_and_direct_execution(tmp_path, monkeypatch):
    from frisket.server.services import models_gateway

    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    monkeypatch.setattr(
        models_gateway,
        "probe_models_gateway",
        lambda *_args, **_kwargs: {
            "ok": True,
            "reachable": True,
            "status": 200,
            "detail": None,
            "service": "frisket-models",
            "version": "1",
            "engines": [],
        },
    )
    app = create_app(tmp_path / "workspace", serve_spa=False)
    workspace = app.state.workspace
    workspace.create("Choices", project_id="choices")
    candidate = {"origin": "https://models.example.test", "token": "gateway-test-key"}
    with TestClient(app) as client:
        validated = client.post("/api/models-gateway/validate", json=candidate)
        assert validated.status_code == 200, validated.text
        saved = client.put(
            "/api/models-gateway",
            json={
                **candidate,
                "validation_token": validated.json()["validation_token"],
            },
        )
        assert saved.status_code == 200, saved.text
        response = client.post(
            "/api/projects/choices/selector-choices",
            json={
                "schema_version": "frisket.selector_choices_query.v1",
                "subject": {
                    "kind": "action",
                    "action_id": "media.transcribe",
                    "field": "engine",
                    "params": {},
                },
            },
        )
    assert response.status_code == 200, response.text
    gateway_choices = [
        choice
        for group in response.json()["groups"]
        for choice in group["choices"]
        if choice["resolved_target"]
        and choice["resolved_target"]["target_id"] == "models-gateway"
    ]
    assert gateway_choices
    assert any(choice["can_run"] for choice in gateway_choices)
    project = workspace.get("choices")
    composition = workspace.execution_composition_for(
        project,
        workspace.action_execution_router_for(project),
        workspace.execution_composition_context_for(),
    )
    connection = composition.provider.connection("models-gateway")
    assert connection is not None
    assert connection.base_url == candidate["origin"]
    assert connection.token == candidate["token"]
