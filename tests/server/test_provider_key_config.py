"""Local-tier provider key config + validate probe (onboard-model-picker-
provider-config-v1).

RED-first 2026-07-03. The local server persists UI-entered provider keys to a
gitignored workspace config file (``<workspace>/.frisket/provider_keys.json``),
loads them at startup, and lets environment variables win on conflict. A
per-provider validate probe issues the cheapest possible real request and never
echoes key values back to the client. Ollama gets a real reachability status
instead of the always-registered-unchecked adapter.

These are white-box tests for ``frisket.server.provider_config`` plus black-box
tests of the ``/api/providers`` routes.
"""

from __future__ import annotations

import json
import os

import httpx
from fastapi.testclient import TestClient

from frisket.server import provider_config
from frisket.server.app import create_app

SECRET = "sk-openai-supersecret-1234"


# ---------------------------------------------------------------------------
# persistence + env-wins (module level)
# ---------------------------------------------------------------------------


def test_key_persists_to_gitignored_file_with_locked_down_perms(tmp_path) -> None:
    root = tmp_path / "ws"
    provider_config.save_local_provider_key(root, "openai", SECRET)

    path = provider_config.local_secrets_path(root)
    assert path.exists()
    # gitignored location: under .frisket, plus a belt-and-suspenders .gitignore
    assert path.parent.name == ".frisket"
    gitignore = path.parent / ".gitignore"
    assert gitignore.exists() and "*" in gitignore.read_text()
    # secret at rest is owner-only
    assert oct(path.stat().st_mode)[-3:] == "600"

    # round-trips from the file (unencrypted inside the gitignored file is allowed)
    assert provider_config.load_local_provider_keys(root)["openai"] == SECRET


def test_delete_removes_only_that_provider(tmp_path) -> None:
    root = tmp_path / "ws"
    provider_config.save_local_provider_key(root, "openai", SECRET)
    provider_config.save_local_provider_key(root, "anthropic", "sk-ant-keep")

    assert provider_config.delete_local_provider_key(root, "openai") is True
    keys = provider_config.load_local_provider_keys(root)
    assert "openai" not in keys
    assert keys["anthropic"] == "sk-ant-keep"
    # deleting an absent provider is a no-op, not an error
    assert provider_config.delete_local_provider_key(root, "openai") is False


