"""Scratch estimates preserve unknown prices through the HTTP contract."""

import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.ai.models.metadata import ModelCallMeta
from frisket.engine.executor import ExecutorDeps
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.server.app import create_app
from frisket.server.services import scratch_action_preview
from tests.engine.test_ocr_read import _png
from tests.deterministic_time import controlled_time


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


@pytest.mark.parametrize("limit", ["2", "0"])
def test_ocr_scratch_uses_real_estimate_for_preapproval(tmp_path, monkeypatch, limit):
    engine = "gemini/gemini-3.5-flash-lite"

    async def recognize(self, engine, pages, ctx, *, usage, **kwargs):
        usage["calls"] += 1
        usage["model_calls"] = [
            ModelCallMeta.provider_call(
                capability="ocr",
                engine=engine,
                provider="gemini",
                provider_kind="chat_api",
                model_ids=["gemini-3.5-flash-lite"],
                credential_source="local",
                provider_reported_cost_usd=None,
                provider_cost_usd=0.03,
                units={"pages": 1},
                cost_source="pricing_data",
                warnings=[],
                duration_ms=5,
            ).as_dict()
        ]
        return [{"text": "Readable page", "blocks": []}]

    monkeypatch.setattr(
        "frisket.ops.ocr_engines.OcrEngines.run_engine_on_pages", recognize
    )
    router = ModelRouter(keys={"gemini": "test-key"}, cache=None, cache_mode="off")
    app = create_app(
        tmp_path / "workspace",
        router=router,
        executor_deps_factory=lambda _pid, _request: ExecutorDeps(
            consent_coverage=ConsentCoverage("test:scratch", Decimal(limit))
        ),
    )
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "Compare"}).json()["id"]
        endpoint = f"/api/projects/{pid}/ocr/compare-scratch"
        multipart = {
            "files": {"file": ("scan.png", _png(), "image/png")},
            "data": {"payload": json.dumps({"engine": engine})},
        }
        quote = client.post(f"{endpoint}/estimate", **multipart)
        assert quote.status_code == 200, quote.text
        estimate = quote.json()["estimate"]
        assert estimate["cost"] == pytest.approx(0.0114688)
        assert estimate["requires_confirmation"] is (limit == "0")
        started = client.post(endpoint, **multipart)
        if limit == "0":
            assert started.status_code == 402, started.text
            multipart["data"]["payload"] = json.dumps(
                {"engine": engine, "confirmation": estimate["promise_set_hash"]}
            )
            started = client.post(endpoint, **multipart)
        assert started.status_code == 202, started.text
        preview_id = started.json()["preview_id"]
        result = None

        def finished():
            nonlocal result
            response = client.get(
                f"/api/projects/{pid}/actions/v1/preview/{preview_id}"
            )
            assert response.status_code == 200, response.text
            result = response.json()
            return result["status"] != "running"

        with controlled_time(timeout=10) as clock:
            clock.wait_until(finished, message="OCR scratch preview did not finish")
        assert result["status"] == "done", result
        assert result["accounting"]["cost_actual"] == 0.03
