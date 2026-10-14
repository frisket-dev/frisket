"""Typed contracts for a local Frisket installation connected to Cloud.

The Cloud service and the Solo backend import this module as their common wire
authority.  Secret-bearing device/token responses are deliberately separate
from the browser-facing local DTOs so FastAPI cannot accidentally serialize a
Cloud bearer into the renderer response.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from frisket.contracts.http.models import WireModel
from frisket.contracts.transcription_sidecar import TranscriptionOptions


CloudAccountKind = Literal["personal", "organization"]
CloudCapability = Literal["llm.complete", "ocr", "transcribe", "document.convert"]
CloudLane = Literal["account_key", "frisket_credits"]
CloudConnectionState = Literal["disconnected", "connected", "expired"]
CloudExecutionStatus = Literal[
    "accepted",
    "running",
    "completed",
    "failed",
    "cancelled",
    "reconciliation_required",
]
CloudExecutionPreference = Literal["local_first", "cloud_first"]


class CloudDeviceAuthorizationRequest(WireModel):
    device_name: str = Field(min_length=1, max_length=120)


class CloudDeviceAuthorizationResponse(WireModel):
    device_code: str = Field(min_length=1, repr=False)
    user_code: str = Field(min_length=1)
    verification_uri: str = Field(min_length=1)
    verification_uri_complete: str = Field(min_length=1)
    expires_in: int = Field(gt=0)
    interval: int = Field(gt=0)


class CloudDeviceAuthorizationDecisionRequest(WireModel):
    approve: bool


class CloudDeviceAuthorizationDecisionResponse(WireModel):
    status: Literal["approved", "declined"]


class CloudTokenRequest(WireModel):
    grant_type: Literal["urn:ietf:params:oauth:grant-type:device_code"]
    device_code: str = Field(min_length=1, repr=False)


class CloudOAuthError(WireModel):
    error: Literal[
        "authorization_pending",
        "slow_down",
        "expired_token",
        "access_denied",
        "invalid_request",
    ]
    error_description: str | None = None


class CloudTokenResponse(WireModel):
    access_token: str = Field(min_length=1, repr=False)
    token_type: Literal["Bearer"]
    expires_in: int = Field(gt=0)


class CloudUser(WireModel):
    id: int = Field(gt=0)
    email: str = Field(min_length=1)
    display_name: str = Field(min_length=1)


class CloudAccount(WireModel):
    id: int = Field(gt=0)
    name: str = Field(min_length=1)
    kind: CloudAccountKind
    balance_micro: int
    available_micro: int = Field(ge=0)
    manage_billing: bool


class CloudSessionResponse(WireModel):
    user: CloudUser
    expires_at: str
    accounts: list[CloudAccount]


class CloudDeviceInfo(WireModel):
    id: int = Field(gt=0)
    device_name: str = Field(min_length=1)
    created_at: str
    expires_at: str
    last_used_at: str | None = None


class CloudDeviceListResponse(WireModel):
    devices: list[CloudDeviceInfo]


class CloudDeviceRevokeResponse(WireModel):
    revoked: bool


class CloudAccountProviderKey(WireModel):
    provider: str = Field(min_length=1)
    label: str = Field(min_length=1)
    configured: bool


class CloudAccountProviderKeysResponse(WireModel):
    providers: list[CloudAccountProviderKey]


class CloudTokenPrice(WireModel):
    kind: Literal["tokens"]
    input_micro_per_million_tokens: int = Field(ge=0)
    output_micro_per_million_tokens: int = Field(ge=0)


class CloudUnitPrice(WireModel):
    kind: Literal["unit"]
    unit: Literal["page", "audio_second"]
    micro_per_unit: int = Field(ge=0)


CloudRoutePrice = Annotated[
    CloudTokenPrice | CloudUnitPrice, Field(discriminator="kind")
]


class CloudLLMOptionSupport(WireModel):
    kind: Literal["llm"]
    structured_output: bool
    tools: bool
    vision: bool
    max_output_tokens: int | None = Field(default=None, gt=0)


class CloudOCROptionSupport(WireModel):
    kind: Literal["ocr"]
    language: bool
    geometry: bool
    searchable_pdf: bool


class CloudTranscriptionOptionSupport(WireModel):
    kind: Literal["transcribe"]
    diarization_mode: Literal["none", "optional", "intrinsic"]
    speaker_hint: Literal["none", "count"]
    language: bool
    model_size: bool
    vad: bool
    context: bool
    clean: bool = False


class CloudDocumentConvertOptionSupport(WireModel):
    kind: Literal["document.convert"]
    mime_types: list[str] = Field(default_factory=list)


CloudRouteOptionSupport = Annotated[
    CloudLLMOptionSupport
    | CloudOCROptionSupport
    | CloudTranscriptionOptionSupport
    | CloudDocumentConvertOptionSupport,
    Field(discriminator="kind"),
]


class CloudCapabilityRoute(WireModel):
    capability: CloudCapability
    engine: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    lane: CloudLane
    route_revision: str = Field(min_length=1)
    ready: bool
    blocker: str | None
    price: CloudRoutePrice | None
    options: CloudRouteOptionSupport

    @model_validator(mode="after")
    def _capability_matches_options(self) -> "CloudCapabilityRoute":
        expected = "llm" if self.capability == "llm.complete" else self.capability
        if self.options.kind != expected:
            raise ValueError("route options must match the route capability")
        return self


class CloudCapabilitiesResponse(WireModel):
    account_id: int = Field(gt=0)
    routes: list[CloudCapabilityRoute]


class CloudLLMRequest(WireModel):
    model: str = Field(min_length=1)
    messages: list[dict[str, JsonValue]]
    schema_: dict[str, JsonValue] | None = Field(default=None, alias="schema")
    max_tokens: int = Field(gt=0)
    temperature: float = Field(allow_inf_nan=False)
    params: dict[str, JsonValue] = Field(default_factory=dict)
    mechanism: str | None = None
    tools: list[dict[str, JsonValue]] | None = None
    reasoning_policy: Literal["disabled", "low_exclude"] | None = None


class CloudLLMExecutionRequest(WireModel):
    capability: Literal["llm.complete", "ocr"] = "llm.complete"
    account_id: int = Field(gt=0)
    route_revision: str = Field(min_length=1)
    request: CloudLLMRequest


class CloudOCROptions(WireModel):
    language: str | None = Field(default=None, min_length=1)
    geometry: bool = False
    searchable_pdf: bool = False


class CloudDocumentConvertOptions(WireModel):
    pass


class CloudOCRExecutionMetadata(WireModel):
    account_id: int = Field(gt=0)
    route_revision: str = Field(min_length=1)
    capability: Literal["ocr"]
    engine: str = Field(min_length=1)
    options: CloudOCROptions


class CloudTranscriptionExecutionMetadata(WireModel):
    account_id: int = Field(gt=0)
    route_revision: str = Field(min_length=1)
    capability: Literal["transcribe"]
    engine: str = Field(min_length=1)
    options: TranscriptionOptions


class CloudDocumentExecutionMetadata(WireModel):
    account_id: int = Field(gt=0)
    route_revision: str = Field(min_length=1)
    capability: Literal["document.convert"]
    engine: str = Field(min_length=1)
    options: CloudDocumentConvertOptions


CloudMediaExecutionMetadata = Annotated[
    CloudOCRExecutionMetadata
    | CloudTranscriptionExecutionMetadata
    | CloudDocumentExecutionMetadata,
    Field(discriminator="capability"),
]


class CloudExecutionQuote(WireModel):
    account_id: int = Field(gt=0)
    route_revision: str = Field(min_length=1)
    lane: CloudLane
    provider: str = Field(min_length=1)
    engine: str = Field(min_length=1)
    estimated_charge_micro: int | None = Field(default=None, ge=0)
    provider_cost_micro: int | None = Field(default=None, ge=0)
    expires_at: str


class CloudQuoteChallenge(WireModel):
    error: Literal["needs_confirmation"]
    confirmation: str = Field(min_length=1)
    quote: CloudExecutionQuote


class CloudLLMToolCall(WireModel):
    name: str = Field(min_length=1)
    args: str | dict[str, JsonValue]
    id: str = Field(min_length=1)
    provider_details: dict[str, JsonValue] | None = None


class CloudLLMResult(WireModel):
    content: str | None
    data: dict[str, JsonValue] | None
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    model: str = Field(min_length=1)
    cost_source: str = Field(min_length=1)
    cached: bool
    provider: str = Field(min_length=1)
    credential_source: str = Field(min_length=1)
    raw: dict[str, JsonValue]
    tool_calls: list[CloudLLMToolCall] | None = None
    output_tool_call: CloudLLMToolCall | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    output_limited: bool


class CloudOCRBlock(WireModel):
    text: str
    bbox: list[list[float]] | None = None
    score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @field_validator("bbox")
    @classmethod
    def _bbox_is_a_quadrilateral(
        cls, value: list[list[float]] | None
    ) -> list[list[float]] | None:
        if value is not None and (
            len(value) != 4 or any(len(point) != 2 for point in value)
        ):
            raise ValueError("bbox must contain four [x, y] points")
        return value


class CloudOCRPage(WireModel):
    page: int = Field(ge=1)
    text: str
    blocks: list[CloudOCRBlock]


class CloudOCRResult(WireModel):
    pages: list[CloudOCRPage]


class CloudTranscriptionWord(WireModel):
    word: str = Field(min_length=1)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)


class CloudTranscriptionSegment(WireModel):
    segment_index: int = Field(ge=0)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)
    text: str
    speaker: str | None = None
    speaker_confidence: str | None = None
    words: list[CloudTranscriptionWord] | None = None


class CloudTranscriptionResult(WireModel):
    text: str
    segments: list[CloudTranscriptionSegment]
    language: str | None
    duration: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class CloudDocumentConvertResult(WireModel):
    markdown: str
    ocr_used: list[JsonValue] = Field(default_factory=list)


CloudExecutionResult = (
    CloudLLMResult
    | CloudOCRResult
    | CloudTranscriptionResult
    | CloudDocumentConvertResult
)


class CloudLLMUsage(WireModel):
    kind: Literal["llm.complete"]
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)


class CloudOCRUsage(WireModel):
    kind: Literal["ocr"]
    pages: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    tokens_in: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    tokens_out: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _one_usage_shape(self) -> "CloudOCRUsage":
        has_pages = self.pages is not None
        has_tokens = self.tokens_in is not None and self.tokens_out is not None
        has_partial_tokens = (self.tokens_in is None) != (self.tokens_out is None)
        if has_partial_tokens or has_pages == has_tokens:
            raise ValueError("OCR usage must contain either pages or token counts")
        return self


class CloudTranscriptionUsage(WireModel):
    kind: Literal["transcribe"]
    audio_seconds: float = Field(ge=0, allow_inf_nan=False)


class CloudDocumentConvertUsage(WireModel):
    kind: Literal["document.convert"]
    pages: int = Field(ge=0)


CloudExecutionUsage = Annotated[
    CloudLLMUsage | CloudOCRUsage | CloudTranscriptionUsage | CloudDocumentConvertUsage,
    Field(discriminator="kind"),
]


class CloudExecutionReceipt(WireModel):
    user_id: int = Field(gt=0)
    account_id: int = Field(gt=0)
    lane: CloudLane
    provider: str = Field(min_length=1)
    engine: str = Field(min_length=1)
    charge_micro: int = Field(ge=0)
    provider_cost_micro: int | None = Field(default=None, ge=0)
    usage: CloudExecutionUsage


class CloudErrorResponse(WireModel):
    error: Literal[
        "invalid_request",
        "invalid_token",
        "connection_expired",
        "account_forbidden",
        "account_suspended",
        "route_changed",
        "insufficient_credits",
        "key_unavailable",
        "rate_limited",
        "execution_not_found",
        "result_expired",
        "execution_conflict",
        "provider_failed",
        "cancelled",
        "reconciliation_required",
    ]
    message: str
    retryable: bool = False
    execution_id: str | None = None


class CloudExecutionResponse(WireModel):
    execution_id: str = Field(min_length=1)
    status: CloudExecutionStatus
    route: CloudCapabilityRoute
    result: CloudExecutionResult | None
    receipt: CloudExecutionReceipt | None
    error: CloudErrorResponse | None = None


# Local backend -> renderer DTOs. These never contain device_code/access_token.


class CloudConnectionIdentity(WireModel):
    id: int = Field(gt=0)
    display_name: str
    email: str


class CloudConnectionAccountSummary(WireModel):
    id: int = Field(gt=0)
    name: str
    kind: CloudAccountKind
    eligible: bool
    eligibility_reason: str | None = None
    balance_label: str | None = None
    balance_url: str | None = None


class CloudConnectionCapabilities(WireModel):
    can_connect: bool
    can_reconnect: bool
    can_disconnect: bool


class CloudConnectionResponse(WireModel):
    state: CloudConnectionState
    identity: CloudConnectionIdentity | None
    expires_at: str | None = None
    accounts: list[CloudConnectionAccountSummary]
    capabilities: CloudConnectionCapabilities


class CloudDeviceAuthorizationStartResponse(WireModel):
    id: str = Field(min_length=1)
    user_code: str = Field(min_length=1)
    verification_uri: str = Field(min_length=1)
    verification_uri_complete: str | None = None
    expires_at: str
    poll_interval_seconds: int = Field(gt=0)


class CloudDeviceAuthorizationPollResponse(WireModel):
    id: str = Field(min_length=1)
    status: Literal["pending", "declined", "expired", "connected"]
    connection: CloudConnectionResponse


class CloudDeviceAuthorizationCancelResponse(WireModel):
    cancelled: bool


class CloudConnectionSignOutResponse(WireModel):
    connection: CloudConnectionResponse
    revocation: Literal["revoked", "not_connected", "failed"]
    warning: str | None = None


class CloudConnectionPreferencesResponse(WireModel):
    preference: CloudExecutionPreference


class CloudConnectionPreferencesPatch(WireModel):
    preference: CloudExecutionPreference


class ProjectCloudProviderKey(CloudAccountProviderKey):
    settings_url: str


class ProjectCloudRoutingResponse(WireModel):
    connection_state: CloudConnectionState
    selected_account_id: int | None
    preference: CloudExecutionPreference
    preference_inherited: bool
    accounts: list[CloudConnectionAccountSummary]
    overlap_provider_labels: list[str]
    can_mutate: bool
    cloud_provider_keys: list[ProjectCloudProviderKey] = Field(default_factory=list)


class ProjectCloudRoutingPatch(WireModel):
    selected_account_id: int | None = None
    preference: CloudExecutionPreference | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "ProjectCloudRoutingPatch":
        if not self.model_fields_set:
            raise ValueError("at least one cloud routing field is required")
        if (
            "selected_account_id" in self.model_fields_set
            and self.selected_account_id is None
        ):
            raise ValueError("selected_account_id cannot be null")
        if "preference" in self.model_fields_set and self.preference is None:
            raise ValueError("preference cannot be null")
        return self


__all__ = [
    name
    for name in globals()
    if name.startswith("Cloud") or name.startswith("ProjectCloud")
]
