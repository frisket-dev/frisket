from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.types import LLMError, LLMRequest
from frisket.engine.jobs import runs as run_jobs
from frisket.engine.executor.action_reservations import (
    _local_endpoint_bindings_for_runner_spec,
)
from frisket.engine.store import Project
from frisket.server import provider_config
from frisket.server.app import create_app
from frisket.server.workspace import Workspace


def _probe(origin: str, **_kwargs) -> dict:
    if origin.endswith("9999"):
        return {
            "reachable": False,
            "models": [],
            "protocol": "unknown",
            "auth_status": "unknown",
            "detail": "connection refused",
        }
    name = "qwen3:8b" if origin.endswith("11434") else "local-model"
    return {
        "reachable": True,
        "models": [name],
        "protocol": "ollama_native"
        if origin.endswith("11434")
        else "openai_compatible",
        "auth_status": "ok",
        "detail": None,
    }


def test_no_configuration_means_no_local_endpoint_or_catalog_row(tmp_path) -> None:
    resolved, notes = provider_config.resolve_local_endpoints(tmp_path, {})
    assert resolved == ()
    assert notes == []
    assert all(
        row["kind"] != "local_http"
        for row in provider_config.build_provider_catalog(tmp_path, {})["providers"]
    )
    assert ModelRouter(use_env_keys=False, local_endpoints=()).local_endpoints == ()


def test_every_persisted_endpoint_uses_one_record_path_and_immutable_origin(
    tmp_path,
) -> None:
    first = provider_config.create_local_endpoint(
        tmp_path, name="Ollama workstation", url="http://127.0.0.1:11434"
    )
    second = provider_config.create_local_endpoint(
        tmp_path, name="LM Studio", url="http://127.0.0.1:1234"
    )

    resolved, notes = provider_config.resolve_local_endpoints(tmp_path, {})
    assert notes == []
    assert [(row.endpoint_id, row.origin) for row in resolved] == [
        (first.endpoint_id, first.origin),
        (second.endpoint_id, second.origin),
    ]
    renamed = provider_config.patch_local_endpoint(
        tmp_path, second.endpoint_id, {"display_name": "LM Studio PC"}
    )
    assert renamed.origin == second.origin
    with pytest.raises(ValueError, match="unknown local endpoint patch field"):
        provider_config.patch_local_endpoint(
            tmp_path, second.endpoint_id, {"origin": "http://127.0.0.1:1235"}
        )


def test_discovery_probes_only_fixed_local_origins_and_adds_qualified_servers(
    tmp_path, monkeypatch
) -> None:
    seen: list[tuple[str, float]] = []

    def probe(origin: str, *, timeout: float) -> dict:
        seen.append((origin, timeout))
        if origin.endswith(("11434", "1234")):
            return {
                "reachable": True,
                "models": ["local-model"],
                "protocol": (
                    "ollama_native" if origin.endswith("11434") else "openai_compatible"
                ),
                "auth_status": "ok",
                "detail": None,
            }
        return {
            "reachable": False,
            "models": [],
            "protocol": "unknown",
            "auth_status": "unknown",
            "detail": "connection refused",
        }

    monkeypatch.setattr(provider_config, "ollama_reachable", probe)

    result = provider_config.discover_local_endpoints(tmp_path)

    assert len(seen) == len(provider_config.LOCAL_ENDPOINT_DISCOVERY_CANDIDATES)
    assert set(seen) == {
        (origin, provider_config.LOCAL_ENDPOINT_DISCOVERY_TIMEOUT_SECONDS)
        for _label, origin in provider_config.LOCAL_ENDPOINT_DISCOVERY_CANDIDATES
    }
    assert result == {
        "candidates": [
            {
                "label": "Ollama",
                "origin": "http://localhost:11434",
                "outcome": "added",
            },
            {
                "label": "LM Studio",
                "origin": "http://localhost:1234",
                "outcome": "added",
            },
            {
                "label": "llama.cpp",
                "origin": "http://localhost:8080",
                "outcome": "not_found",
            },
            {
                "label": "vLLM",
                "origin": "http://localhost:8000",
                "outcome": "not_found",
            },
        ]
    }
    configured, notes = provider_config.resolve_local_endpoints(tmp_path, {})
    assert notes == []
    assert [(endpoint.display_name, endpoint.origin) for endpoint in configured] == [
        ("Ollama", "http://localhost:11434"),
        ("LM Studio", "http://localhost:1234"),
    ]


