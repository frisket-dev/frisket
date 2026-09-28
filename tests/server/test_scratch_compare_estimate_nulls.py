"""Scratch estimates preserve unknown prices through the HTTP contract."""

import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.executor import ExecutorDeps
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.server.app import create_app
from frisket.server.services import scratch_action_preview
from tests.engine.test_ocr_read import _png


@pytest.mark.parametrize("kind", ["ocr", "transcribe"])
@pytest.mark.parametrize("cost", [None, 0.0, 0.25], ids=["unknown", "zero", "known"])
def test_scratch_compare_estimate_preserves_nullable_costs(
    tmp_path, monkeypatch, kind, cost
):
    # Control the provider quote, not the rating, consent or HTTP serializers.
    original = scratch_action_preview.estimate_from_cost_basis

    def quote(*args, **kwargs):
        return {
            **original(*args, **kwargs),
            "cost": cost,
            "cost_source": "unknown" if cost is None else "pricing_data",
        }

    monkeypatch.setattr(scratch_action_preview, "estimate_from_cost_basis", quote)
    monkeypatch.setattr(
        "frisket.server.services.scratch_transcribe.probe_for_ingest",
        lambda *args, **kwargs: {"kind": "audio", "duration_seconds": 15.0},
    )
    router = ModelRouter(
        keys={"gemini": "test-key", "openai": "test-key"},
        cache=None,
        cache_mode="off",
    )
    app = create_app(
        tmp_path / "workspace",
        router=router,
        executor_deps_factory=lambda _pid, _request: ExecutorDeps(
            consent_coverage=ConsentCoverage("test:scratch", Decimal("2"))
        ),
    )
    if kind == "ocr":
        engine = "gemini/gemini-3.5-flash-lite"
        upload = ("scan.png", _png(), "image/png")
    else:
        engine = "openai/whisper-1"
        upload = ("sample.wav", b"RIFF0000WAVEfmt scratch transcript", "audio/wav")
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "Compare"}).json()["id"]
        endpoint = f"/api/projects/{pid}/{kind}/compare-scratch"
        multipart = {
            "files": {"file": upload},
            "data": {"payload": json.dumps({"engine": engine})},
        }
        response = client.post(f"{endpoint}/estimate", **multipart)
        assert response.status_code == 200, response.text
        estimate = response.json()["estimate"]
        assert estimate["cost"] == cost
        assert estimate["billed_cost"] == (None if cost is None else int(cost * 1e6))
        if cost is None:
            assert estimate["requires_confirmation"] is True
            refusal = client.post(endpoint, **multipart)
            assert refusal.status_code == 402, refusal.text
            details = refusal.json()["error"]["details"]
            assert details["estimate"]["cost"] is None
            assert details["estimate"]["billed_cost"] is None
            assert details["promise_set_hash"] == estimate["promise_set_hash"]
