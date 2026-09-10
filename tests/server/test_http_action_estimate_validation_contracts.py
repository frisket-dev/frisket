"""Runtime and OpenAPI coverage for action-form preflight HTTP contracts."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.contracts.http.action_estimate_validation import ActionEstimate
from frisket.server.route_errors import RouteError, register_route_error_handler
from frisket.server.routes.action_preview_support import (
    register_action_param_validation_routes,
    register_action_preview_routes,
)


class _EstimateService:
    def __init__(self) -> None:
        self.action: dict[str, Any] | None = None

    def estimate(
        self,
        project_id: str,
        action: dict[str, Any],
        *,
        request_context: Any,
    ) -> dict[str, Any]:
        assert request_context is not None
        self.action = action
        if action.get("kind") == "fail":
            raise RouteError(400, "service-owned estimate refusal")
        return {
            "schema_version": "frisket.action_estimate_result.v1",
            "action": {"kind": str(action.get("kind", ""))},
            "project_id": project_id,
            "estimate": {
                "rows": 3,
                "cost": 0.125,
                "pricing_key": "openai.gpt-4o-mini.token",
                "engine": "openai/gpt-4o-mini",
                "remote_capability": "model:complete",
                "requires_confirmation": True,
                "avg_input_tokens": 42,
                "audio_seconds": 10.5,
                "billing_label": "Frisket credits",
                "venue_label": "Hosted provider",
                "cost_source": "estimated",
                "warning": "sampled quote",
                "billed_cost": 250_000,
                "policy_id": "tenant-v1",
            },
        }


class _ValidationService:
    def validate_params(
        self, project_id: str, action: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "schema_version": "frisket.action_param_validation_result.v1",
            "action": {"kind": str(action.get("kind", ""))},
            "project_id": project_id,
            "diagnostics": {
                "pattern": {
                    "ok": False,
                    "message": "unbalanced parenthesis",
                    "position": 6,
                },
                "template": {"ok": True},
            },
        }


def test_action_estimate_contract_refuses_deleted_money_aliases() -> None:
    canonical = {
        "rows": 3,
        "cost": 0.125,
        "cost_source": "pricing_data",
        "billed_cost": 125_000,
        "policy_id": "tenant-v1",
        "pricing_key": "provider.capability.unit",
        "engine": "provider/model",
    }
    assert ActionEstimate.model_validate(canonical).pricing_key == (
        "provider.capability.unit"
    )
    for alias, value in {
        "provider_cost_usd": 0.125,
        "unit_price_usd": 0.01,
        "units": {"requests": 1},
        "pages": 1,
        "llm": True,
    }.items():
        with pytest.raises(ValidationError):
            ActionEstimate.model_validate({**canonical, alias: value})


def test_action_estimate_and_validation_contracts_preserve_live_shapes() -> None:
    app = FastAPI()
    register_route_error_handler(app)
    estimate_service = _EstimateService()
    register_action_preview_routes(app, service=estimate_service)  # type: ignore[arg-type]
    register_action_param_validation_routes(
        app,
        service=_ValidationService(),  # type: ignore[arg-type]
    )
    client = TestClient(app)

    estimate = client.post(
        "/api/projects/project-1/actions/v1/estimate",
        json={
            "action": {"kind": "map.classify", "params": {"model": "x"}},
            "ignored_wrapper_key": True,
        },
    )
    assert estimate.status_code == 200, estimate.text
    estimate_body = estimate.json()
    assert estimate_service.action == {
        "kind": "map.classify",
        "params": {"model": "x"},
    }
    assert "cost" not in estimate_body
    assert estimate_body["estimate"] == {
        "rows": 3,
        "cost": 0.125,
        "pricing_key": "openai.gpt-4o-mini.token",
        "engine": "openai/gpt-4o-mini",
        "remote_capability": "model:complete",
        "requires_confirmation": True,
        "avg_input_tokens": 42,
        "audio_seconds": 10.5,
        "billing_label": "Frisket credits",
        "venue_label": "Hosted provider",
        "cost_source": "estimated",
        "warning": "sampled quote",
        "billed_cost": 250_000,
        "policy_id": "tenant-v1",
    }

    validation = client.post(
        "/api/projects/project-1/actions/v1/validate-params",
        json={"action": {"kind": "map.regex_extract", "params": {}}},
    )
    assert validation.status_code == 200, validation.text
    assert validation.json()["diagnostics"] == {
        "pattern": {
            "ok": False,
            "message": "unbalanced parenthesis",
            "position": 6,
        },
        "template": {"ok": True},
    }

    refused = client.post(
        "/api/projects/project-1/actions/v1/estimate",
        json={"action": {"kind": "fail"}},
    )
    assert refused.status_code == 400
    assert refused.json() == {"detail": "service-owned estimate refusal"}

    document = app.openapi()
    for path in (
        "/api/projects/{pid}/actions/v1/estimate",
        "/api/projects/{pid}/actions/v1/validate-params",
    ):
        operation = document["paths"][path]["post"]
        assert operation["requestBody"]["required"] is True
        assert set(operation["responses"]) >= {
            "200",
            "400",
            "401",
            "403",
            "404",
            "422",
            "500",
        }
