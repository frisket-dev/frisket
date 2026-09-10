"""Runtime and OpenAPI coverage for the translate comparison HTTP boundary."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.contracts.http.translate_comparison import TranslateComparisonResponse
from frisket.server.routes.previews import register_preview_routes
from frisket.server.services.previews import PreviewRequestError, PreviewService


class _TranslateComparisonService:
    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    async def translate_compare_scratch(
        self, project_id: str, *, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.payload = payload
        if payload["text"] == "refuse":
            raise PreviewRequestError(
                code="invalid_request",
                message="paste some text to translate",
                field="text",
            )
        return {
            "schema_version": "frisket.translate_compare_preview.v1",
            "source": {"scratch": True, "text_length": len(payload["text"])},
            "target_language": payload["target_language"],
            "engines": payload["engines"],
            "results": [
                {
                    "engine": "opus_mt",
                    "translation": "Hola",
                    "detected_language": "en",
                    "runtime_ms": 12,
                    "errors": [],
                }
            ],
            "warnings": [{"code": "sampled", "extension": [1, True]}],
            "errors": [
                {
                    "code": "translate_preview_engine_failed",
                    "engine": "hy_mt2",
                    "message": "weights unavailable",
                    "retry": {"after_ms": 100},
                }
            ],
        }


def _client() -> tuple[TestClient, _TranslateComparisonService]:
    app = FastAPI()
    service = _TranslateComparisonService()
    register_preview_routes(app, service=service)  # type: ignore[arg-type]
    return TestClient(app), service


class _ScratchWorkspace:
    def get(self, project_id: str) -> None:
        del project_id
        return None


def _normalizing_client() -> TestClient:
    app = FastAPI()
    register_preview_routes(
        app,
        service=PreviewService(_ScratchWorkspace()),  # type: ignore[arg-type]
    )
    return TestClient(app)


def test_translate_comparison_preserves_json_defaults_extensions_and_bare_400() -> None:
    client, service = _client()

    response = client.post(
        "/api/projects/project-1/translate/compare-scratch",
        json={
            "engines": ["opus_mt"],
            "text": "Hello",
            "ignored_compatibility_key": True,
        },
    )

    assert response.status_code == 200, response.text
    assert service.payload == {
        "engines": ["opus_mt"],
        "text": "Hello",
        "target_language": "English",
        "language": None,
    }
    assert response.json()["results"][0]["translation"] == "Hola"
    assert response.json()["warnings"] == [{"code": "sampled", "extension": [1, True]}]
    assert response.json()["errors"][0]["retry"] == {"after_ms": 100}
    assert response.content == (
        b'{"schema_version":"frisket.translate_compare_preview.v1",'
        b'"source":{"scratch":true,"text_length":5},'
        b'"target_language":"English","engines":["opus_mt"],'
        b'"results":[{"engine":"opus_mt","translation":"Hola",'
        b'"detected_language":"en","runtime_ms":12,"errors":[]}],'
        b'"warnings":[{"code":"sampled","extension":[1,true]}],'
        b'"errors":[{"code":"translate_preview_engine_failed",'
        b'"engine":"hy_mt2","message":"weights unavailable",'
        b'"retry":{"after_ms":100}}]}'
    )

    refused = client.post(
        "/api/projects/project-1/translate/compare-scratch",
        json={"engines": ["opus_mt"], "text": "refuse"},
    )
    assert refused.status_code == 400
    assert refused.json() == {
        "schema_version": "frisket.action_error.v1",
        "code": "invalid_request",
        "message": "paste some text to translate",
        "action_kind": None,
        "field": "text",
        "details": {},
        "needs_confirmation": False,
    }
    assert refused.content == (
        b'{"schema_version":"frisket.action_error.v1",'
        b'"code":"invalid_request",'
        b'"message":"paste some text to translate",'
        b'"action_kind":null,"field":"text",'
        b'"details":{},"needs_confirmation":false}'
    )


def test_translate_comparison_contract_closes_outer_source_and_engine_results() -> None:
    payload = {
        "schema_version": "frisket.translate_compare_preview.v1",
        "source": {"scratch": True, "text_length": 5},
        "target_language": "English",
        "engines": ["opus_mt"],
        "results": [
            {
                "engine": "opus_mt",
                "translation": "Hola",
                "detected_language": "en",
                "runtime_ms": 1,
                "errors": [],
            }
        ],
        "warnings": [{"future_warning": ["safe", 1]}],
        "errors": [{"future_error": {"safe": True}}],
    }
    assert TranslateComparisonResponse.model_validate(payload).model_dump() == payload

    with pytest.raises(ValidationError):
        TranslateComparisonResponse.model_validate({**payload, "outer_extra": True})
    with pytest.raises(ValidationError):
        TranslateComparisonResponse.model_validate(
            {**payload, "source": {"scratch": True, "text_length": 5, "extra": 1}}
        )
    with pytest.raises(ValidationError):
        TranslateComparisonResponse.model_validate(
            {
                **payload,
                "results": [{**payload["results"][0], "future_engine_field": True}],
            }
        )
    for field in ("warnings", "errors"):
        with pytest.raises(ValidationError):
            TranslateComparisonResponse.model_validate({**payload, field: [None]})


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({}, "invalid_request"),
        ({"engines": "opus_mt", "text": "hello"}, "invalid_request"),
        (
            {
                "engines": [],
                "text": 7,
                "target_language": None,
                "language": "en",
            },
            "invalid_request",
        ),
        (
            {
                "engines": ["deepl"],
                "text": 7,
                "target_language": None,
                "language": "en",
            },
            "billable_engine_requires_run",
        ),
    ],
)
def test_translate_comparison_compatibility_objects_reach_service_normalization(
    payload: dict[str, Any], code: str
) -> None:
    response = _normalizing_client().post(
        "/api/projects/project-1/translate/compare-scratch", json=payload
    )

    assert response.status_code == 400, response.text
    assert response.json()["schema_version"] == "frisket.action_error.v1"
    assert response.json()["code"] == code
    assert "detail" not in response.json()


@pytest.mark.parametrize("payload", [[], None, "not an object"])
def test_translate_comparison_non_object_json_remains_fastapi_422(payload: Any) -> None:
    client, service = _client()

    response = client.post(
        "/api/projects/project-1/translate/compare-scratch", json=payload
    )

    assert response.status_code == 422
    assert service.payload is None


def test_translate_comparison_openapi_declares_json_contract_and_existing_errors() -> (
    None
):
    client, _service = _client()
    operation = client.app.openapi()["paths"][
        "/api/projects/{pid}/translate/compare-scratch"
    ]["post"]

    document = client.app.openapi()
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert {item["$ref"] for item in request_schema["anyOf"]} == {
        "#/components/schemas/TranslateComparisonRequest",
        "#/components/schemas/TranslateComparisonCompatibilityRequest",
    }
    assert document["components"]["schemas"]["TranslateComparisonRequest"][
        "required"
    ] == ["engines", "text"]
    compatibility_schema = document["components"]["schemas"][
        "TranslateComparisonCompatibilityRequest"
    ]
    assert compatibility_schema["type"] == "object"
    assert "additionalProperties" in compatibility_schema
    assert set(operation["responses"]) >= {
        "200",
        "400",
        "401",
        "403",
        "404",
        "409",
        "422",
        "500",
    }
