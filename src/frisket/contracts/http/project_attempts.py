"""HTTP contracts for the project-keyed attempt receipt reader."""

from __future__ import annotations

from typing import Literal

from pydantic import JsonValue

from frisket.contracts.http.models import WireModel


class ProjectAttemptTarget(WireModel):
    target_id: str
    transport: str
    engine: str | None
    operator: str | None
    egress_class: str | None
    region: str | None
    credential_source: str | None


class ProjectAttemptConsent(WireModel):
    id: str
    grant_basis: str | None
    actor: str | None
    granted_at: str | None
    promise_set_hash: str | None


class ProjectAttemptSettlement(WireModel):
    price_card_version: str | None
    terminal_status: str | None
    charge_usd: str | None
    unsettleable: str = None  # type: ignore[assignment]
    pricing_key: str | None = None
    unit_rate: str = None  # type: ignore[assignment]
    quantity_unit: str = None  # type: ignore[assignment]
    charge_authority: str = None  # type: ignore[assignment]
    ceiling_mode: str = None  # type: ignore[assignment]
    row_settlement_mode: str = None  # type: ignore[assignment]
    metered_quantity: str = None  # type: ignore[assignment]
    metered_unit: str = None  # type: ignore[assignment]
    billable_quantity: str = None  # type: ignore[assignment]
    rated_charge_usd: str | None = None
    charged_quantity: str | None = None
    absorbed_overage_usd: str | None = None
    rated_calls: int = None  # type: ignore[assignment]
    unmetered_calls: int = None  # type: ignore[assignment]
    consented_quantity: str | None = None
    exceeds_consented: bool | None = None


class ProjectAttemptBorneBy(WireModel):
    credentialed_providers: list[str]


class ProjectAttemptReceipt(WireModel):
    attempt_id: str
    run_id: int | None
    receipt_id: str | None
    seq: int
    state: str
    action_identity_hash: str
    scope: list[int]
    target: ProjectAttemptTarget | None
    consent: ProjectAttemptConsent | None
    cost_basis: JsonValue | None
    price_card_version: str | None
    settlement: ProjectAttemptSettlement | None
    evaluation: JsonValue | None
    borne_by: ProjectAttemptBorneBy | None
    created_at: str


class ProjectAttemptsPage(WireModel):
    schema_version: Literal["frisket.attempt_receipts.v1"]
    order: Literal["created_at DESC"]
    offset: int
    limit: int
    total: int
    has_more: bool
    next_offset: int | None
    run_id: int | None
    attempts: list[ProjectAttemptReceipt]


__all__ = [
    "ProjectAttemptBorneBy",
    "ProjectAttemptConsent",
    "ProjectAttemptReceipt",
    "ProjectAttemptSettlement",
    "ProjectAttemptTarget",
    "ProjectAttemptsPage",
]
