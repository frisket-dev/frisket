"""Exact pre-import quote for a frozen, server-measured PDF OCR scope."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from typing import Any, Mapping

from frisket.actions.media import OcrParams, ocr_options
from frisket.contracts.http.action_estimate_validation import ActionEstimate
from frisket.engine.runner.confirmation_context import ConsentQuote
from frisket.engine.runner.network_policy import remote_capability_for_spec
from frisket.engine.runner.validation import _bind_rated_quote, confirmation_estimate
from frisket.engine.store.cells import replay_generated_value_hash
from frisket.execution.consent_coverage import effective_consent_coverage
from frisket.execution.pricing_policy import default_pricing_policy
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
    ScratchActionRecipe,
    scratch_estimate,
)


PDF_SOURCE_PAGE_OCR_SCOPE_SCHEMA = "frisket.pdf_source_page_ocr_scope.v1"

PDF_IMPORT_OCR_APPROVAL_SCHEMA = "frisket.pdf_import_ocr_approval.v1"
PDF_IMPORT_OCR_SCOPE_SCHEMA = "frisket.pdf_import_ocr_scope.v1"
PDF_IMPORT_OCR_CHILD_SCOPE_SCHEMA = "frisket.pdf_import_ocr_child_scope.v1"
PDF_IMPORT_OCR_ALLOCATION_SCHEMA = "frisket.pdf_import_ocr_allocation.v1"


class PdfImportOcrApprovalError(RuntimeError):
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
    quote: Mapping[str, Any]
    allocation: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PdfImportOcrGrant:
        if value.get("schema_version") != PDF_IMPORT_OCR_APPROVAL_SCHEMA:
            raise ValueError("invalid PDF import OCR grant schema")
        grant = cls(
            schema_version=PDF_IMPORT_OCR_APPROVAL_SCHEMA,
            actor=value["actor"],
            promise_set_hash=value["promise_set_hash"],
            scope_hash=value["scope_hash"],
            scope=dict(value["scope"]),
            ocr_action_params=dict(value["ocr_action_params"]),
            target=dict(value["target"]),
            route=dict(value["route"]),
            promises=tuple(dict(row) for row in value["promises"]),
            quote=dict(value["quote"]),
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
        if self.scope.get("schema_version") == PDF_SOURCE_PAGE_OCR_SCOPE_SCHEMA:
            _validate_source_page_grant(self)
            return
        if self.allocation.get("schema_version") != PDF_IMPORT_OCR_ALLOCATION_SCHEMA:
            raise ValueError("PDF import OCR grant allocation schema is invalid")
        if self.allocation.get("currency") != "USD":
            raise ValueError("PDF import OCR grant currency is invalid")
        if self.allocation.get("row_quote_authority") != "canonical_commit_occurrences":
            raise ValueError("PDF import OCR grant row quote authority is invalid")
        source_count = int(self.scope["source_count"])
        page_count = int(self.scope["page_count"])
        if (
            int(self.allocation.get("source_count", -1)) != source_count
            or int(self.allocation.get("page_count", -1)) != page_count
        ):
            raise ValueError("PDF import OCR grant allocation scope changed")
        approved = self.allocation.get("approved_billed_cost")
        if not isinstance(approved, int) or isinstance(approved, bool) or approved < 0:
            raise ValueError("PDF import OCR grant approved cost is invalid")
        if self.quote.get("billed_cost") != approved:
            raise ValueError("PDF import OCR grant quote total changed")
        policy_id = self.allocation.get("policy_id")
        if not isinstance(policy_id, str) or not policy_id:
            raise ValueError("PDF import OCR grant pricing policy is invalid")
        children = self.allocation.get("children")
        if not isinstance(children, list) or not children:
            raise ValueError("PDF import OCR grant has no child quotes")
        child_sources = child_pages = child_billed = 0
        previous_end = 0
        for child in children:
            if not isinstance(child, Mapping):
                raise ValueError("PDF import OCR grant child quote is invalid")
            start = int(child["commit_ordinal_start"])
            end = int(child["commit_ordinal_end"])
            if start <= previous_end or end < start or end - start >= 256:
                raise ValueError("PDF import OCR grant child range is invalid")
            if child.get("child_key") != f"commit-ordinals:{start}-{end}":
                raise ValueError("PDF import OCR grant child identity changed")
            child_quote = child.get("quote")
            if not isinstance(child_quote, Mapping):
                raise ValueError("PDF import OCR grant child quote is invalid")
            if child_quote.get("policy_id") != policy_id:
                raise ValueError("PDF import OCR grant child pricing policy changed")
            child_sources += int(child["source_count"])
            child_pages += int(child["page_count"])
            child_billed += int(child_quote["billed_cost"])
            previous_end = end
        if (
            child_sources != source_count
            or child_pages != page_count
            or child_billed != approved
        ):
            raise ValueError("PDF import OCR grant child totals changed")

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
            "quote": dict(self.quote),
            "allocation": dict(self.allocation),
        }


@dataclass(frozen=True)
class PdfImportOcrApprovalQuote:
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


def _compact_promise_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep child grants bounded while retaining an exact row-quote digest."""

    compact = []
    for raw in rows:
        row = dict(raw)
        basis = row.get("basis")
        if isinstance(basis, Mapping) and basis.get("row_quote_quantities") is not None:
            basis = dict(basis)
            quantities = basis.pop("row_quote_quantities")
            encoded = json.dumps(
                quantities, sort_keys=True, separators=(",", ":")
            ).encode()
            basis["row_quote_quantities_digest"] = hashlib.sha256(encoded).hexdigest()
            basis["row_quote_quantities_count"] = len(quantities)
            row["basis"] = basis
        compact.append(row)
    return compact


