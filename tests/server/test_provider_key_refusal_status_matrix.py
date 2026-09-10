"""Cross-protocol status contract for provider-key refusals."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from frisket.contracts.action import ActionResult
from frisket.contracts.http.action_estimate_validation import ActionRunRequest
from frisket.contracts.http.copilot import CopilotRequest
from frisket.engine.runner import (
    MissingProviderKey,
    ProviderKeyRefusal,
    ProviderSpendCapExceeded,
    ProviderSpendCapUnenforceable,
)
from frisket.execution.provider import ExecutionCompositionContext
from frisket.server.action_enqueue import provider_key_refusal_error
from frisket.server.routes.action_runs import register_action_run_routes
from frisket.server.routes.project_research import register_project_copilot_routes
from frisket.server.services import action_preview_runs
from frisket.server.services.action_preview_runs import ActionPreviewRunService
from frisket.server.services.action_runs import (
    ActionRunResponse,
    v1_action_result_http_status,
)


@dataclass(frozen=True)
class _RefusalCase:
    refusal: ProviderKeyRefusal
    code: str
    details: dict[str, Any]


class _RunService:
    def __init__(self, refusal: ProviderKeyRefusal) -> None:
        self.refusal = refusal

    def run_action(
        self, project_id: str, body: dict[str, Any], **_: Any
    ) -> ActionRunResponse:
        error = provider_key_refusal_error(body["kind"], self.refusal)
        result = ActionResult(
            action={"kind": body["kind"], "action_id": "action"},
            status="failed",
            project_id=project_id,
            errors=[error],
        )
        return ActionRunResponse(
            status_code=v1_action_result_http_status(result),
            payload=result.model_dump(mode="json"),
        )


class _RefusingRunner:
    def __init__(self, refusal: ProviderKeyRefusal) -> None:
        self.refusal = refusal

    def prepare_preview(self, *_: object, **__: object) -> object:
        raise self.refusal


class _PreviewPlan:
    action_kind = "map.prompt"
    runner_spec: dict[str, Any] = {}

    def __init__(self, refusal: ProviderKeyRefusal) -> None:
        self.refusal = refusal

    def make_runner(self) -> _RefusingRunner:
        return _RefusingRunner(self.refusal)


class _PreviewWorkspace:
    edition = "solo"
    executor_deps_factory = None

    def get(self, _: str) -> object:
        return object()

    def action_execution_router_for(self, _: object) -> object:
        return object()

    def execution_composition_for(self, _: object, __: object, ___: object) -> object:
        return object()

    @staticmethod
    def edition_execution_composition_context_for(
        _request_context: object = None,
    ) -> ExecutionCompositionContext:
        return ExecutionCompositionContext.direct()


class _CopilotService:
    def __init__(self, refusal: ProviderKeyRefusal) -> None:
        self.refusal = refusal

    async def chat(self, _: str, __: dict[str, Any]) -> dict[str, Any]:
        raise self.refusal


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
        }
    )


def _endpoint(app: FastAPI, path: str):
    return next(
        route.endpoint for route in app.routes if getattr(route, "path", None) == path
    )


def _run_seat(refusal: ProviderKeyRefusal) -> tuple[int, dict[str, Any]]:
    path = "/api/projects/{pid}/actions/v1/run"
    app = FastAPI()
    register_action_run_routes(app, service=_RunService(refusal))  # type: ignore[arg-type]
    response = _endpoint(app, path)(
        _request(path),
        "project",
        ActionRunRequest.model_validate({"kind": "map.prompt"}),
    )
    return response.status_code, json.loads(response.body)["errors"][0]


def _preview_seat(
    monkeypatch: pytest.MonkeyPatch, refusal: ProviderKeyRefusal
) -> tuple[int, dict[str, Any]]:
    monkeypatch.setattr(
        action_preview_runs,
        "resolve_map_preview",
        lambda *_args, **_kwargs: _PreviewPlan(refusal),
    )
    response = ActionPreviewRunService(_PreviewWorkspace()).start_preview(  # type: ignore[arg-type]
        "project", {"kind": "map.prompt"}
    )
    return response.status_code, response.payload["error"]


def _copilot_seat(refusal: ProviderKeyRefusal) -> tuple[int, dict[str, Any]]:
    path = "/api/projects/{pid}/copilot"
    app = FastAPI()
    register_project_copilot_routes(  # type: ignore[arg-type]
        app, service=_CopilotService(refusal)
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            _endpoint(app, path)(
                _request(path),
                "project",
                CopilotRequest(messages=[{"role": "user", "content": "help"}]),
            )
        )
    return caught.value.status_code, caught.value.detail


def test_provider_key_refusal_status_and_value_fidelity_across_http_seats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = (
        _RefusalCase(
            MissingProviderKey("anthropic"),
            "missing_provider_key",
            {"provider": "anthropic", "retryable": True, "resumable": True},
        ),
        _RefusalCase(
            ProviderSpendCapExceeded(
                provider="anthropic",
                cap_micro=1_500_000,
                spent_micro=2_250_000,
                unmetered_calls=2,
            ),
            "provider_spend_cap_exceeded",
            {
                "provider": "anthropic",
                "cap_usd": 1.5,
                "spent_usd": 2.25,
                "unmetered_calls": 2,
                "setting": "spend_cap_usd",
                "retryable": False,
                "resumable": False,
            },
        ),
        _RefusalCase(
            ProviderSpendCapUnenforceable(
                provider="anthropic", cap_micro=1_500_000, unmetered_calls=3
            ),
            "provider_spend_cap_unenforceable",
            {
                "provider": "anthropic",
                "cap_usd": 1.5,
                "unmetered_calls": 3,
                "setting": "spend_cap_usd",
                "retryable": False,
                "resumable": False,
            },
        ),
    )
    seats = (
        ("run", 400, lambda refusal: _run_seat(refusal)),
        ("preview", 400, lambda refusal: _preview_seat(monkeypatch, refusal)),
        ("copilot", 409, lambda refusal: _copilot_seat(refusal)),
    )

    for case in cases:
        for seat, expected_status, invoke in seats:
            status, error = invoke(case.refusal)
            assert status == expected_status, (case.code, seat)
            assert error["code"] == case.code, (case.code, seat)
            assert error["details"] == case.details, (case.code, seat)
