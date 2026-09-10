"""Host projection of the closed column, named-result and evidence routes."""

from __future__ import annotations

from frisket.contracts.action import ReceiptEvidence
from frisket.engine.executor.recordsets import (
    DERIVE_TABLE_FROM_LIST,
    feedable_named_result_ref,
)


def project_routed_outputs(plan, facts, refs, *, project):
    fields = plan.program._resolved_output_fields
    by_name = {ref["name"]: ref for ref in refs}
    columns, named, evidence = [], [], []
    failed = set(facts.failed_row_ids)
    successful_rows = [row_id for row_id in facts.row_ids if row_id not in failed]
    for field in fields:
        base = by_name[plan.output_names[field.key]]
        if field.route is None:
            columns.append(base)
            if field.named_result is not None:
                from frisket.sdk.media import media_successful_result_row_ids

                named.append(
                    feedable_named_result_ref(
                        source_action_kind=plan.action.action_id,
                        sheet_id=base["sheet_id"],
                        column_id=base["column_id"],
                        run_id=base["run_id"],
                        op_id=base["op_id"],
                        route=base["name"],
                        schema_name=field.named_result.schema_name,
                        schema_json=dict(field.schema),
                        row_ids=media_successful_result_row_ids(
                            project,
                            run_id=base["run_id"],
                            column_id=base["column_id"],
                            row_ids=facts.row_ids,
                        ),
                        may_feed=field.named_result.may_feed,
                        extra={"output_key": field.key},
                    )
                )
            continue
        projection = field.route
        route, target = projection.route, projection.route.target
        if field.hidden:
            evidence.append(
                ReceiptEvidence(ref={**base, "kind": "typed_hidden_output"})
            )
        if target.kind == "column":
            columns.append(
                {
                    **base,
                    "role": "map_result_column",
                    "route": route.name,
                    "path": route.path,
                }
            )
        elif target.kind == "named_result":
            may_feed = list(target.may_feed)
            if projection.schema.get("type") != "array":
                may_feed = [item for item in may_feed if item != DERIVE_TABLE_FROM_LIST]
            named.append(
                feedable_named_result_ref(
                    source_action_kind=plan.action.action_id,
                    sheet_id=base["sheet_id"],
                    column_id=base["column_id"],
                    run_id=base["run_id"],
                    op_id=base["op_id"],
                    route=route.name,
                    schema_name=target.schema_name,
                    schema_json=projection.schema,
                    row_ids=successful_rows,
                    may_feed=may_feed,
                    path=route.path,
                )
            )
        else:
            evidence.append(
                ReceiptEvidence(
                    ref={
                        "kind": "map_python_receipt_evidence",
                        "route": route.name,
                        "path": route.path,
                        "sheet_id": facts.sheet_id,
                        "column_id": base["column_id"],
                        "retention": target.retention,
                        "op_id": facts.op_id,
                        "run_id": facts.run_id,
                    },
                    retention=target.retention,
                )
            )
    return columns, named, evidence
