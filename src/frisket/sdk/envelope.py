"""The standard receipt envelope: `receipt=` as declared fields, not a builder.

The Deliverable-0 audit verified a byte-uniform common core across all 28 ops:
an `input_rows` ReceiptIO + one `input_column.*` ReceiptIO per declared input
column (kind slugs derived from the action kind), mechanical `provider_use`
(model router split for model-backed ops, a single local entry otherwise), and
a bounded set of per-run evidence blocks. `StandardReceipt` presents that core
from `CapturedFacts`; everything op-specific is either a declared hook or a
`ctx.cite(...)` config-evidence block, so an op declares receipt FIELDS instead
of writing a `present()` builder.

Evidence order is canonical: author cites first (in call order), then
`field_roles`, `model_calls` (pinned) + `prompt` for model-backed ops, then
`run_counts`. Every evidence ref carries trailing `op_id`/`run_id`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from frisket.ai.models.metadata import model_calls_cost_actual
from frisket.contracts.action import ActionError, ReceiptEvidence, ReceiptIO
from frisket.sdk.capture import CapturedFacts
from frisket.sdk.declaration import Op
from frisket.sdk.provenance import Provenance


def row_failure_errors(
    action_kind: str,
    facts: CapturedFacts,
    status: str,
    *,
    code: str = "map_rows_failed",
) -> list[ActionError]:
    if status != "failed":
        return []
    return [
        ActionError(
            code=code,
            message=f"{action_kind} failed for every target row",
            action_kind=action_kind,
            details={
                "total_rows": facts.total_rows,
                "completed_rows": facts.completed_rows,
                "failed_rows": facts.failed_rows,
            },
        )
    ]


class CiteContext:
    """Handed to the op's `cites=` hook. `ctx.cite(kind, **payload)` appends one
    per-run evidence block; the kind is namespaced by the op automatically and
    the payload gains trailing `op_id`/`run_id`."""

    def __init__(self, decl: Op, project: Any, facts: CapturedFacts) -> None:
        self.project = project
        self.facts = facts
        self._decl = decl
        self._evidence: list[ReceiptEvidence] = []

    def cite(
        self, kind: str, *, retention: str = "compactable", **payload: Any
    ) -> None:
        ref = {
            "kind": f"{self._decl.underscored}_{kind}",
            **payload,
            "op_id": self.facts.op_id,
            "run_id": self.facts.run_id,
        }
        self._evidence.append(ReceiptEvidence(ref=ref, retention=retention))


def model_run_status(facts: CapturedFacts) -> str:
    total, failed = facts.total_rows, facts.failed_rows
    if total > 0 and failed >= total:
        return "failed"
    if failed > 0 or facts.run_status == "partial":
        return "partial"
    if facts.run_status == "failed":
        return "failed"
    return "completed"


@dataclass(frozen=True)
class StandardReceipt:
    """Declared receipt fields for an envelope-shaped op.

    - `cites`: `(ctx, facts) -> None` hook adding per-run config-evidence
      blocks via `ctx.cite(kind, **payload, retention=...)`.
    - `run_counts`: emit the canonical run-counts evidence block
      (`total_rows/completed_rows/failed_rows/failed_row_ids/result_count/
      model_call_count/cost_actual`); `run_counts_extra(project, facts)`
      merges op-specific keys (e.g. clean_dates' unparseable_rows) before the
      trailing ids.
    - `field_roles`: `(name, facts) -> role`; adds `role` + `schema` to every
      output ref and emits the canonical `<kind>_field_roles` evidence block.
    - `output_ref_extra`: `(project, facts, base_ref) -> dict` merged into each
      output ref after the mechanical fields.
    - `input_ref_extra`: `(name, facts) -> dict` merged into each
      `input_column.*` ref (e.g. judge's judged_output/source role).
    - `prompt_extra`: `(facts) -> dict` merged into the model prompt evidence
      payload (model-backed ops only).
    - `provider_use`: `(project, facts) -> list[dict]` override for ops whose
      provider split is neither the model-router one nor the single local entry.
    - `status`: `(facts) -> str` override of the derived status.
    - `named_results`: `(project, facts) -> list[dict]` feedable named-result
      refs (list column -> child table seam).
    """

    cites: Callable[[CiteContext, CapturedFacts], None] | None = None
    run_counts: bool = True
    run_counts_extra: Callable[[Any, CapturedFacts], dict[str, Any]] | None = None
    field_roles: Callable[[str, CapturedFacts], str] | None = None
    role_evidence: bool = True
    output_ref_extra: (
        Callable[[Any, CapturedFacts, dict[str, Any]], dict[str, Any]] | None
    ) = None
    input_ref_extra: Callable[[str, CapturedFacts], dict[str, Any]] | None = None
    prompt_extra: Callable[[CapturedFacts], dict[str, Any]] | None = None
    provider_use: Callable[[Any, CapturedFacts], list[dict[str, Any]]] | None = None
    status: Callable[[CapturedFacts], str] | None = None
    errors: Callable[[CapturedFacts, str], list[ActionError]] | None = None
    named_results: (
        Callable[[Any, CapturedFacts, list[dict[str, Any]]], list[dict[str, Any]]]
        | None
    ) = None

    def bind(
        self, decl: Op
    ) -> Callable[[Any, CapturedFacts, list[dict[str, Any]]], Provenance]:
        def present(
            project: Any, facts: CapturedFacts, base_output_refs: list[dict[str, Any]]
        ) -> Provenance:
            return self._present(decl, project, facts, base_output_refs)

        return present

    # --- assembly ---------------------------------------------------------

    def _present(
        self,
        decl: Op,
        project: Any,
        facts: CapturedFacts,
        base_output_refs: list[dict[str, Any]],
    ) -> Provenance:
        output_refs = self._output_refs(decl, project, facts, base_output_refs)
        status = (
            self.status(facts)
            if self.status is not None
            else (
                model_run_status(facts)
                if decl.model_backed
                else ("partial" if facts.failed_rows else "completed")
            )
        )
        return Provenance(
            inputs=self._inputs(decl, facts),
            output_refs=output_refs,
            provider_use=self._provider_use(decl, project, facts),
            evidence=self._evidence(decl, project, facts, output_refs),
            status=status,
            errors=self._errors(decl, facts, status),
            named_result_refs=(
                self.named_results(project, facts, output_refs)
                if self.named_results is not None
                else []
            ),
        )

    def _inputs(self, decl: Op, facts: CapturedFacts) -> list[ReceiptIO]:
        inputs = [
            ReceiptIO(
                name="input_rows",
                ref={
                    "kind": f"{decl.underscored}_input_rows",
                    "sheet_id": facts.sheet_id,
                    "row_ids": facts.row_ids,
                    "op_id": facts.op_id,
                    "run_id": facts.run_id,
                },
            )
        ]
        column_kind = f"{decl.underscored}_input_column"
        if decl.rich_input_columns:
            for col in facts.input_columns_rich:
                ref = {
                    "kind": column_kind,
                    "sheet_id": facts.sheet_id,
                    "column_id": col["column_id"],
                    "name": col["name"],
                    "type": col["type"],
                    "ai_generated": col["ai_generated"],
                    "source_run_id": col["source_run_id"],
                    "source_receipt_id": col["source_receipt_id"],
                }
                if self.input_ref_extra is not None:
                    ref.update(self.input_ref_extra(col["name"], facts))
                inputs.append(ReceiptIO(name=f"input_column.{col['name']}", ref=ref))
            return inputs
        for name, column_id in facts.input_column_ids.items():
            ref: dict[str, Any] = {
                "kind": column_kind,
                "sheet_id": facts.sheet_id,
                "column_id": column_id,
                "name": name,
            }
            if decl.input_ref_includes_type:
                ref["type"] = facts.input_column_types.get(name)
            if self.input_ref_extra is not None:
                ref.update(self.input_ref_extra(name, facts))
            inputs.append(ReceiptIO(name=f"input_column.{name}", ref=ref))
        return inputs

    def _output_refs(
        self,
        decl: Op,
        project: Any,
        facts: CapturedFacts,
        base_output_refs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for base, fact in zip(base_output_refs, facts.output_facts):
            ref = dict(base)
            if self.field_roles is not None:
                ref["role"] = self.field_roles(base["name"], facts)
                ref["schema"] = fact.field.get("schema") or {}
            if self.output_ref_extra is not None:
                ref.update(self.output_ref_extra(project, facts, base))
            refs.append(ref)
        return refs

    def _provider_use(
        self, decl: Op, project: Any, facts: CapturedFacts
    ) -> list[dict[str, Any]]:
        if self.provider_use is not None:
            return self.provider_use(project, facts)
        if decl.model_backed:
            from frisket.engine.executor.action_support import _model_call_provider_use

            return _model_call_provider_use(
                facts.model_calls, model=facts.model, run=facts.run
            )
        return [
            {
                "provider": "local",
                "service": f"frisket.{decl.slug}",
                "external_api": False,
                "cost_actual": facts.cost_actual,
            }
        ]

    def _evidence(
        self,
        decl: Op,
        project: Any,
        facts: CapturedFacts,
        output_refs: list[dict[str, Any]],
    ) -> list[ReceiptEvidence]:
        receipt_cost_actual: float | None = facts.cost_actual
        if decl.model_backed:
            receipt_cost_actual = model_calls_cost_actual(facts.model_calls)
        ctx = CiteContext(decl, project, facts)
        if self.cites is not None:
            self.cites(ctx, facts)
        evidence = list(ctx._evidence)
        if self.field_roles is not None and self.role_evidence:
            evidence.append(
                ReceiptEvidence(
                    ref={
                        "kind": f"{decl.underscored}_field_roles",
                        "fields": [
                            {
                                "name": ref["name"],
                                "role": ref["role"],
                                "column_id": ref["column_id"],
                                "type": ref["type"],
                            }
                            for ref in output_refs
                        ],
                        "op_id": facts.op_id,
                        "run_id": facts.run_id,
                    }
                )
            )
        if decl.model_backed:
            evidence.append(
                ReceiptEvidence(
                    ref={
                        "kind": f"{decl.underscored}_model_calls",
                        "model_call_ids": facts.model_call_ids,
                        "model_call_count": len(facts.model_call_ids),
                        "cost_actual": receipt_cost_actual,
                        "op_id": facts.op_id,
                        "run_id": facts.run_id,
                    },
                    retention="pinned",
                )
            )
            prompt_ref: dict[str, Any] = {
                "kind": f"{decl.underscored}_prompt",
                "prompt_hash": facts.prompt_hash,
                "model": facts.model,
            }
            if self.prompt_extra is not None:
                prompt_ref.update(self.prompt_extra(facts))
            prompt_ref["op_id"] = facts.op_id
            prompt_ref["run_id"] = facts.run_id
            evidence.append(ReceiptEvidence(ref=prompt_ref))
        if self.run_counts:
            counts: dict[str, Any] = {
                "kind": f"{decl.underscored}_run_counts",
                "total_rows": facts.total_rows,
                "completed_rows": facts.completed_rows,
                "failed_rows": facts.failed_rows,
                "failed_row_ids": facts.failed_row_ids,
                "result_count": len(facts.row_ids),
                "model_call_count": len(facts.model_call_ids),
                "cost_actual": receipt_cost_actual,
            }
            if self.run_counts_extra is not None:
                counts.update(self.run_counts_extra(project, facts))
            counts["op_id"] = facts.op_id
            counts["run_id"] = facts.run_id
            evidence.append(ReceiptEvidence(ref=counts))
        return evidence

    def _errors(self, decl: Op, facts: CapturedFacts, status: str) -> list[ActionError]:
        if self.errors is not None:
            return self.errors(facts, status)
        if not decl.model_backed or status != "failed":
            return []
        return [
            ActionError(
                code=decl.map_error_code,
                message=f"{decl.kind} failed for every target row",
                action_kind=decl.kind,
                details={
                    "total_rows": facts.total_rows,
                    "completed_rows": facts.completed_rows,
                    "failed_rows": facts.failed_rows,
                },
            )
        ]