def test_discovery_does_not_probe_or_rewrite_an_already_configured_origin(
    tmp_path, monkeypatch
) -> None:
    existing = provider_config.create_local_endpoint(
        tmp_path,
        name="My LM Studio",
        url="http://localhost:1234",
    )
    config_path = tmp_path / ".frisket" / "provider_keys.json"
    before = config_path.read_bytes()
    seen: list[str] = []

    def probe(origin: str, *, timeout: float) -> dict:
        assert timeout == provider_config.LOCAL_ENDPOINT_DISCOVERY_TIMEOUT_SECONDS
        seen.append(origin)
        return {
            "reachable": False,
            "models": [],
            "protocol": "unknown",
            "auth_status": "unknown",
            "detail": "connection refused",
        }

    monkeypatch.setattr(provider_config, "ollama_reachable", probe)

    result = provider_config.discover_local_endpoints(tmp_path)

    assert "http://localhost:1234" not in seen
    assert len(seen) == len(provider_config.LOCAL_ENDPOINT_DISCOVERY_CANDIDATES) - 1
    assert result["candidates"][1] == {
        "label": "LM Studio",
        "origin": "http://localhost:1234",
        "outcome": "already_added",
    }
    assert config_path.read_bytes() == before
    configured = provider_config.load_local_endpoints(tmp_path)
    assert configured == (existing,)


