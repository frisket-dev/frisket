"""The AI providers picker offers only what the project's Allowed services
lets Run use."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.server.provider_config import (
    create_local_endpoint,
    save_local_provider_key,
)
from frisket.service_access import (
    AllowedServices,
    ApprovedEndpoint,
    ApprovedEndpoints,
    write_approved_endpoints,
)

LOOPBACK_ORIGIN = "http://127.0.0.1:11434"
REMOTE_ORIGIN = "http://ollama.lan.example.test:11434"


def _offered(entries: list[dict[str, Any]]) -> set[str]:
    offered: set[str] = set()
    for entry in entries:
        if entry.get("kind") == "local_http":
            offered.add(str(entry.get("url") or entry.get("origin")))
        elif entry.get("models"):
            offered.add(str(entry["id"]))
    return offered


@pytest.mark.parametrize(
    ("setting", "approved", "expected"),
    [
        ("this_machine_only", (), {LOOPBACK_ORIGIN}),
        ("approved_services", (), {LOOPBACK_ORIGIN, REMOTE_ORIGIN}),
        (
            "approved_services",
            ("api.openai.com",),
            {LOOPBACK_ORIGIN, REMOTE_ORIGIN, "openai"},
        ),
    ],
)
def test_picker_offers_what_the_setting_allows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: AllowedServices,
    approved: tuple[str, ...],
    expected: set[str],
) -> None:
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL"):
        monkeypatch.delenv(name, raising=False)

    def reachable(_origin: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "reachable": True,
            "protocol": "openai_compatible",
            "models": ["llama3"],
            "auth_status": "ok",
        }

    monkeypatch.setattr("frisket.server.provider_config.ollama_reachable", reachable)
    root = tmp_path / "ws"
    root.mkdir()
    for name, origin in (("Here", LOOPBACK_ORIGIN), ("Ollama", REMOTE_ORIGIN)):
        create_local_endpoint(root, name=name, url=origin)
    save_local_provider_key(root, "openai", "sk-test-not-real")
    write_approved_endpoints(
        root,
        ApprovedEndpoints(endpoints=tuple(ApprovedEndpoint(host=h) for h in approved)),
    )
    client = TestClient(create_app(root))
    pid = client.post(
        "/api/projects", json={"name": "Picker", "allowed_services": setting}
    ).json()["id"]

    listed = client.get("/api/providers", params={"project_id": pid})

    assert listed.status_code == 200, listed.text
    assert _offered(listed.json()["providers"]) == expected
