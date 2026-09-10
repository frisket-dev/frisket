"""HTTP contracts for the pre-auth instance/runtime identity routes.

Existing-behavior migration for the three F1a operations: GET /api/config,
GET /api/health, and the Team-only GET /api/instance. These wires render
BEFORE sign-in, and their central contract problem is edition additivity:
hosted compositions add fields the open producers never emit. The rule here
is required-intersection fields + optional edition-only fields + open JSON
leaves for the genuinely additive subtrees — ``queue`` (the open producer's
frisket.queue_health.v1 payload with dynamic per-status job counts) and the
hosted-only ``posture``. Field declaration order matches the open producers'
construction order so a validated response serializes to the same bytes the
bare dicts did; edition-only fields are declared last and omitted when unset.
"""

from __future__ import annotations

from typing import Literal

from pydantic import JsonValue

from frisket.contracts.http.models import WireModel


class RuntimeConfigResponse(WireModel):
    """``GET /api/config`` (routes/runtime_config.py).

    The open producer always emits every field; a hosted composition may
    omit ``in_container`` (the compose-vs-shell restart copy has no hosted
    meaning), and only a mailing edition reports a non-null sender."""

    cache_mode: Literal["replay", "fresh", "replay_strict", "off"]
    live_calls_possible: bool
    cache_mode_editable: bool
    # Local installations store this setting outside a portable project. Team
    # and hosted deployments keep the user-owned value on the signed-in
    # identity profile instead.
    cost_preapproval_usd: str
    cost_preapproval_editable: bool
    in_container: bool | None = None
    recipe_fence_posture: str
    email_from_address: str | None
    email_from_name: str | None
    auth_methods: "AuthMethods"
    # Whether this composition exposes any workbench plugin contributions.
    # REQUIRED, with no fail-open default: runtime layout loading consumes it.
    plugins_available: bool
    # Whether users may manage nonbundled plugin lifecycle and configuration.
    # Bundled-only compositions expose contributions without exposing manager
    # controls, so this is deliberately distinct from ``plugins_available``.
    plugin_management_available: bool
    product_telemetry_available: bool


class OIDCAuthMethod(WireModel):
    """One named browser OIDC choice exposed by this running composition."""

    id: str
    label: str


class AuthMethods(WireModel):
    """Browser sign-in methods that are actually configured at runtime."""

    password: bool
    magic_link: bool
    oidc: list[OIDCAuthMethod]


class RuntimeConfigUpdateRequest(WireModel):
    """Local-only ``PATCH /api/config`` preference mutation."""

    cache_mode: Literal["replay", "fresh", "replay_strict", "off"] | None = None
    cost_preapproval_usd: str | None = None
    confirmed: bool = False


class HealthResponse(WireModel):
    """``GET /api/health`` (routes/health.py).

    ``ok`` is the intersection. ``queue`` is the open producer's additive
    subtree (queue_health_payload — dynamic ``jobs`` status keys, so the
    leaf stays open JSON). ``tier``/``posture`` are hosted-only additive
    fields with no open producer; they must validate when present and be
    omitted when unset, never required."""

    ok: bool
    queue: dict[str, JsonValue] | None = None
    tier: str | None = None
    posture: dict[str, JsonValue] | None = None


class InstanceInfoResponse(WireModel):
    """``GET /api/instance`` (team/session_routes.py).

    Team-outer-app only; the local core app deliberately serves 404 and the
    browser falls back to its default identity."""

    display_name: str
    support_contact: str | None


__all__ = [
    "AuthMethods",
    "HealthResponse",
    "InstanceInfoResponse",
    "OIDCAuthMethod",
    "RuntimeConfigResponse",
    "RuntimeConfigUpdateRequest",
]