class PdfImportOcrApprovalQuoter:
    """Resolve and rate the exact import scope through ordinary OCR routing."""

    def __init__(self, workspace: Any) -> None:
        self._workspace = workspace

    def quote(
        self,
        project_id: str,
        binding: Mapping[str, Any],
        request_context: Any = None,
    ) -> dict[str, Any]:
        selected_count = int(binding["selected_count"])
        total_pages = int(binding["known_pages"])
        if selected_count < 1 or total_pages < 1:
            raise PdfImportOcrApprovalError(
                "paid OCR approval requires at least one measured PDF page"
            )
        action_params = dict(binding["ocr_action_params"])
        try:
            params = OcrParams.model_validate({"source": "pdf_import", **action_params})
            normalized_options = ocr_options(params).normalize(params.engine.root)
        except (TypeError, ValueError) as exc:
            raise PdfImportOcrApprovalError(str(exc)) from exc

        project = self._workspace.get(project_id)
        router = self._workspace.action_execution_router_for(project)
        execution_context = self._workspace.edition_execution_composition_context_for(
            request_context
        )
        composition = self._workspace.execution_composition_for(
            project, router, execution_context
        )
        coverage = None
        if self._workspace.executor_deps_factory is not None:
            coverage = self._workspace.executor_deps_factory(
                project_id, request_context
            ).consent_coverage
        effective = effective_consent_coverage(project, coverage)
        child_partitions = [dict(row) for row in binding["child_partitions"]]
        child_partitions_hash = hashlib.sha256(
            json.dumps(child_partitions, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if (
            not child_partitions
            or sum(int(row["source_count"]) for row in child_partitions)
            != selected_count
            or sum(int(row["page_count"]) for row in child_partitions) != total_pages
            or any(
                int(row["source_count"]) < 1 or int(row["page_count"]) < 1
                for row in child_partitions
            )
            or child_partitions_hash != binding["child_partitions_hash"]
        ):
            raise PdfImportOcrApprovalError(
                "paid OCR child partitions do not match the measured PDF scope"
            )
        work_scope = {
            "schema_version": PDF_IMPORT_OCR_SCOPE_SCHEMA,
            "identity": str(binding["manifest_hash"]),
            "storage_identity": str(binding["storage_identity"]),
            "draft_id": str(binding["draft_id"]),
            "roster_digest": str(binding["work_scope_hash"]),
            "source_count": selected_count,
            "page_count": total_pages,
            "ocr_params_hash": replay_generated_value_hash(action_params),
            "child_partitions_hash": str(binding["child_partitions_hash"]),
        }
        spec = {
            "action_kind": "media.ocr",
            "sheet_id": 0,
            "params": params.model_dump(mode="json"),
            "engine": params.engine.root,
            **normalized_options,
        }
        recipe = ScratchActionRecipe("media.ocr", "ocr")
        policy = default_pricing_policy()
        remote_capability = remote_capability_for_spec(recipe, spec)

        def resolve_scope(
            page_count: int, scope: Mapping[str, Any]
        ) -> ResolvedExecution:
            resolved = resolve_for_action(
                project,
                spec,
                recipe,
                composition=composition,
                bounded_scratch_input=BoundedScratchInput(page_count, scope),
            )
            if isinstance(resolved, Refusal):
                raise PdfImportOcrApprovalError(resolved.remedy)
            if not isinstance(resolved, ResolvedExecution):
                raise PdfImportOcrApprovalError("OCR route is unavailable")
            return resolved

        def rated_scope(
            source_count: int,
            page_count: int,
            scope: Mapping[str, Any],
            *,
            row_quantities: Mapping[int, float] | None = None,
        ) -> tuple[ResolvedExecution, dict[str, Any], str]:
            resolved = resolve_for_action(
                project,
                spec,
                recipe,
                composition=composition,
                bounded_scratch_input=BoundedScratchInput(
                    page_count, scope, row_quantities=row_quantities
                ),
            )
            if isinstance(resolved, Refusal):
                raise PdfImportOcrApprovalError(resolved.remedy)
            if not isinstance(resolved, ResolvedExecution):
                raise PdfImportOcrApprovalError("OCR route is unavailable")
            raw = scratch_estimate(resolved, quantity_name=None, quantity=page_count)
            raw["rows"] = source_count
            rated = confirmation_estimate(recipe, spec, raw, policy=policy)
            bound = _bind_rated_quote(resolved, rated)
            uncovered = project_uncovered_user_claims(
                project,
                bound.promise_set,
                spec=spec,
                consent_coverage=effective,
            )
            payload = {
                **rated,
                "rows": source_count,
                "requires_confirmation": bool(uncovered),
            }
            promise_hash = consented_set_hash(bound.promise_set)
            if uncovered:
                payload["claims"] = gate_claims_payload(
                    uncovered,
                    bound.resolution.facts,
                    bound.presentation,
                    has_cost_claim=any(
                        promise.field == "cost"
                        for promise in bound.promise_set.promises
                    ),
                )
                payload["promise_set_hash"] = promise_hash
            estimate = ActionEstimate.model_validate(payload).model_dump(
                mode="json", exclude_unset=True
            )
            if estimate.get("billed_cost") is None:
                raise PdfImportOcrApprovalError(
                    "the selected paid OCR route has no exact price"
                )
            return bound, estimate, promise_hash

        child_quotes = []
        for partition in child_partitions:
            row_pages = {
                int(row_id): int(page_count)
                for row_id, page_count in partition["row_pages"].items()
            }
            child_scope = {
                "schema_version": PDF_IMPORT_OCR_CHILD_SCOPE_SCHEMA,
                "parent_identity": str(binding["manifest_hash"]),
                "storage_identity": str(binding["storage_identity"]),
                "draft_id": str(binding["draft_id"]),
                "child_key": str(partition["child_key"]),
                "commit_ordinal_start": int(partition["commit_ordinal_start"]),
                "commit_ordinal_end": int(partition["commit_ordinal_end"]),
                "roster_digest": str(partition["roster_digest"]),
                "source_count": int(partition["source_count"]),
                "page_count": int(partition["page_count"]),
                "ocr_params_hash": work_scope["ocr_params_hash"],
            }
            child_bound, child_estimate, child_hash = rated_scope(
                child_scope["source_count"],
                child_scope["page_count"],
                child_scope,
                row_quantities=row_pages,
            )
            public_partition = {
                key: value for key, value in partition.items() if key != "row_pages"
            }
            child_quotes.append(
                {
                    **public_partition,
                    "scope": child_scope,
                    "promise_set_hash": child_hash,
                    "target": target_snapshot(child_bound.resolution),
                    "route": _route_snapshot(child_bound),
                    "promises": _compact_promise_rows(
                        promise_rows(child_bound.promise_set)
                    ),
                    "quote": ConsentQuote.from_estimate(child_estimate).model_dump(
                        mode="json"
                    ),
                }
            )

        child_costs = [row["quote"].get("cost") for row in child_quotes]
        if any(value is None for value in child_costs):
            raise PdfImportOcrApprovalError(
                "the selected paid OCR route has no exact provider price"
            )
        policy_ids = {str(row["quote"]["policy_id"]) for row in child_quotes}
        if len(policy_ids) != 1:
            raise PdfImportOcrApprovalError("OCR child pricing policy changed")
        billed_cost = sum(int(row["quote"]["billed_cost"]) for row in child_quotes)
        provider_cost = sum(Decimal(str(value)) for value in child_costs)

        resolved = resolve_scope(total_pages, work_scope)
        rated = scratch_estimate(resolved, quantity_name=None, quantity=total_pages)
        if remote_capability is not None:
            rated["remote_capability"] = remote_capability
        rated.update(
            {
                "rows": selected_count,
                "cost": float(provider_cost),
                "billed_cost": billed_cost,
                "policy_id": next(iter(policy_ids)),
            }
        )
        bound = _bind_rated_quote(resolved, rated)
        uncovered = project_uncovered_user_claims(
            project,
            bound.promise_set,
            spec=spec,
            consent_coverage=effective,
        )
        estimate_payload = {
            **rated,
            "rows": selected_count,
            "requires_confirmation": bool(uncovered),
        }
        approval_hash = consented_set_hash(bound.promise_set)
        if uncovered:
            estimate_payload["claims"] = gate_claims_payload(
                uncovered,
                bound.resolution.facts,
                bound.presentation,
                has_cost_claim=any(
                    promise.field == "cost" for promise in bound.promise_set.promises
                ),
            )
            estimate_payload["promise_set_hash"] = approval_hash
        estimate = ActionEstimate.model_validate(estimate_payload).model_dump(
            mode="json", exclude_unset=True
        )
        authorization = {
            "schema_version": PDF_IMPORT_OCR_APPROVAL_SCHEMA,
            "actor": effective.principal,
            "promise_set_hash": approval_hash,
            "scope_hash": str(binding["manifest_hash"]),
            "scope": work_scope,
            "ocr_action_params": action_params,
            "target": target_snapshot(bound.resolution),
            "route": _route_snapshot(bound),
            "promises": promise_rows(bound.promise_set),
            "quote": ConsentQuote.from_estimate(estimate_payload).model_dump(
                mode="json"
            ),
            "allocation": {
                "schema_version": PDF_IMPORT_OCR_ALLOCATION_SCHEMA,
                "roster": "effective_unmatched_ocr_selected_commit_order",
                "max_commit_occurrences_per_child": 256,
                "source_count": selected_count,
                "page_count": total_pages,
                "approved_billed_cost": billed_cost,
                "currency": "USD",
                "policy_id": next(iter(policy_ids)),
                "row_quote_authority": "canonical_commit_occurrences",
                "children": child_quotes,
            },
        }
        grant = PdfImportOcrGrant.from_mapping(authorization)
        return PdfImportOcrApprovalQuote(estimate, grant.as_mapping()).as_mapping()


__all__ = [
    "PDF_IMPORT_OCR_APPROVAL_SCHEMA",
    "PDF_IMPORT_OCR_ALLOCATION_SCHEMA",
    "PDF_IMPORT_OCR_CHILD_SCOPE_SCHEMA",
    "PDF_IMPORT_OCR_SCOPE_SCHEMA",
    "PdfImportOcrApprovalError",
    "PdfImportOcrApprovalQuote",
    "PdfImportOcrApprovalQuoter",
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
) -> PdfImportOcrApprovalQuote:
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
    recipe = ScratchActionRecipe("media.ocr", "ocr")
    resolved = resolve_for_action(
        project,
        spec,
        recipe,
        composition=composition,
        bounded_scratch_input=BoundedScratchInput(len(children), scope),
    )
    if isinstance(resolved, Refusal):
        raise PdfImportOcrApprovalError(resolved.remedy)
    if not isinstance(resolved, ResolvedExecution):
        raise PdfImportOcrApprovalError("OCR route is unavailable")
    rated = scratch_estimate(resolved, quantity_name=None, quantity=len(children))
    rated.update(
        cost=float(cost),
        billed_cost=sum(child["quote"]["billed_cost"] for child in children),
        policy_id=next(iter(policies)),
        rows=scope["source_count"],
    )
    bound = _bind_rated_quote(resolved, rated)
    effective = effective_consent_coverage(project, consent_coverage)
    uncovered = project_uncovered_user_claims(
        project, bound.promise_set, spec=spec, consent_coverage=effective
    )
    payload = {**rated, "requires_confirmation": bool(uncovered)}
    approval_hash = consented_set_hash(bound.promise_set)
    if uncovered:
        payload["claims"] = gate_claims_payload(
            uncovered,
            bound.resolution.facts,
            bound.presentation,
            has_cost_claim=any(p.field == "cost" for p in bound.promise_set.promises),
        )
        payload["promise_set_hash"] = approval_hash
    estimate = ActionEstimate.model_validate(payload).model_dump(
        mode="json", exclude_unset=True
    )
    quote = ConsentQuote.from_estimate(rated).model_dump(mode="json")
    allocation = {
        "schema_version": PDF_IMPORT_OCR_ALLOCATION_SCHEMA,
        "row_quote_authority": "admitted_source_pages",
        "currency": "USD",
        "policy_id": next(iter(policies)),
        "approved_billed_cost": quote["billed_cost"],
        "children": children,
        "requires_confirmation": bool(uncovered),
    }
    grant = PdfImportOcrGrant.from_mapping(
        {
            "schema_version": PDF_IMPORT_OCR_APPROVAL_SCHEMA,
            "actor": effective.principal,
            "promise_set_hash": approval_hash,
            "scope_hash": _source_page_hash(scope),
            "scope": scope,
            "ocr_action_params": dict(ocr_action_params),
            "target": target_snapshot(bound.resolution),
            "route": _route_snapshot(bound),
            "promises": promise_rows(bound.promise_set),
            "quote": quote,
            "allocation": allocation,
        }
    )
    return PdfImportOcrApprovalQuote(estimate, grant.as_mapping())


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
    if grant.quote.get("billed_cost") != sum(
        child["quote"]["billed_cost"] for child in children
    ) or grant.allocation.get("approved_billed_cost") != grant.quote.get("billed_cost"):
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
        TrustedConsentApproval,
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
        if (
            dict(context.spec) != child["spec"]
            or target_snapshot(resolved.resolution) != child["target"]
            or _route_snapshot(resolved) != child["route"]
            or promise_rows(resolved.promise_set) != child["promises"]
            or ConsentQuote.from_estimate(context.estimate).model_dump(mode="json")
            != child["quote"]
        ):
            return TrustedConsentRefusal(
                "source-page OCR page, settings, route or quote changed"
            )
        return TrustedConsentApproval("pdf_source_page_grant_derived")

    return authorize
