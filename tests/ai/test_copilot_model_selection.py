"""Copilot model-selection and error-remediation contract.

Two prior defects are pinned here:

1. The copilot model is HARDCODED: ``frisket.copilot.COPILOT_MODEL`` is
   ``anthropic/claude-sonnet-4-6`` and ``CopilotRequest`` carries no model
   field, so no UI affordance can exist. On a keyless workspace with no
   explicitly configured local endpoint, the copilot can NEVER work.
2. The copilot error path bypasses the error-CLASS remediation seam
   (``frisket/llm/remediation.py``, onboard-error-remediation-v1) that was
   built exactly for this failure: ``ProjectCopilotService.chat`` re-raises
   ``str(exc)`` verbatim, so the user reads adapter-registry internals instead
   of "No API key is configured for 'anthropic'. Add one in Settings → AI
   Providers, or pick a different model."

The contract is:
- ``CopilotRequest`` accepts an OPTIONAL ``model`` ("provider/name", nonblank,
  default None — absent keeps today's wire shape valid).
- The endpoint routes the REQUESTED model to the LLM seam verbatim.
- When the request names no model, the service prefers a CONFIGURED provider
  (anthropic → openai → gemini, each at its copilot-quality default) before
  falling back to the legacy ``COPILOT_MODEL`` constant — so a gemini-only
  workspace gets a working copilot instead of a guaranteed 502.
- LLM failures surface REMEDIATION copy through the existing
  ``classify_llm_error`` seam: missing-key failures name the provider and
  Settings → AI Providers; an unreachable-Ollama failure names the configured
  URL. The raw "no adapter for provider" router string never reaches the wire.

Fake-model idiom follows tests/test_entities_llm_dispatch.py: a real
``ModelRouter`` with a recording stub adapter injected into ``_adapters`` —
no network, no cassettes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.contracts.http.copilot import CopilotRequest
from frisket.ai.llm import ModelRouter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.ai.llm.remediation import missing_provider_key_message
from frisket.ai.llm.types import LLMRequest, LLMResponse
from frisket.server.app import create_app


COPILOT_REPLY: dict[str, Any] = {
    "reply": "Here is a plan.",
    "needs_import": False,
    "proposals": [],
}


class _RecordingAdapter:
    """Records every LLMRequest and answers with a fixed, schema-valid reply."""

    def __init__(self, reply: dict[str, Any]):
        self.reply = reply
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        del client
        self.requests.append(req)
        return LLMResponse(
            content=json.dumps(self.reply),
            data=dict(self.reply),
            tokens_in=42,
            tokens_out=17,
            cost=0.004,
            model=req.model,
        )


def _stub_router(*providers: str) -> tuple[ModelRouter, _RecordingAdapter]:
    """A hermetic router configured with exactly ``providers`` (fake keys),
    every one of them answered by the same recording stub adapter."""
    router = ModelRouter(
        keys={provider: "k" for provider in providers},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    adapter = _RecordingAdapter(COPILOT_REPLY)
    for provider in providers:
        router._adapters[provider] = adapter  # noqa: SLF001
    return router, adapter


def _copilot_client(tmp_path: Path, router: ModelRouter) -> tuple[TestClient, str]:
    client = TestClient(create_app(tmp_path / "ws", router=router))
    project_id = client.post("/api/projects", json={"name": "Copilot"}).json()["id"]
    return client, project_id


def _chat(client: TestClient, project_id: str, *, model: str | None = None) -> Any:
    body: dict[str, Any] = {"messages": [{"role": "user", "content": "help"}]}
    if model is not None:
        body["model"] = model
    return client.post(f"/api/projects/{project_id}/copilot", json=body)


# ---------------------------------------------------------------------------
# wire contract
# ---------------------------------------------------------------------------


def test_copilot_request_model_field_is_optional_and_shaped() -> None:
    messages = [{"role": "user", "content": "hi"}]

    assert CopilotRequest.model_validate({"messages": messages}).model is None
    assert (
        CopilotRequest.model_validate(
            {"messages": messages, "model": "gemini/gemini-2.5-flash"}
        ).model
        == "gemini/gemini-2.5-flash"
    )
    # Qualified local tags (colons/dots) and dotted Gemini names stay valid.
    assert (
        CopilotRequest.model_validate(
            {"messages": messages, "model": "ollama/@desk/qwen3:8b"}
        ).model
        == "ollama/@desk/qwen3:8b"
    )
    for bad in (
        "",
        "   ",
        "claude",
        "/qwen3:8b",
        "ollama/",
        "ollama/qwen3:8b",
        # No surrounding whitespace or URL-shaped/uppercase provider tokens;
        # length is bounded.
        " anthropic/claude-haiku-4-5",
        "anthropic/claude-haiku-4-5 ",
        "anthropic/ claude",
        "anthropic /claude",
        "http://x",
        "Anthropic/claude-haiku-4-5",
        "anthropic/" + "x" * 300,
    ):
        with pytest.raises(ValidationError):
            CopilotRequest.model_validate({"messages": messages, "model": bad})


# ---------------------------------------------------------------------------
# model routing
# ---------------------------------------------------------------------------


def test_copilot_endpoint_routes_the_requested_model_verbatim(
    tmp_path: Path,
) -> None:
    router, adapter = _stub_router("anthropic")
    client, project_id = _copilot_client(tmp_path, router)

    response = _chat(client, project_id, model="anthropic/claude-haiku-4-5")

    assert response.status_code == 200
    assert [req.model for req in adapter.requests] == ["anthropic/claude-haiku-4-5"]


def test_copilot_default_model_prefers_a_configured_provider(
    tmp_path: Path,
) -> None:
    """A gemini-only workspace must not default to the unconfigured anthropic
    constant: no-model requests route to the configured provider instead."""
    router, adapter = _stub_router("gemini")
    client, project_id = _copilot_client(tmp_path, router)

    response = _chat(client, project_id)

    assert response.status_code == 200
    assert adapter.requests, "expected the copilot to reach the LLM seam"
    assert {req.model for req in adapter.requests} == {"gemini/gemini-3.6-flash"}


# ---------------------------------------------------------------------------
# remediation copy on the wire
# ---------------------------------------------------------------------------


def test_copilot_missing_key_surfaces_remediation_copy_not_router_internals(
    tmp_path: Path,
) -> None:
    keyless = ModelRouter(cache=None, cache_mode="off", use_env_keys=False)
    client, project_id = _copilot_client(tmp_path, keyless)

    response = _chat(client, project_id)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail == missing_provider_key_message("anthropic")
    assert "no adapter for provider" not in detail


def test_copilot_unreachable_ollama_names_the_configured_url(
    tmp_path: Path,
) -> None:
    """An explicit ollama model against a dead address must say no local AI
    server is responding at <url> (local-openai-compat-server-v1 copy), not
    leak transport internals. Port 9 (discard) on loopback refuses
    connections without any network dependency."""
    dead_url = "http://127.0.0.1:9"
    router = ModelRouter(
        local_endpoints=(
            LocalModelEndpointConfig(
                endpoint_id="dead-server",
                display_name="Dead server",
                origin=dead_url,
                source="local_file",
            ),
        ),
        cache=None,
        cache_mode="off",
        use_env_keys=False,
        max_retries=1,
    )
    client, project_id = _copilot_client(tmp_path, router)

    response = _chat(client, project_id, model="ollama/@dead-server/qwen3:8b")

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert f"No local AI server is responding at {dead_url}" in detail
