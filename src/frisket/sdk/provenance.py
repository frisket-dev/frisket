from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from frisket.contracts.action import (
    ActionResultStatus,
    Receipt,
    ReceiptIO,
    ReceiptRecord,
)
from frisket.sdk.capture import CapturedFacts


@dataclass
class Provenance:
    """The receipt parts an op presents from facts. `output_refs` feed both the
    receipt outputs and the host-built ActionOutputs.

    `named_result_refs` is the typed "list column -> child table" seam: array-typed
    generated columns advertise a feedable `named_result` ref (built via
    `frisket.executor.recordsets.feedable_named_result_ref`) that may feed
    derive.table_from_list. They appear as extra receipt outputs (keyed by `route`)
    and as extra `named_result` ActionOutputs, alongside the column outputs. A general
    axis (map.ner, map.extract); the host projects them so no op needs a write hook
    just to emit them. Empty for ops that produce no list columns."""

    inputs: list[ReceiptIO]
    output_refs: list[dict[str, Any]]
    provider_use: list[dict[str, Any]]
    records: list[ReceiptRecord]
    status: ActionResultStatus
    errors: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    named_result_refs: list[dict[str, Any]] = field(default_factory=list)


def build_receipt_from_provenance(
    provenance: Provenance,
    facts: CapturedFacts,
    *,
    receipt_id: str,
    project_id: str,
    action_id: str,
    action_kind: str,
    idempotency_key: str | None,
) -> Receipt:
    """Build the durable receipt envelope from neutral MapRunner facts."""

    warnings: list[str] = []
    seen_warnings: set[str] = set()
    for warning in [*provenance.warnings, *facts.warnings]:
        if warning in seen_warnings:
            continue
        seen_warnings.add(warning)
        warnings.append(warning)

    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action_kind,
        run_id=facts.run_id,
        change_ids=[facts.change_id],
        idempotency_key=idempotency_key,
        params_hash=facts.params_hash,
        status=provenance.status,
        inputs=provenance.inputs,
        outputs=[
            *(
                ReceiptIO(
                    name=ref["name"],
                    kind=("column" if ref.get("kind") == "map_result_column" else None),
                    ref=ref,
                )
                for ref in provenance.output_refs
            ),
            *(
                ReceiptIO(name=ref["route"], ref=ref)
                for ref in provenance.named_result_refs
            ),
        ],
        provider_use=provenance.provider_use,
        records=provenance.records,
        errors=provenance.errors,
        warnings=warnings,
    )
