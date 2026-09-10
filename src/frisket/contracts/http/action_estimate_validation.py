"""HTTP contracts for the two action-form preflight posts.

The action payload deliberately stays an open JSON object. Its detailed
validation belongs to the service that owns the action kind, so making this
transport wrapper into a closed ActionSpec would change which service error a
caller receives.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel

from frisket.contracts.action import ActionError, ActionResult
from frisket.contracts.http.models import HttpError, WireModel


class ActionEstimateValidationRequest(BaseModel):
    """Tolerant shared ``{action: object}`` preflight request wrapper."""

    model_config = ConfigDict(extra="ignore", strict=True)

    action: dict[str, JsonValue]


class ActionRunRequest(RootModel[dict[str, JsonValue]]):
    """Tolerant v1 action envelope owned by the action-kind resolver.

    The HTTP route deliberately accepts recursive JSON instead of a closed
    ``ActionSpec``.  That preserves the action service's existing refusal
    status and payload for producer-owned extensions and invalid action kinds.
    """

    model_config = ConfigDict(strict=True)


class ActionEstimate(WireModel):
    """The closed canonical estimate/consent preview envelope."""

    model_config = ConfigDict(strict=True)

    rows: int
    cost: float | None
    cost_source: str
    billed_cost: int | None
    policy_id: str = Field(min_length=1)
    pricing_key: str | None = None
    engine: str | None = None
    remote_capability: str | None = None
    avg_input_tokens: int | None = None
    audio_seconds: float | None = None
    billing_label: str | None = None
    venue_label: str | None = None
    warning: str | None = None
    claims: list["ActionEstimateClaim"] | None = None
    promise_set_hash: str | None = None
    requires_confirmation: bool | None = None


class ActionEstimateClaim(WireModel):
    field: str
    display: str


class ActionEstimateResult(WireModel):
    schema_version: Literal["frisket.action_estimate_result.v1"]
    action: dict[str, str]
    project_id: str
    estimate: ActionEstimate


class ActionParamDiagnostic(WireModel):
    ok: bool
    message: str | None = None
    position: int | None = None


class ActionLogicalOutput(WireModel):
    key: str = Field(min_length=1)
    column_type: str = Field(min_length=1)
    existing_column_policy: Literal["generated", "compatible"] = "generated"


class ActionParamValidationResult(WireModel):
    schema_version: Literal["frisket.action_param_validation_result.v1"]
    action: dict[str, str]
    project_id: str
    diagnostics: dict[str, ActionParamDiagnostic]
    logical_outputs: list[ActionLogicalOutput] = Field(default_factory=list)
    creates_sheet: bool | None = None


ACTION_PREFLIGHT_ERROR_RESPONSES = {
    400: {"model": HttpError | ActionError},
    **{status: {"model": HttpError} for status in (401, 403, 404, 422, 500)},
}

# V1 action execution reports its domain outcomes as ActionResult envelopes,
# including confirmation and idempotency refusals. Authentication and malformed
# JSON remain the ordinary FastAPI HttpError/422 transport cases.
ACTION_RUN_ERROR_RESPONSES = {
    status: {"model": ActionResult} for status in (400, 402, 409, 500)
}


__all__ = [
    "ACTION_PREFLIGHT_ERROR_RESPONSES",
    "ACTION_RUN_ERROR_RESPONSES",
    "ActionEstimate",
    "ActionEstimateClaim",
    "ActionEstimateResult",
    "ActionEstimateValidationRequest",
    "ActionRunRequest",
    "ActionLogicalOutput",
    "ActionParamDiagnostic",
    "ActionParamValidationResult",
]
