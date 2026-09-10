"""Embedding provider and model picker contract.

The helper (route-free, testable) projects embedding_capabilities into a picker
payload filtered by modality / source column type, with disabled reasons. The
server route wraps it with the per-project router. No UI.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app
from frisket.server.embedding_catalog import embedding_provider_catalog_payload


class _NoKeyRouter:
    """No remote providers configured (deterministic, independent of env keys)."""

    def providers(self):
        return []


def _payload(**kw):
    kw.setdefault("router", _NoKeyRouter())
    kw.setdefault("env", {})
    return embedding_provider_catalog_payload(**kw)


# --------------------------------------------------------------------------
# helper contract
# --------------------------------------------------------------------------


def test_helper_entry_shape():
    payload = _payload(local_available=True)
    required = {
        "provider_id",
        "provider_kind",
        "model_id",
        "label",
        "modalities",
        "dimensions",
        "distance_metrics",
        "local",
        "available",
        "error",
        "disabled_reason",
        "pricing",
        "privacy",
    }
    for p in payload["providers"]:
        assert required <= set(p), p.get("model_id")


def test_local_text_available_vs_disabled_with_reason():
    on = _payload(local_available=True)
    off = _payload(local_available=False)
    local_on = [p for p in on["providers"] if p["provider_kind"] == "local_process"]
    assert local_on and any(p["available"] for p in local_on)
    assert all(p["disabled_reason"] is None for p in local_on if p["available"])
    local_off = [p for p in off["providers"] if p["provider_kind"] == "local_process"]
    assert local_off and all(not p["available"] for p in local_off)
    assert all(p["disabled_reason"] for p in local_off)


def test_remote_disabled_without_key_with_privacy_note():
    payload = _payload(local_available=True)
    openai = [p for p in payload["providers"] if p["provider_id"] == "openai"]
    assert openai and all(not p["available"] for p in openai)
    assert all("OPENAI_API_KEY" in p["disabled_reason"] for p in openai)
    # remote engines are flagged non-local with a remote-egress privacy note
    assert all(not p["local"] for p in openai)
    assert all(p["privacy"].get("egress") == "remote" for p in openai)


def test_remote_available_with_key():
    class _WithOpenAI:
        def providers(self):
            return ["openai"]

    payload = embedding_provider_catalog_payload(
        router=_WithOpenAI(), env={}, local_available=True
    )
    openai = [p for p in payload["providers"] if p["provider_id"] == "openai"]
    assert openai and any(p["available"] for p in openai)
    assert any(p["disabled_reason"] is None for p in openai)


def test_known_remote_models_publish_their_input_token_rate():
    payload = _payload(local_available=True)
    by_model = {p["model_id"]: p for p in payload["providers"]}
    assert by_model["text-embedding-3-small"]["pricing"] == {
        "policy": "known_unit_price",
        "input_usd_per_million_tokens": 0.02,
        "source_url": "https://developers.openai.com/api/docs/models/text-embedding-3-small",
        "updated": "2026-08-28",
    }
    assert (
        by_model["gemini-embedding-001"]["pricing"]["input_usd_per_million_tokens"]
        == 0.15
    )


def test_unbuilt_sidecar_media_engines_are_not_picker_choices():
    payload = _payload(local_available=True, modality="image")
    assert payload["modality"] == "image"
    reserved = {
        "open_clip/ViT-B-32/laion2b_s34b_b79k",
        "audio-embedding",
        "video-embedding",
        "file-page-embedding",
    }
    assert reserved.isdisjoint(p["model_id"] for p in payload["providers"])


def test_modality_text_marks_media_disabled_not_hidden():
    # Contract change: incompatible models are SHOWN but disabled (modality_compatible
    # False + a reason), not filtered away, so the picker can grey them out.
    payload = _payload(local_available=True, modality="text")
    assert payload["modality"] == "text"
    assert any(
        p["modality_compatible"] and "text" in p["modalities"]
        for p in payload["providers"]
    )
    media_only = [p for p in payload["providers"] if "text" not in p["modalities"]]
    assert media_only  # present, not hidden
    for p in media_only:
        assert p["modality_compatible"] is False
        assert p["disabled_reason"]


def test_source_column_type_maps_to_modality():
    img = _payload(local_available=True, source_column_type="image")
    assert img["modality"] == "image" and img["source_column_type"] == "image"
    # image-capable models are compatible; text-only models present but disabled
    assert any(
        p["modality_compatible"] and "image" in p["modalities"]
        for p in img["providers"]
    )
    text_only = [p for p in img["providers"] if "image" not in p["modalities"]]
    assert text_only and all(not p["modality_compatible"] for p in text_only)
    link = _payload(local_available=True, source_column_type="link")
    assert link["modality"] == "text"  # text-ish column → text modality


# --------------------------------------------------------------------------
# server route
# --------------------------------------------------------------------------


def _client(tmp_path):
    return TestClient(create_app(tmp_path / "ws", router=ModelRouter(keys={})))


def test_route_returns_catalog(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "p"}).json()["id"]
    resp = client.get(
        f"/api/projects/{pid}/embeddings/v1/provider-catalog?modality=text"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema_version"] == "frisket.embedding_provider_catalog.v1"
    assert body["modality"] == "text"
    assert any("text" in p["modalities"] for p in body["providers"])
    openai = [p for p in body["providers"] if p["provider_id"] == "openai"]
    assert openai and all(not p["available"] for p in openai)
    assert all(p["disabled_reason"] for p in openai)


def test_route_missing_project_404(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/projects/nope/embeddings/v1/provider-catalog")
    assert resp.status_code == 404