def test_env_var_wins_over_file_key(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ws"
    provider_config.save_local_provider_key(root, "openai", "sk-file-loser")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-winner")

    effective = provider_config.resolve_effective_keys(root, env=dict(os.environ))
    # env-owned provider is dropped from the file layer so the router falls back
    # to the environment for it (env wins on conflict).
    assert "openai" not in effective

    status = {
        row["id"]: row
        for row in provider_config.provider_key_status(root, env=dict(os.environ))
    }
    assert status["openai"]["configured"] is True
    assert status["openai"]["source"] == "env"


def test_status_reports_local_file_source_and_hint_only(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ws"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider_config.save_local_provider_key(root, "openai", SECRET)

    status = {
        row["id"]: row for row in provider_config.provider_key_status(root, env={})
    }
    openai = status["openai"]
    assert openai["configured"] is True
    assert openai["source"] == "local_file"
    # only the tail is ever revealed; the full secret never appears
    assert openai["hint"] == "...1234"
    assert SECRET not in json.dumps(status)


# ---------------------------------------------------------------------------
# validate probe (cheapest real request, never echoes the key)
# ---------------------------------------------------------------------------


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_probe_valid_key_reports_ok_and_never_echoes_key() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["x_api_key"] = request.headers.get("x-api-key")
        # cheapest real request is a models listing (no tokens billed)
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": []})

    result = provider_config.probe_provider(
        "openai", "sk-live-valid-abcd", client=_mock_client(handler)
    )
    assert result["ok"] is True
    assert result["reachable"] is True
    assert result["status"] == 200
    # the probe carried the key on the wire but never returns it to the caller
    assert "sk-live-valid-abcd" in (seen.get("auth") or "")
    assert "sk-live-valid-abcd" not in json.dumps(result)


def test_probe_bad_key_reports_not_ok_but_reachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid key"})

    result = provider_config.probe_provider(
        "anthropic", "sk-ant-bad", client=_mock_client(handler)
    )
    assert result["reachable"] is True
    assert result["ok"] is False
    assert result["status"] == 401


def test_probe_unreachable_host_reports_not_reachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    result = provider_config.probe_provider(
        "openai", "sk-x", client=_mock_client(handler)
    )
    assert result["reachable"] is False
    assert result["ok"] is False


def test_ollama_reachability_probe() -> None:
    def up(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})

    reachable = provider_config.ollama_reachable(
        "http://localhost:11434", client=_mock_client(up)
    )
    assert reachable["reachable"] is True

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    offline = provider_config.ollama_reachable(
        "http://localhost:11434", client=_mock_client(down)
    )
    assert offline["reachable"] is False


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


def test_providers_catalog_groups_by_provider_with_models(
    tmp_path, monkeypatch
) -> None:
    # deterministic ollama badge regardless of a real local daemon
    monkeypatch.setattr(
        provider_config,
        "ollama_reachable",
        lambda *a, **k: {"reachable": False, "detail": "not running"},
    )
    client = TestClient(create_app(tmp_path / "ws"))
    resp = client.get("/api/providers")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["tier"] == "local"

    by_id = {p["id"]: p for p in payload["providers"]}
    assert {"anthropic", "openai", "gemini"} <= set(by_id)
    assert all(provider["kind"] != "local_http" for provider in payload["providers"])

    anthropic = by_id["anthropic"]
    assert anthropic["kind"] == "platform_api"
    model_ids = [m["id"] for m in anthropic["models"]]
    assert any(mid.startswith("anthropic/claude") for mid in model_ids)
    # prices come from the pricing table; unknowns are null, never fabricated
    for model in anthropic["models"]:
        assert "price" in model  # present (may be null)

    openrouter = by_id["openrouter"]
    assert openrouter["models"] == [
        {
            "id": "openrouter/qwen/qwen3-8b",
            "label": "Qwen 3 8B — open weights; requests go to OpenRouter",
            "price": {"input": 0.117, "output": 0.455},
            "local": False,
        },
        {
            "id": "openrouter/qwen/qwen3-vl-8b-instruct",
            "label": "Qwen3-VL 8B — open vision",
            "price": {"input": 0.117, "output": 0.455},
            "local": False,
        },
        {
            "id": "openrouter/qwen/qwen3-vl-32b-instruct",
            "label": "Qwen3-VL 32B — open vision",
            "price": {"input": 0.104, "output": 0.416},
            "local": False,
        },
        {
            "id": "openrouter/z-ai/glm-5.3-flash",
            "label": "GLM 5.3 Flash — open vision",
            "price": {"input": 0.075, "output": 0.25},
            "local": False,
        },
        {
            "id": "openrouter/moonshotai/kimi-k3",
            "label": "Kimi K3 — open vision",
            "price": {"input": 3.0, "output": 15.0},
            "local": False,
        },
        {
            "id": "openrouter/xiaomi/mimo-v2.5",
            "label": "MiMo V2.5 — open vision",
            "price": {"input": 0.14, "output": 0.28},
            "local": False,
        },
        {
            "id": "openrouter/minimax/minimax-m3",
            "label": "MiniMax M3 — hosted multimodal OCR via OpenRouter",
            "price": {"input": 0.3, "output": 1.2},
            "local": False,
        },
    ]


def test_providers_catalog_live_listing_prices_installed_models_too(
    tmp_path, monkeypatch
) -> None:
    """Every installed local model is known zero on the live catalog path."""
    monkeypatch.setattr(
        provider_config,
        "ollama_reachable",
        lambda *a, **k: {
            "reachable": True,
            "status": 200,
            "models": ["qwen3:8b", "some-other-unpriced-model"],
            "detail": None,
            "protocol": "ollama_native",
        },
    )
    root = tmp_path / "ws"
    endpoint = provider_config.create_local_endpoint(
        root, name="Ollama workstation", url="http://127.0.0.1:11434"
    )
    client = TestClient(create_app(root))
    resp = client.get("/api/providers")
    assert resp.status_code == 200, resp.text
    ollama = next(
        provider
        for provider in resp.json()["providers"]
        if provider["kind"] == "local_http"
    )
    assert ollama["reachable"] is True
    prices = {m["id"]: m["price"] for m in ollama["models"]}
    assert prices == {
        f"ollama/@{endpoint.endpoint_id}/qwen3:8b": {"input": 0.0, "output": 0.0},
        f"ollama/@{endpoint.endpoint_id}/some-other-unpriced-model": {
            "input": 0.0,
            "output": 0.0,
        },
    }


def test_set_key_route_redacts_and_router_uses_it(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        provider_config, "ollama_reachable", lambda *a, **k: {"reachable": False}
    )
    monkeypatch.setattr(
        provider_config,
        "probe_provider",
        lambda provider, key, **k: {
            "provider": provider,
            "ok": True,
            "reachable": True,
            "status": 200,
            "detail": None,
        },
    )
    app = create_app(tmp_path / "ws")
    client = TestClient(app)

    validated = client.post("/api/providers/openai/validate", json={"key": SECRET})
    assert validated.status_code == 200, validated.text
    saved = client.put(
        "/api/providers/keys/openai",
        json={"key": SECRET, "validation_token": validated.json()["validation_token"]},
    )
    assert saved.status_code == 200, saved.text
    assert SECRET not in saved.text
    openai = {p["id"]: p for p in saved.json()["providers"]}["openai"]
    assert openai["configured"] is True
    assert openai["hint"] == "...1234"

    # a fresh project's router now resolves the UI-entered key
    pid = client.post("/api/projects", json={"name": "keys"}).json()["id"]
    project = app.state.workspace.get(pid)
    assert (
        app.state.workspace.router_for(project).adapter_for("openai").api_key == SECRET
    )

    deleted = client.delete("/api/providers/keys/openai")
    assert deleted.status_code == 200, deleted.text
    openai = {p["id"]: p for p in deleted.json()["providers"]}["openai"]
    assert openai["configured"] is False


def test_set_key_rejects_unknown_provider(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    resp = client.put("/api/providers/keys/bogus", json={"key": "sk-nope"})
    assert resp.status_code == 400
    assert "sk-nope" not in resp.text


def test_validate_route_never_echoes_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_config,
        "probe_provider",
        lambda provider, key, **k: {"ok": True, "reachable": True, "status": 200},
    )
    client = TestClient(create_app(tmp_path / "ws"))
    resp = client.post(
        "/api/providers/openai/validate", json={"key": "sk-should-not-echo"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert "sk-should-not-echo" not in resp.text


# ---------------------------------------------------------------------------
# Local-model token no-echo pins: catalog, diagnose, and pull DTOs must never
# contain a configured local-model token VALUE, with a planted canary proving
# it.
# ---------------------------------------------------------------------------

CANARY_TOKEN = "sk-canary-local-model-token-must-never-echo"  # noqa: S105


def _configure_canary_tokens(root, monkeypatch):
    monkeypatch.setattr(
        provider_config,
        "ollama_reachable",
        lambda *a, **k: {
            "reachable": True,
            "status": 200,
            "models": [],
            "detail": None,
            "protocol": "ollama_native",
            "auth_status": "ok",
            "token_configured": True,
        },
    )
    return provider_config.create_local_endpoint(
        root,
        name="Authenticated Ollama",
        url="http://127.0.0.1:11434",
        inference_token=CANARY_TOKEN,
        provisioning_token=CANARY_TOKEN,
        edge_auth=True,
        pull_enabled=True,
    )


def test_catalog_payload_never_echoes_the_configured_token(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "ws"
    endpoint = _configure_canary_tokens(root, monkeypatch)
    client = TestClient(create_app(root))

    resp = client.get("/api/providers")
    assert resp.status_code == 200, resp.text
    assert CANARY_TOKEN not in resp.text
    ollama = next(
        provider
        for provider in resp.json()["providers"]
        if provider.get("endpoint_id") == endpoint.endpoint_id
    )
    assert ollama["token_configured"] is True


def test_diagnose_payload_never_echoes_the_configured_token(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "ws"
    _configure_canary_tokens(root, monkeypatch)
    client = TestClient(create_app(root))

    resp = client.get("/api/diagnose")
    assert resp.status_code == 200, resp.text
    assert CANARY_TOKEN not in resp.text


def test_pull_dto_never_carries_a_token_field(tmp_path, monkeypatch) -> None:
    """The route-level pull DTO (``_pull_dto``) has no token-bearing field at
    all -- a configured local-model token must never appear in the enqueue
    response or the pull listing/detail DTOs. (The actual worker-boundary
    redaction of upstream-echoed tokens in ``error_message`` is pinned
    end-to-end, with a real mocked upstream, by
    tests/test_model_pull_handler.py's canary tests.)"""
    root = tmp_path / "ws"
    endpoint = _configure_canary_tokens(root, monkeypatch)
    client = TestClient(create_app(root))

    resp = client.post(
        "/api/providers/models/pull",
        json={"ref": f"ollama/@{endpoint.endpoint_id}/smollm:135m"},
    )
    assert resp.status_code == 202, resp.text
    assert CANARY_TOKEN not in resp.text
    pull_id = resp.json()["pull"]["id"]

    listing = client.get("/api/providers/models/pulls")
    assert listing.status_code == 200, listing.text
    assert CANARY_TOKEN not in listing.text

    single = client.get(f"/api/providers/models/pulls/{pull_id}")
    assert single.status_code == 200, single.text
    assert CANARY_TOKEN not in single.text
