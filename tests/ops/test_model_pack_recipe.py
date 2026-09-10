from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frisket.ops.base import OpContext
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.ner import NerOutput
from frisket.ops.ner_local import LocalNerExtractor
from frisket.server.app import create_app


class _Resp:
    status_code = 200
    text = "ok"

    def json(self):
        return {
            "results": [
                [
                    {
                        "text": "Ada Lovelace",
                        "label": "person",
                        "start": 0,
                        "end": 12,
                        "score": 0.98,
                    }
                ]
            ]
        }


class _Http:
    def __init__(self):
        self.calls = []

    async def post(self, url, **kwargs):  # noqa: ANN001
        self.calls.append((url, kwargs))
        return _Resp()


def test_ner_recipe_is_registered():
    declaration = ACTION_REGISTRY.get("map.ner")
    assert "engine" in declaration.definition.run.params_model.model_fields
    assert NerOutput.model_fields["entities"].annotation is not None


@pytest.mark.asyncio
async def test_ner_recipe_calls_model_pack_sidecar(monkeypatch):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
    http = _Http()

    out = await LocalNerExtractor("gliner", OpContext(http=http)).extract(
        "Ada Lovelace worked with Babbage.",
        labels=["person", "organization"],
        threshold=0.5,
    )

    assert out[0]["text"] == "Ada Lovelace"
    assert http.calls[0][0] == "http://models/ner"
    body = http.calls[0][1]["json"]
    assert body["texts"] == ["Ada Lovelace worked with Babbage."]
    assert body["labels"] == ["person", "organization"]


@pytest.mark.asyncio
async def test_gliner_raw_label_survives_double_canonicalization(monkeypatch):
    # execute() canonicalizes once, then the generic row pipeline
    # (row_execution.py) runs postprocess_value on the already-canonical
    # output. metadata.raw_label for a zero-shot label whose spelling changes
    # must survive that second pass.
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")

    class _ZeroShotResp:
        status_code = 200
        text = "ok"

        def json(self):
            return {
                "results": [
                    [
                        {
                            "text": "a@b.com",
                            "label": "email address",
                            "start": 0,
                            "end": 7,
                            "score": 0.9,
                        }
                    ]
                ]
            }

    class _ZeroShotHttp:
        async def post(self, url, **kwargs):  # noqa: ANN001
            return _ZeroShotResp()

    from frisket.actions.ner import normalize_ner_output

    executed = await LocalNerExtractor(
        "gliner", OpContext(http=_ZeroShotHttp())
    ).extract(
        "a@b.com",
        labels=["email address"],
        threshold=0.5,
    )
    stored = normalize_ner_output(executed).entities

    assert stored[0]["type"] == "email_address"
    assert stored[0]["metadata"] == {"raw_label": "email address"}


def test_ner_action_catalog_reports_sidecar_availability(monkeypatch, tmp_path):
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    client = TestClient(create_app(tmp_path / "workspace"))
    entries = {
        entry["kind"]: entry
        for entry in client.get("/api/actions/v1/catalog").json()["actions"]
    }

    engines = entries["map.ner"]["ui_hints"]["engines"]
    assert [e["id"] for e in engines] == ["spacy", "gliner", "llm"]

    gliner = next(e for e in engines if e["id"] == "gliner")
    assert gliner["tier"] == "sidecar"
    assert gliner["available"] is False
    assert "FRISKET_MODELS_URL" in gliner["error"]
