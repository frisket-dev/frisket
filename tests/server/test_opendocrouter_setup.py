"""Inline setup and engine discovery use the ordinary public API."""

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.credentials import resolve_credential_with_source
from frisket.execution.definitions import StaticExecutionTargetProvider
from frisket.server import provider_config
from frisket.server.app import create_app


@pytest.mark.parametrize("action", ["media.ocr", "media.to_markdown"])
def test_catalog_key_setup_and_target(tmp_path, monkeypatch, action):
    monkeypatch.delenv("OPEN_DOC_ROUTER_API_KEY", raising=False)
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://www.opendocrouter.ai/v1/credits"
        assert request.method == "GET"
        return httpx.Response(200, json={"balance_usd": 5})

    transport = httpx.Client(transport=httpx.MockTransport(handler))
    probe = provider_config.probe_provider
    monkeypatch.setattr(
        provider_config,
        "probe_provider",
        lambda provider, key: probe(provider, key, client=transport),
    )
    with TestClient(
        create_app(tmp_path / "ws", router=ModelRouter(use_env_keys=False))
    ) as client:
        pid = client.post("/api/projects", json={"name": "Documents"}).json()["id"]

        def choices():
            response = client.post(
                f"/api/projects/{pid}/selector-choices",
                json={
                    "schema_version": "frisket.selector_choices_query.v1",
                    "subject": {
                        "kind": "action",
                        "action_id": action,
                        "field": "engine",
                        "params": {},
                    },
                },
            )
            assert response.status_code == 200, response.text
            return [
                c
                for g in response.json()["groups"]
                for c in g["choices"]
                if c["authored_selection"]
                .get("engine", "")
                .startswith("opendocrouter/")
            ]

        missing = choices()
        assert len(missing) == 11
        assert all(
            c["status"] == "needs_setup" and c["setup"]["provider"] == "opendocrouter"
            for c in missing
        )
        endpoint = f"/api/projects/{pid}/provider-keys"
        validated = client.post(
            endpoint + "/validate",
            json={"provider": "opendocrouter", "key": "test-odr-secret"},
        )
        assert validated.status_code == 200 and validated.json()["ok"], validated.text
        saved = client.post(
            endpoint,
            json={
                "provider": "opendocrouter",
                "key": "test-odr-secret",
                "validation_token": validated.json()["validation_token"],
            },
        )
        assert saved.status_code == 200, saved.text
        assert "test-odr-secret" not in saved.text
        project = client.app.state.workspace.get(pid)
        credential = resolve_credential_with_source(project, "OPEN_DOC_ROUTER_API_KEY")
        assert (
            credential.value == "test-odr-secret" and credential.source == "project_key"
        )
        assert "test-odr-secret" not in repr(credential)
        target = StaticExecutionTargetProvider(secrets=project, env={})
        assert target.connection("opendocrouter").token == credential.value
        available = choices()
        assert all(c["status"] == "ready" for c in available), available
        odr = next(t for t in target.targets() if t.id == "opendocrouter")
        assert len(odr.engines) == 22
        assert {s.transport for s in odr.engines} == {"opendocrouter.parse"}
        # The already-running app exposes a refreshed model without a restart.
        from dataclasses import replace
        from frisket.opendocrouter_catalog import current_catalog, DocumentCatalog

        catalog = current_catalog()
        model = replace(catalog.models[0], id="example/new-parser", name="New parser")
        monkeypatch.setattr(
            "frisket.opendocrouter_catalog._catalog",
            DocumentCatalog(catalog.price_version, (*catalog.models, model)),
        )
        refreshed = choices()
        added = next(
            c for c in refreshed if c["authored_selection"]["engine"] == model.engine
        )
        assert added["status"] == "ready"
        assert "New parser" in str(added)

    transport.close()
