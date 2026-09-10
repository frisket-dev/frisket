"""product-friction-provider-live-mode-v1: provider keys validate before save.

Direct save routes must not persist a new/replacement provider key until the
same provider/key pair has passed the validation probe. Failed validation stores
nothing, and existing configured keys can be probed without exposing values.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.server import provider_config
from frisket.server.app import create_app


def _probe_stub(monkeypatch):
    seen: list[tuple[str, str]] = []

    def fake_probe(provider: str, key: str, **_kwargs):
        seen.append((provider, key))
        ok = key.endswith("-valid")
        return {
            "provider": provider,
            "ok": ok,
            "reachable": True,
            "status": 200 if ok else 401,
            "detail": None if ok else "HTTP 401: key was rejected",
        }

    monkeypatch.setattr(provider_config, "probe_provider", fake_probe)
    return seen


def test_local_provider_key_save_requires_successful_validation(
    tmp_path, monkeypatch
) -> None:
    _probe_stub(monkeypatch)
    client = TestClient(create_app(tmp_path / "ws"))

    direct = client.put("/api/providers/keys/openai", json={"key": "sk-direct-valid"})
    assert direct.status_code == 400
    assert provider_config.load_local_provider_keys(tmp_path / "ws") == {}
    assert "sk-direct-valid" not in direct.text

    rejected = client.post("/api/providers/openai/validate", json={"key": "sk-bad"})
    assert rejected.status_code == 200
    assert rejected.json()["ok"] is False
    assert "validation_token" not in rejected.json()
    assert provider_config.load_local_provider_keys(tmp_path / "ws") == {}

    validated = client.post(
        "/api/providers/openai/validate", json={"key": "sk-good-valid"}
    )
    token = validated.json()["validation_token"]
    saved = client.put(
        "/api/providers/keys/openai",
        json={"key": "sk-good-valid", "validation_token": token},
    )
    assert saved.status_code == 200, saved.text
    assert (
        provider_config.load_local_provider_keys(tmp_path / "ws")["openai"]
        == "sk-good-valid"
    )
    assert "sk-good-valid" not in saved.text


def test_project_provider_key_save_requires_validation_and_existing_key_can_be_tested(
    tmp_path, monkeypatch
) -> None:
    seen = _probe_stub(monkeypatch)
    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post("/api/projects", json={"name": "Provider Keys"}).json()["id"]

    direct = client.post(
        f"/api/projects/{pid}/provider-keys",
        json={"provider": "openai", "key": "sk-project-valid"},
    )
    assert direct.status_code == 400
    project = client.app.state.workspace.get(pid)
    assert project.provider_key_catalog_rows() == {}

    validated = client.post(
        f"/api/projects/{pid}/provider-keys/validate",
        json={"provider": "openai", "key": "sk-project-valid"},
    )
    token = validated.json()["validation_token"]
    saved = client.post(
        f"/api/projects/{pid}/provider-keys",
        json={
            "provider": "openai",
            "key": "sk-project-valid",
            "validation_token": token,
        },
    )
    assert saved.status_code == 200, saved.text
    assert "sk-project-valid" not in saved.text

    existing = client.post(
        f"/api/projects/{pid}/provider-keys/validate",
        json={"provider": "openai"},
    )
    assert existing.status_code == 200, existing.text
    assert existing.json()["ok"] is True
    assert "sk-project-valid" not in existing.text
    assert seen[-1] == ("openai", "sk-project-valid")
