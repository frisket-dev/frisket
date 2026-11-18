"""Consent to OCR exact source pages of a reviewed PDF import."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from typing import Any, Mapping

from frisket.contracts.http.action_estimate_validation import ActionEstimate
from frisket.engine.runner.confirmation_context import ConsentEstimate
from frisket.engine.runner.validation import _bind_rated_estimate
from frisket.engine.store.cells import replay_generated_value_hash
from frisket.execution.consent_coverage import effective_consent_coverage
from frisket.execution.promises import promise_rows_hash
from frisket.execution.resolve_for_action import (
    BoundedScratchInput,
    consented_set_hash,
    gate_claims_payload,
    project_uncovered_user_claims,
    promise_rows,
    resolve_for_action,
    target_snapshot,
)
from frisket.execution.resolver import Refusal, ResolvedExecution
from frisket.server.services.scratch_action_preview import (
    ScratchActionProgram,
    scratch_estimate,
)


PDF_SOURCE_PAGE_OCR_SCOPE_SCHEMA = "frisket.pdf_source_page_ocr_scope.v1"

PDF_IMPORT_OCR_CONSENT_SCHEMA = "frisket.pdf_import_ocr_approval.v1"
PDF_IMPORT_OCR_ALLOCATION_SCHEMA = "frisket.pdf_import_ocr_allocation.v1"


class PdfImportOcrConsentError(RuntimeError):
    """The selected paid OCR route cannot produce an exact wizard quote."""


@dataclass(frozen=True)
class PdfImportOcrGrant:
    """The exact parent approval carried from the wizard into import execution."""

    schema_version: str
    actor: str
    promise_set_hash: str
    scope_hash: str
    scope: Mapping[str, Any]
    ocr_action_params: Mapping[str, Any]
    target: Mapping[str, Any]
    route: Mapping[str, Any]
    promises: tuple[Mapping[str, Any], ...]
    consent_estimate: Mapping[str, Any]
    allocation: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PdfImportOcrGrant:
        if value.get("schema_version") != PDF_IMPORT_OCR_CONSENT_SCHEMA:
            raise ValueError("invalid PDF import OCR grant schema")
        grant = cls(
            schema_version=PDF_IMPORT_OCR_CONSENT_SCHEMA,
            actor=value["actor"],
            promise_set_hash=value["promise_set_hash"],
            scope_hash=value["scope_hash"],
            scope=dict(value["scope"]),
            ocr_action_params=dict(value["ocr_action_params"]),
            target=dict(value["target"]),
            route=dict(value["route"]),
            promises=tuple(dict(row) for row in value["promises"]),
            consent_estimate=dict(value["quote"]),
            allocation=dict(value["allocation"]),
        )
        grant._validate()
        return grant

    def _validate(self) -> None:
        if not isinstance(self.actor, str) or not self.actor:
            raise ValueError("PDF import OCR grant actor is invalid")
        for name, value in (
            ("promise hash", self.promise_set_hash),
            ("scope hash", self.scope_hash),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError(f"PDF import OCR grant {name} is invalid")
        if self.scope.get("schema_version") != PDF_SOURCE_PAGE_OCR_SCOPE_SCHEMA:
            raise ValueError("PDF import OCR grant scope schema is invalid")
        _validate_source_page_grant(self)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "actor": self.actor,
            "promise_set_hash": self.promise_set_hash,
            "scope_hash": self.scope_hash,
            "scope": dict(self.scope),
            "ocr_action_params": dict(self.ocr_action_params),
            "target": dict(self.target),
            "route": dict(self.route),
            "promises": [dict(row) for row in self.promises],
            "quote": dict(self.consent_estimate),
            "allocation": dict(self.allocation),
        }


@dataclass(frozen=True)
class PdfImportOcrConsentEstimate:
    estimate: Mapping[str, Any]
    authorization: Mapping[str, Any]

    def as_mapping(self) -> dict[str, Any]:
        return {
            "estimate": dict(self.estimate),
            "authorization": dict(self.authorization),
        }


def _route_snapshot(resolved: ResolvedExecution) -> dict[str, Any]:
    facts = resolved.resolution.facts
    return {
        "engine": facts.engine,
        "operator": facts.operator,
        "egress_class": facts.egress_class,
        "region": facts.region,
        "cost_posture": facts.cost_posture,
        "cloud_payer": (
            facts.cloud_payer.to_snapshot() if facts.cloud_payer is not None else None
        ),
    }


__all__ = [
    "PDF_IMPORT_OCR_CONSENT_SCHEMA",
    "PDF_IMPORT_OCR_ALLOCATION_SCHEMA",
    "PdfImportOcrConsentError",
    "PdfImportOcrConsentEstimate",
    "PdfImportOcrGrant",
]


def _source_page_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def source_page_ocr_grant(
    *,
    project_id: str,
    plan_token: str,
    project: Any,
    composition: Any,
    consent_coverage: Any,
    targets: list[dict[str, Any]],
    children: list[dict[str, Any]],
    ocr_action_params: Mapping[str, Any],
) -> PdfImportOcrConsentEstimate:
    """Freeze exact scratch page authorities into the existing import grant.

    Each child is one distinct original byte/page/settings target. Occurrence
    identities remain in the parent roster, independently of execution dedup.
    The reviewed parent quote is the sum of the actual per-page reservations.
    """
    if not children or not targets:
        raise ValueError("source-page OCR grant has no missing pages")
    policies = {child["quote"]["policy_id"] for child in children}
    if len(policies) != 1:
        raise ValueError("source-page OCR pricing policy changed")
    if any(
        child["quote"].get("cost") is None or child["quote"].get("billed_cost") is None
        for child in children
    ):
        # An allowance never covers a page without a price, and consent is to
        # an exact cost, so an unpriced page cannot be estimated at all.
        raise PdfImportOcrConsentError(
            "The selected OCR engine has no exact price for these pages; choose a priced engine."
        )
    scope = {
        "schema_version": PDF_SOURCE_PAGE_OCR_SCOPE_SCHEMA,
        "project_id": project_id,
        "plan_token": plan_token,
        "targets": targets,
        "page_count": len(children),
        "source_count": len({target["occurrence_key"] for target in targets}),
        "ocr_params_hash": replay_generated_value_hash(dict(ocr_action_params)),
    }
    cost = sum(Decimal(str(child["quote"]["cost"])) for child in children)
    spec = dict(children[0]["spec"])
    program = ScratchActionProgram("media.ocr", "ocr")
    resolved = resolve_for_action(
        project,
        spec,
        program,
        composition=composition,
        bounded_scratch_input=BoundedScratchInput(len(children), scope),
    )
    if isinstance(resolved, Refusal):
        raise PdfImportOcrConsentError(resolved.remedy)
    if not isinstance(resolved, ResolvedExecution):
        raise PdfImportOcrConsentError("OCR route is unavailable")
    rated = scratch_estimate(resolved, quantity_name=None, quantity=len(children))
    rated.update(
        cost=float(cost),
        billed_cost=sum(child["quote"]["billed_cost"] for child in children),
        policy_id=next(iter(policies)),
        rows=scope["source_count"],
    )
    bound = _bind_rated_estimate(resolved, rated)
    effective = effective_consent_coverage(project, consent_coverage)
    uncovered = project_uncovered_user_claims(
        project, bound.promise_set, spec=spec, consent_coverage=effective
    )
    payload = {**rated, "requires_confirmation": bool(uncovered)}
    consent_hash = consented_set_hash(bound.promise_set)
    if uncovered:
        payload["claims"] = gate_claims_payload(
            uncovered,
            bound.resolution.facts,
            bound.presentation,
            has_cost_claim=any(p.field == "cost" for p in bound.promise_set.promises),
            rated_estimate=rated,
        )
        payload["promise_set_hash"] = consent_hash
    estimate = ActionEstimate.model_validate(payload).model_dump(
        mode="json", exclude_unset=True
    )
    consent_estimate = ConsentEstimate.from_estimate(rated).model_dump(mode="json")
    allocation = {
        "schema_version": PDF_IMPORT_OCR_ALLOCATION_SCHEMA,
        "row_quote_authority": "admitted_source_pages",
        "currency": "USD",
        "policy_id": next(iter(policies)),
        "approved_billed_cost": consent_estimate["billed_cost"],
        "children": children,
        "requires_confirmation": bool(uncovered),
    }
    grant = PdfImportOcrGrant.from_mapping(
        {
            "schema_version": PDF_IMPORT_OCR_CONSENT_SCHEMA,
            "actor": effective.principal,
            "promise_set_hash": consent_hash,
            "scope_hash": _source_page_hash(scope),
            "scope": scope,
            "ocr_action_params": dict(ocr_action_params),
            "target": target_snapshot(bound.resolution),
            "route": _route_snapshot(bound),
            "promises": promise_rows(bound.promise_set),
            "quote": consent_estimate,
            "allocation": allocation,
        }
    )
    return PdfImportOcrConsentEstimate(estimate, grant.as_mapping())


def _validate_source_page_grant(grant: PdfImportOcrGrant) -> None:
    scope = grant.scope
    children = grant.allocation.get("children")
    targets = scope.get("targets")
    if (
        not scope.get("project_id")
        or not scope.get("plan_token")
        or not isinstance(targets, list)
        or not targets
        or not isinstance(children, list)
        or not children
        or grant.allocation.get("row_quote_authority") != "admitted_source_pages"
        or grant.allocation.get("schema_version") != PDF_IMPORT_OCR_ALLOCATION_SCHEMA
        or grant.allocation.get("currency") != "USD"
    ):
        raise ValueError("source-page OCR grant scope is incomplete")
    keys = [child.get("child_key") for child in children]
    if None in keys or len(set(keys)) != len(keys):
        raise ValueError("source-page OCR grant work identities changed")
    if set(keys) != {target.get("work_key") for target in targets}:
        raise ValueError("source-page OCR grant targets changed")
    if len(children) != scope.get("page_count"):
        raise ValueError("source-page OCR grant page count changed")
    if scope.get("ocr_params_hash") != replay_generated_value_hash(
        dict(grant.ocr_action_params)
    ):
        raise ValueError("source-page OCR grant settings changed")
    if grant.scope_hash != _source_page_hash(scope):
        raise ValueError("source-page OCR grant scope hash changed")
    if grant.consent_estimate.get("billed_cost") != sum(
        child["quote"]["billed_cost"] for child in children
    ) or grant.allocation.get("approved_billed_cost") != grant.consent_estimate.get(
        "billed_cost"
    ):
        raise ValueError("source-page OCR grant quote changed")
    if promise_rows_hash(grant.promises) != grant.promise_set_hash:
        raise ValueError("source-page OCR grant promise set changed")


def source_page_ocr_authorizer(
    grant: PdfImportOcrGrant,
    *,
    project_id: str,
    plan_token: str,
    target: Mapping[str, Any],
    work_key: str,
):
    """Derive authority only for the exact frozen page, route and rated quote."""
    from frisket.engine.runner.validation import (
        TrustedConsentGrant,
        TrustedConsentRefusal,
    )
    from frisket.execution.resolve_for_action import SCRATCH_PREVIEW_UNIT_ID

    grant._validate()
    if grant.scope.get("schema_version") != PDF_SOURCE_PAGE_OCR_SCOPE_SCHEMA or any(
        (
            grant.scope.get("project_id") != project_id,
            grant.scope.get("plan_token") != plan_token,
        )
    ):
        raise ValueError("source-page OCR approval belongs to another split")
    if {"work_key": work_key, **dict(target)} not in grant.scope["targets"]:
        raise ValueError("source page identity is outside the approved work set")
    child = next(
        (
            item
            for item in grant.allocation["children"]
            if item["child_key"] == work_key
        ),
        None,
    )
    if child is None:
        raise ValueError("source page is outside the approved work set")

    def authorize(context):
        if context.principal != grant.actor:
            return TrustedConsentRefusal(
                "source-page OCR approval belongs to another principal"
            )
        if context.row_ids != (SCRATCH_PREVIEW_UNIT_ID,):
            return TrustedConsentRefusal("source-page OCR runtime scope changed")
        resolved = context.resolved_execution
        # Scratch estimates calculate this UI flag from the current account's
        # allowance threshold. An already approved page still binds every
        # economic fact and promise when a background worker has zero allowance.
        actual_estimate = ConsentEstimate.from_estimate(context.estimate).model_dump(
            mode="json", exclude={"requires_confirmation"}
        )
        expected_estimate = {
            key: value
            for key, value in child["quote"].items()
            if key != "requires_confirmation"
        }
        if (
            dict(context.spec) != child["spec"]
            or target_snapshot(resolved.resolution) != child["target"]
            or _route_snapshot(resolved) != child["route"]
            or promise_rows(resolved.promise_set) != child["promises"]
            or actual_estimate != expected_estimate
        ):
            return TrustedConsentRefusal(
                "source-page OCR page, settings, route or quote changed"
            )
        return TrustedConsentGrant("pdf_source_page_grant_derived")

    return authorize