def test_catalog_attributes_only_qualified_models_to_each_endpoint(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(provider_config, "ollama_reachable", _probe)
    first = provider_config.create_local_endpoint(
        tmp_path, name="Ollama", url="http://127.0.0.1:11434"
    )
    second = provider_config.create_local_endpoint(
        tmp_path, name="LM Studio", url="http://127.0.0.1:1234"
    )
    offline = provider_config.create_local_endpoint(
        tmp_path, name="llama.cpp", url="http://127.0.0.1:9999"
    )

    local = [
        row
        for row in provider_config.build_provider_catalog(tmp_path, {})["providers"]
        if row["kind"] == "local_http"
    ]
    assert [row["endpoint_id"] for row in local] == [
        first.endpoint_id,
        second.endpoint_id,
        offline.endpoint_id,
    ]
    assert all("id" not in row for row in local)
    assert local[0]["models"][0]["id"] == f"ollama/@{first.endpoint_id}/qwen3:8b"
    assert local[1]["models"][0]["id"] == f"ollama/@{second.endpoint_id}/local-model"
    assert local[2]["models"] == []


def test_workspace_and_worker_resolve_the_same_endpoint_list(tmp_path) -> None:
    first = provider_config.create_local_endpoint(
        tmp_path, name="Ollama", url="http://127.0.0.1:11434"
    )
    second = provider_config.create_local_endpoint(
        tmp_path, name="LM Studio", url="http://127.0.0.1:1234"
    )
    project = Project.create(tmp_path / "project.frisket", name="project")
    try:
        live = Workspace(tmp_path).router_for(project).local_endpoints
        queued = run_jobs.resolve_run_local_endpoints(tmp_path, org_id=None, env={})
    finally:
        project.close()
    assert [endpoint.endpoint_id for endpoint in live] == [
        first.endpoint_id,
        second.endpoint_id,
    ]
    assert queued == live


def test_queued_runner_spec_freezes_only_canonical_model_authority() -> None:
    endpoint = LocalModelEndpointConfig(
        endpoint_id="studio",
        display_name="LM Studio",
        origin="http://studio:1234",
        source="local_file",
    )
    router = ModelRouter(use_env_keys=False, local_endpoints=(endpoint,))

    bindings = _local_endpoint_bindings_for_runner_spec(
        {
            "model": "ollama/@studio/qwen3:8b",
            "prompt": "ollama/this-is-ordinary-user-text",
        },
        router=router,
    )

    assert bindings == [{"endpoint_id": "studio", "origin": endpoint.origin}]


@pytest.mark.asyncio
async def test_router_sends_qualified_completion_and_embedding_to_selected_endpoint(
    monkeypatch,
) -> None:
    first = LocalModelEndpointConfig(
        endpoint_id="ollama-workstation",
        display_name="Ollama workstation",
        origin="http://ollama:11434",
        source="local_file",
    )
    second = LocalModelEndpointConfig(
        endpoint_id="studio",
        display_name="LM Studio",
        origin="http://studio:1234",
        source="local_file",
    )
    router = ModelRouter(use_env_keys=False, local_endpoints=(first, second))
    seen: list[tuple[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((str(request.url), body))
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})
        return httpx.Response(
            200,
            json={
                "id": "completion-1",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    embedding_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(
        "frisket.ai.llm.router.httpx.AsyncClient",
        lambda *_args, **_kwargs: embedding_client,
    )
    model_id = "ollama/@studio/qwen3:8b"
    try:
        response = await router.complete(
            LLMRequest(model=model_id, messages=[{"role": "user", "content": "hi"}])
        )
        vectors = await router.embed(["hello"], model=model_id)
    finally:
        await router._client.aclose()

    assert [url for url, _body in seen] == [
        "http://studio:1234/v1/chat/completions",
        "http://studio:1234/v1/embeddings",
    ]
    assert all(body["model"] == "qwen3:8b" for _url, body in seen)
    assert response.model == model_id
    assert vectors == [[0.1, 0.2]]
    with pytest.raises(LLMError, match="ollama/@"):
        router.resolve_local_model("ollama/qwen3:8b")


def test_local_endpoint_routes_are_strict_patch_resources(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(provider_config, "ollama_reachable", _probe)
    client = TestClient(create_app(tmp_path / "workspace"))

    rejected_aliases = client.post(
        "/api/providers/local-endpoints",
        json={"name": "LM Studio", "url": "http://127.0.0.1:1234"},
    )
    assert rejected_aliases.status_code == 422

    created = client.post(
        "/api/providers/local-endpoints",
        json={"display_name": "LM Studio", "origin": "http://127.0.0.1:1234"},
    )
    assert created.status_code == 200, created.text
    endpoint = next(
        row for row in created.json()["providers"] if row["kind"] == "local_http"
    )

    updated = client.patch(
        f"/api/providers/local-endpoints/{endpoint['endpoint_id']}",
        json={"display_name": "LM Studio PC"},
    )
    assert updated.status_code == 200, updated.text
    assert any(row["label"] == "LM Studio PC" for row in updated.json()["providers"])
    no_op = client.patch(
        f"/api/providers/local-endpoints/{endpoint['endpoint_id']}",
        json={},
    )
    assert no_op.status_code == 422
    immutable = client.patch(
        f"/api/providers/local-endpoints/{endpoint['endpoint_id']}",
        json={"origin": "http://127.0.0.1:1235"},
    )
    assert immutable.status_code == 422

    deleted = client.delete(f"/api/providers/local-endpoints/{endpoint['endpoint_id']}")
    assert deleted.status_code == 200, deleted.text
    assert all(row["kind"] != "local_http" for row in deleted.json()["providers"])


def test_local_endpoint_discovery_route_returns_typed_fixed_candidates(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(provider_config, "ollama_reachable", _probe)
    client = TestClient(create_app(tmp_path / "workspace"))

    response = client.post("/api/providers/local-endpoints/discover")

    assert response.status_code == 200, response.text
    assert [candidate["outcome"] for candidate in response.json()["candidates"]] == [
        "added",
        "added",
        "added",
        "added",
    ]
