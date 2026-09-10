"""HTTP contracts for the notification read/actor-state browser routes.

Existing-behavior migration: these DTOs freeze the executable wire truth the
notification service producers already emit (``public_notification_item``,
``public_actor_state``, the page/summary envelopes, and the bare seen/ack
count results). emit/deliver and the channel/route/delivery routes are a
different cluster and stay untyped.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
)

from frisket.contracts.http.models import WireModel


# The envelope literals the producers emit
# (frisket.server.notifications.service). Restated here because the contracts
# package stays out of the service layer; the backend contract test pins them
# against the producer's own constants.
NOTIFICATION_PAGE_SCHEMA_VERSION = "frisket.notifications_page.v1"
NOTIFICATION_SUMMARY_SCHEMA_VERSION = "frisket.notifications_summary.v1"


class _CompatibleRequest(BaseModel):
    """Keep the existing operational request coercion at this boundary."""

    model_config = ConfigDict(
        extra="ignore",
        strict=False,
        validate_by_alias=True,
        validate_by_name=False,
    )

    def provided_payload(self) -> dict[str, Any]:
        """Return exactly the supplied public wire keys for the service."""

        return self.model_dump(by_alias=True, exclude_unset=True)


class NotificationStateFilterRequest(_CompatibleRequest):
    """The seen/ack bulk filter. An empty body is the everything-filter.

    Every field is TOLERANT: a value the annotation cannot coerce passes
    through raw instead of failing request validation, because the service's
    ``normalize_filter_payload`` owns the refusals and its error envelopes —
    including the raw Python ``int()`` message leak — are frozen wire
    behavior. A route-level 422 here would replace those frozen 400s.
    """

    notification_ids: list[int] | None = None
    source_kind: str | None = None
    source_ref: dict[str, JsonValue] | None = None
    before_created_at: str | None = None

    @field_validator("*", mode="wrap")
    @classmethod
    def _tolerate_uncoercible_values(cls, value: Any, handler: Any) -> Any:
        try:
            return handler(value)
        except ValidationError:
            return value

    def filter_payload(self) -> dict[str, Any]:
        """Exactly the keys the caller provided, as the service reads today.

        The service's ``normalize_filter_payload`` reads each key with
        ``.get``, so an absent key and an explicit null are equivalent — but
        the route-seam contract pins that the service still receives only the
        caller's own keys, not a four-key expansion.
        """

        return {name: getattr(self, name) for name in self.model_fields_set}


class NotificationItem(WireModel):
    id: int
    source_kind: str
    source_ref: dict[str, JsonValue]
    source_event_ids: list[int]
    event_count: int
    event_kinds: list[str]
    title: str
    summary: str
    severity: str
    deep_link: dict[str, JsonValue]
    created_at: str
    updated_at: str
    state: Literal["unseen", "seen", "read", "acknowledged"]
    seen_at: str | None
    read_at: str | None
    acknowledged_at: str | None
    acknowledged_by: str | None


class NotificationPage(WireModel):
    schema_version: Literal[NOTIFICATION_PAGE_SCHEMA_VERSION]
    order: Literal["desc"]
    offset: int
    limit: int
    total: int
    has_more: bool
    next_offset: int | None
    notifications: list[NotificationItem]


class NotificationSummarySourceRef(WireModel):
    source_kind: str
    source_ref: dict[str, JsonValue]
    unseen: int


class NotificationSummary(WireModel):
    schema_version: Literal[NOTIFICATION_SUMMARY_SCHEMA_VERSION]
    total: int
    unseen: int
    seen: int
    read: int
    acknowledged: int
    by_severity: dict[str, int]
    by_source_kind: dict[str, int]
    # Watch-only by construction: the producing SQL groups on
    # json_extract(source_ref, '$.watch_id').
    by_source_ref: list[NotificationSummarySourceRef]


class NotificationActorState(WireModel):
    notification_id: int
    actor_id: str
    state: Literal["unseen", "seen", "read", "acknowledged"]
    seen_at: str | None
    read_at: str | None
    acknowledged_at: str | None
    acknowledged_by: str | None
    updated_at: str


class SeenResult(WireModel):
    # Bare one-field object with no schema_version — frozen as-is.
    seen_count: int


class AckResult(WireModel):
    acknowledged_count: int


class NotificationChannelRequest(_CompatibleRequest):
    """Compatibility request shape for channel create and patch.

    These values deliberately remain permissive: the notification service owns
    coercion and its long-standing error messages.  In particular, ``enabled``
    must arrive there as the caller supplied it.
    """

    kind: str | None = None
    name: str | None = None
    raw_enabled: JsonValue | None = Field(default=None, alias="enabled")
    owner_kind: str | None = None
    owner_ref: str | None = None
    audience: JsonValue | None = None
    to: str | None = None
    from_: str | None = Field(default=None, alias="from")
    from_address: str | None = None
    provider: str | None = None
    channel_label: str | None = None
    webhook_url: str | None = None
    webhook_host: str | None = None
    webhook_url_secret_ref: str | None = None
    url_secret_ref: str | None = None
    webhook_secret_ref: str | None = None
    secret_ref: str | None = None
    signing_secret_ref: str | None = None
    webhook_signing_secret_ref: str | None = None
    signature_secret_ref: str | None = None
    signature_header: str | None = None
    max_request_bytes: JsonValue | None = None
    max_response_bytes: JsonValue | None = None
    max_redirects: JsonValue | None = None


class NotificationRouteRequest(_CompatibleRequest):
    """Compatibility request shape for route create and patch."""

    name: str | None = None
    raw_enabled: JsonValue | None = Field(default=None, alias="enabled")
    owner_kind: str | None = None
    owner_ref: str | None = None
    recipient_actor_id: str | None = None
    channel_id: JsonValue | None = None
    source_kind: str | None = None
    source_ref_match: JsonValue | None = None
    source_ref_match_json: JsonValue | None = None
    event_kinds: JsonValue | None = None
    severity_min: str | None = None
    delivery_mode: str | None = None
    digest_cadence: str | None = None
    digest_timezone: str | None = None
    digest_anchor_time: str | None = None
    template_key: str | None = None


class NotificationChannel(WireModel):
    id: int
    kind: Literal["in_app", "email", "slack", "webhook"]
    name: str
    enabled: bool
    owner_kind: str
    config: dict[str, JsonValue]
    has_secret: bool
    created_at: str
    updated_at: str


class NotificationChannelsPage(WireModel):
    schema_version: Literal["frisket.notification_channels.v1"]
    channels: list[NotificationChannel]


class NotificationRoute(WireModel):
    id: int
    name: str
    enabled: bool
    owner_kind: str
    owner_ref: str
    recipient_actor_id: str | None
    channel_id: int
    source_kind: str | None
    source_ref_match: dict[str, JsonValue]
    event_kinds: list[str]
    severity_min: str
    delivery_mode: Literal["immediate", "digest"]
    digest_cadence: Literal["hourly", "daily", "manual"] | None
    digest_timezone: str
    digest_anchor_time: str | None
    template_key: str | None
    created_at: str
    updated_at: str


class NotificationRoutesPage(WireModel):
    schema_version: Literal["frisket.notification_routes.v1"]
    routes: list[NotificationRoute]


class NotificationDeliveryRequest(WireModel):
    id: int
    route_id: int | None
    channel_id: int
    notification_id: int | None
    digest_run_id: int | None
    delivery_kind: str
    dedupe_key: str
    status: Literal[
        "queued",
        "processing",
        "sent",
        "failed",
        "skipped",
        "cancelled",
        "reconciliation_required",
    ]
    job_id: int | None
    available_at: str
    last_error: str | None
    provider_ref: str | None
    created_at: str
    updated_at: str
    sent_at: str | None


class NotificationDeliveryRequestsPage(WireModel):
    schema_version: Literal["frisket.notification_delivery_requests.v1"]
    order: Literal["desc"]
    offset: int
    limit: int
    total: int
    has_more: bool
    next_offset: int | None
    delivery_requests: list[NotificationDeliveryRequest]


__all__ = [
    "NOTIFICATION_PAGE_SCHEMA_VERSION",
    "NOTIFICATION_SUMMARY_SCHEMA_VERSION",
    "AckResult",
    "NotificationActorState",
    "NotificationChannel",
    "NotificationChannelRequest",
    "NotificationChannelsPage",
    "NotificationDeliveryRequest",
    "NotificationDeliveryRequestsPage",
    "NotificationItem",
    "NotificationPage",
    "NotificationRoute",
    "NotificationRouteRequest",
    "NotificationRoutesPage",
    "NotificationStateFilterRequest",
    "NotificationSummary",
    "NotificationSummarySourceRef",
    "SeenResult",
]
