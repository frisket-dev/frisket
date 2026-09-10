"""Admission of completed, replay-valid semantic matches and review evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from frisket.actions.core import SemanticJoin, _ProjectAction
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.semantic_match_types import (
    SemanticJoinSource,
    SemanticMatch,
    SemanticMatchValues,
)
from frisket.actions.types import (
    ProjectScope,
    RowSource,
    RunBackfiller,
    SheetRows,
    TableError,
)
from frisket.contracts.action import ActionError, Receipt, ReceiptEvidence
from frisket.engine.executor.action_receipts import (
    _positive_ref_int,
    _receipt_ops_are_applied,
    _receipt_ref,
)
from frisket.engine.executor.embedding_read import TableReadRefused
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


def _semantic_origin(project: Project, receipt: Receipt) -> str | None:
    """Recognize the registered semantic producer and its exact durable run."""
    try:
        terminal = ACTION_REGISTRY.get(receipt.action_kind).definition.run
        origin = receipt.action_kind
        if isinstance(terminal, _ProjectAction) and terminal.capabilities == (
            RunBackfiller,
        ):
            sources = [
                item.ref
                for item in receipt.evidence
                if item.ref.get("kind") == "backfill_source_generation"
            ]
            if len(sources) != 1:
                return None
            source = sources[0]
            if _positive_ref_int(source, "successor_run_id") != receipt.run_id:
                return None
            origin = source.get("source_action_kind")
            terminal = ACTION_REGISTRY.get(origin).definition.run
        if not isinstance(terminal, SemanticJoin):
            return None
        run = project.db.execute(
            "SELECT action_kind,op_id FROM runs WHERE id=?", (receipt.run_id,)
        ).fetchone()
        if (
            run is None
            or run["action_kind"] != origin
            or run["op_id"] not in receipt.op_ids
        ):
            return None
        return origin
    except (KeyError, TypeError, ValueError):
        return None


def _resolve_semantic_matches(
    project: Project,
    *,
    receipt_id: str,
    row_ids: tuple[int, ...] | None,
    include_unmatched: bool,
) -> dict[str, Any] | ActionError:
    source_receipt = ReceiptStore(project).parsed_by_id(receipt_id)
    if source_receipt is None:
        return _derive_link_table_input_error(
            "source semantic join receipt does not exist",
            "params.source.receipt_id",
        )
    source_action_kind = _semantic_origin(project, source_receipt)
    if (
        source_action_kind is None
        or source_receipt.status != "completed"
        or not _receipt_ops_are_applied(project, source_receipt.op_ids)
    ):
        return _derive_link_table_input_error(
            "source receipt must be a completed, applied semantic-join receipt",
            "params.source.receipt_id",
            details={
                "receipt_id": receipt_id,
                "action_kind": source_receipt.action_kind,
                "status": source_receipt.status,
            },
        )
    from frisket.engine.executor.semantic_join_action import _replay_error

    join_replay_error = _replay_error(project, source_receipt)
    if join_replay_error is not None:
        return _derive_link_table_input_error(
            "source semantic-join receipt is stale",
            "params.source.receipt_id",
            details=join_replay_error.details,
        )

    source_ref = _receipt_ref(source_receipt, "semantic_join_source_sheet")
    target_ref = _receipt_ref(source_receipt, "semantic_join_target_sheet")
    source_column_ref = _receipt_ref(source_receipt, "semantic_join_source_column")
    target_column_ref = _receipt_ref(source_receipt, "semantic_join_target_column")
    if (
        source_ref is None
        or target_ref is None
        or source_column_ref is None
        or target_column_ref is None
    ):
        return _derive_link_table_input_error(
            "source receipt lacks semantic join sheet or column refs",
            "params.source.receipt_id",
        )

    output_refs = _semantic_join_output_refs_by_role(source_receipt)
    required_roles = {"match_value", "match_score", "target_row_id"}
    if set(output_refs) != required_roles:
        return _derive_link_table_input_error(
            "source receipt lacks required semantic join output refs",
            "params.source.receipt_id",
            details={"roles": sorted(output_refs)},
        )
    if any(
        "derive.link_table" not in (ref.get("may_feed") or [])
        for ref in output_refs.values()
    ):
        return _derive_link_table_input_error(
            "source receipt outputs are not allowed to feed derive.link_table",
            "params.source.receipt_id",
        )

    source_sheet_id = _positive_ref_int(source_ref, "sheet_id")
    target_sheet_id = _positive_ref_int(target_ref, "sheet_id")
    source_column_id = _positive_ref_int(source_column_ref, "column_id")
    target_column_id = _positive_ref_int(target_column_ref, "column_id")
    source_run_id = _positive_ref_int(source_ref, "run_id")
    if source_run_id != source_receipt.run_id or None in {
        source_sheet_id,
        target_sheet_id,
        source_column_id,
        target_column_id,
        source_run_id,
    }:
        return _derive_link_table_input_error(
            "source receipt contains invalid semantic join ids",
            "params.source.receipt_id",
        )

    source_row_ids = [
        int(row_id)
        for row_id in source_ref.get("row_ids", [])
        if isinstance(row_id, int) and row_id > 0
    ]
    if row_ids is not None:
        selected_row_ids = list(row_ids)
        missing_scope = sorted(set(selected_row_ids) - set(source_row_ids))
        if missing_scope:
            return _derive_link_table_input_error(
                "row_ids must come from the source join receipt",
                "scope.row_ids",
                details={"missing": missing_scope},
            )
    else:
        selected_row_ids = source_row_ids
    if not selected_row_ids:
        return _derive_link_table_input_error(
            "source receipt has no source rows to materialize",
            "params.source.receipt_id",
        )
    visible_rows = project.visible_row_ids(source_sheet_id, selected_row_ids)
    missing_visible = sorted(set(selected_row_ids) - set(visible_rows))
    if missing_visible:
        return _derive_link_table_input_error(
            "source rows are not visible on the join source sheet",
            "scope.row_ids",
            details={"missing": missing_visible},
        )

    column_error = _validate_derive_link_table_columns(
        project,
        source_sheet_id=source_sheet_id,
        target_sheet_id=target_sheet_id,
        source_column_id=source_column_id,
        target_column_id=target_column_id,
        output_refs=output_refs,
        source_run_id=source_run_id,
    )
    if column_error is not None:
        return column_error

    output_column_ids = {
        role: int(ref["column_id"]) for role, ref in output_refs.items()
    }
    source_values = project.get_values(
        source_sheet_id, source_column_id, row_ids=selected_row_ids
    )
    match_values = project.get_values(
        source_sheet_id, output_column_ids["match_value"], row_ids=selected_row_ids
    )
    score_values = project.get_values(
        source_sheet_id, output_column_ids["match_score"], row_ids=selected_row_ids
    )
    target_row_values = project.get_values(
        source_sheet_id, output_column_ids["target_row_id"], row_ids=selected_row_ids
    )
    missing_values = sorted(set(selected_row_ids) - set(source_values))
    if missing_values:
        return _derive_link_table_input_error(
            "source rows no longer resolve on the source sheet",
            "scope.row_ids",
            details={"missing": missing_values},
        )

    target_value_cache: dict[int, Any] = {}
    records: list[dict[str, Any]] = []
    parent_row_ids: list[int] = []
    for source_row_id in selected_row_ids:
        target_row_id = target_row_values.get(source_row_id)
        if target_row_id is None:
            if not include_unmatched:
                continue
            target_value = None
        else:
            if (
                not isinstance(target_row_id, int)
                or isinstance(target_row_id, bool)
                or target_row_id <= 0
            ):
                return _derive_link_table_input_error(
                    "target row id output must be a positive integer or null",
                    "params.source",
                    details={"source_row_id": source_row_id, "value": target_row_id},
                )
            target_value = _target_join_value(
                project,
                target_sheet_id=target_sheet_id,
                target_column_id=target_column_id,
                target_row_id=target_row_id,
                cache=target_value_cache,
            )
            if isinstance(target_value, ActionError):
                return target_value
        score = score_values.get(source_row_id)
        if score is not None and (
            not isinstance(score, (int, float)) or isinstance(score, bool)
        ):
            return _derive_link_table_input_error(
                "match score output must be numeric or null",
                "params.source",
                details={"source_row_id": source_row_id, "value": score},
            )
        record = {
            "source_row_id": source_row_id,
            "source_value": _link_table_text_value(source_values.get(source_row_id)),
            "target_row_id": target_row_id,
            "target_value": _link_table_text_value(target_value),
            "match_score": score,
            "match_value": _link_table_text_value(match_values.get(source_row_id)),
        }
        records.append(record)
        parent_row_ids.append(source_row_id)

    review_refs = _derive_link_table_review_refs(
        project,
        source_run_id=source_run_id,
        output_column_ids=set(output_column_ids.values()),
        selected_row_ids=selected_row_ids,
    )
    return {
        "source_receipt": source_receipt,
        "source_action_kind": source_action_kind,
        "source_receipt_id": source_receipt.receipt_id,
        "source_run_id": source_run_id,
        "source_op_ids": list(source_receipt.op_ids),
        "source_sheet_id": source_sheet_id,
        "source_sheet_name": source_ref.get("name"),
        "target_sheet_id": target_sheet_id,
        "target_sheet_name": target_ref.get("name"),
        "source_column_id": source_column_id,
        "source_column_name": source_column_ref.get("name"),
        "target_column_id": target_column_id,
        "target_column_name": target_column_ref.get("name"),
        "output_refs": output_refs,
        "output_column_ids": output_column_ids,
        "selected_row_ids": selected_row_ids,
        "observed_cells": {
            "source": source_values,
            "match_value": match_values,
            "match_score": score_values,
            "target_row_id": target_row_values,
        },
        "records": records,
        "parent_row_ids": parent_row_ids,
        "review_refs": review_refs,
    }


def _semantic_join_output_refs_by_role(receipt: Receipt) -> dict[str, dict[str, Any]]:
    refs: dict[str, dict[str, Any]] = {}
    for output in receipt.outputs:
        ref = output.ref
        if ref.get("kind") != "semantic_join_output_column":
            continue
        role = ref.get("role")
        if isinstance(role, str):
            refs[role] = dict(ref)
    return refs


def _validate_derive_link_table_columns(
    project: Project,
    *,
    source_sheet_id: int,
    target_sheet_id: int,
    source_column_id: int,
    target_column_id: int,
    output_refs: dict[str, dict[str, Any]],
    source_run_id: int,
) -> ActionError | None:
    from frisket.engine.store.result_generations import ResultGenerationStore

    generations = ResultGenerationStore(project)
    for sheet_id, column_id, label in (
        (source_sheet_id, source_column_id, "source column"),
        (target_sheet_id, target_column_id, "target column"),
    ):
        row = project.db.execute(
            "SELECT c.id FROM columns c JOIN sheets s ON s.id=c.sheet_id "
            "WHERE c.id=? AND c.sheet_id=? AND c.hidden=0 AND s.hidden=0",
            (column_id, sheet_id),
        ).fetchone()
        if row is None:
            return _derive_link_table_input_error(
                f"{label} from the source receipt is missing",
                "params.source.receipt_id",
                details={"sheet_id": sheet_id, "column_id": column_id},
            )
    for role, ref in output_refs.items():
        column_id = _positive_ref_int(ref, "column_id")
        sheet_id = _positive_ref_int(ref, "sheet_id")
        if (
            column_id is None
            or sheet_id != source_sheet_id
            or _positive_ref_int(ref, "run_id") != source_run_id
        ):
            return _derive_link_table_input_error(
                "semantic join output ref is invalid",
                "params.source.receipt_id",
                details={"role": role, "ref": ref},
            )
        row = project.db.execute(
            "SELECT id, name, type, current_run_id FROM columns "
            "WHERE id=? AND sheet_id=? AND hidden=0",
            (column_id, source_sheet_id),
        ).fetchone()
        if row is None:
            return _derive_link_table_input_error(
                "semantic join output column is missing",
                "params.source.receipt_id",
                details={"role": role, "column_id": column_id},
            )
        if row["name"] != ref.get("name") or row["type"] != ref.get("type"):
            return _derive_link_table_input_error(
                "semantic join output column changed",
                "params.source.receipt_id",
                details={
                    "role": role,
                    "column_id": column_id,
                    "expected_name": ref.get("name"),
                    "current_name": row["name"],
                    "expected_type": ref.get("type"),
                    "current_type": row["type"],
                },
            )
        # SDK replay above already proves every referenced managed cell still
        # belongs to this exact run. A backfill can publish new cell heads while
        # leaving the compatibility column pointer on the original generation.
        if (
            not generations.is_generation_managed(column_id)
            and row["current_run_id"] != source_run_id
        ):
            return _derive_link_table_input_error(
                "semantic join output column changed runs",
                "params.source.receipt_id",
                details={
                    "role": role,
                    "column_id": column_id,
                    "expected_run_id": source_run_id,
                    "current_run_id": row["current_run_id"],
                },
            )
    return None


def _target_join_value(
    project: Project,
    *,
    target_sheet_id: int,
    target_column_id: int,
    target_row_id: int,
    cache: dict[int, Any],
) -> Any | ActionError:
    if target_row_id not in cache:
        values = project.get_values(
            target_sheet_id, target_column_id, row_ids=[target_row_id]
        )
        if target_row_id not in values:
            return _derive_link_table_input_error(
                "target row id does not resolve on the target sheet",
                "params.source",
                details={"target_row_id": target_row_id},
            )
        cache[target_row_id] = values[target_row_id]
    return cache[target_row_id]


def _link_table_text_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _derive_link_table_review_refs(
    project: Project,
    *,
    source_run_id: int,
    output_column_ids: set[int],
    selected_row_ids: list[int],
) -> dict[str, Any]:
    selected = set(selected_row_ids)
    bodies = ReceiptStore(project).bodies_for_action_status(
        action_kind="review.decision",
        status="completed",
    )
    receipt_ids: list[str] = []
    reviewed_row_ids: list[int] = []
    decisions: list[dict[str, Any]] = []
    for body in bodies:
        receipt = Receipt.model_validate_json(body)
        target_ref = _receipt_ref(receipt, "target_result_cell")
        if target_ref is None:
            continue
        if (
            target_ref.get("run_id") != source_run_id
            or target_ref.get("column_id") not in output_column_ids
            or target_ref.get("row_id") not in selected
        ):
            continue
        if not _receipt_ops_are_applied(project, receipt.op_ids):
            continue
        row_id = int(target_ref["row_id"])
        receipt_ids.append(receipt.receipt_id)
        if row_id not in reviewed_row_ids:
            reviewed_row_ids.append(row_id)
        decisions.append(
            {
                "receipt_id": receipt.receipt_id,
                "row_id": row_id,
                "column_id": int(target_ref["column_id"]),
                "decision": (receipt.review or {}).get("decision"),
                "op_ids": list(receipt.op_ids),
            }
        )
    return {
        "kind": "semantic_join_review_decisions",
        "receipt_ids": receipt_ids,
        "reviewed_row_ids": reviewed_row_ids,
        "decisions": decisions,
    }


def _derive_link_table_input_error(
    message: str,
    field: str,
    *,
    details: dict[str, Any] | None = None,
) -> ActionError:
    return ActionError(
        code="invalid_input_ref",
        message=f"derive.link_table {message}",
        action_kind="derive.link_table",
        field=field,
        details=details or {},
    )


def _source_snapshot(resolved: dict[str, Any]) -> str:
    values = {key: value for key, value in resolved.items() if key != "source_receipt"}
    values["receipt_hash"] = resolved["source_receipt"].params_hash
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                values, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
    )


def validate_semantic_match_source(project: Project, fact: dict[str, Any]) -> None:
    try:
        selected = fact["requested_row_ids"]
        if (
            not isinstance(selected, list)
            or not selected
            or any(type(row) is not int or row <= 0 for row in selected)
            or len(set(selected)) != len(selected)
            or type(fact["include_unmatched"]) is not bool
        ):
            raise ValueError("invalid semantic read fact")
        source = SemanticJoinSource(
            kind="semantic_join", receipt_id=fact["source_receipt_id"]
        )
        resolved = _resolve_semantic_matches(
            project,
            receipt_id=source.receipt_id,
            row_ids=tuple(selected),
            include_unmatched=fact["include_unmatched"],
        )
    except (KeyError, TypeError, ValueError):
        raise TableError(
            "stale_replay", "The admitted semantic source is invalid"
        ) from None
    if isinstance(resolved, ActionError) or _source_snapshot(resolved) != fact.get(
        "snapshot_hash"
    ):
        raise TableError(
            "stale_replay", "The admitted semantic matches or reviews changed"
        )


class AdmittedSemanticMatchReader:
    def __init__(
        self, project: Project, *, scope: ProjectScope | SheetRows, action_kind: str
    ):
        self.project = project
        self.scope = scope
        self.action_kind = action_kind
        self.parent_sheet_id: int | None = None
        self.sources: set[RowSource] = set()
        self.source_roles: dict[RowSource, str] = {}
        self.facts: list[dict[str, Any]] = []
        self._matches: dict[RowSource, SemanticMatch] = {}
        self._values: dict[RowSource, dict[str, Any]] = {}
        self._reads: list[dict[str, Any]] = []

    def read(
        self, source: SemanticJoinSource, *, include_unmatched: bool = False
    ) -> tuple[SemanticMatch, ...]:
        source = SemanticJoinSource.model_validate(source)
        if type(include_unmatched) is not bool:
            raise TableError("invalid_params", "include_unmatched must be a boolean")
        selected = self.scope.row_ids if isinstance(self.scope, SheetRows) else None
        resolved = _resolve_semantic_matches(
            self.project,
            receipt_id=source.receipt_id,
            row_ids=selected,
            include_unmatched=include_unmatched,
        )
        if isinstance(resolved, ActionError):
            raise TableReadRefused(
                resolved.model_copy(update={"action_kind": self.action_kind})
            )
        if isinstance(self.scope, SheetRows) and (
            selected is None or self.scope.sheet_id != resolved["source_sheet_id"]
        ):
            raise TableError(
                "invalid_input_ref",
                "Select explicit rows on the semantic receipt's source sheet",
            )
        if self.parent_sheet_id not in (None, resolved["source_sheet_id"]):
            raise TableError(
                "invalid_input_ref", "Semantic matches must share a source sheet"
            )
        self.parent_sheet_id = resolved["source_sheet_id"]
        items = []
        for record in resolved["records"]:
            parent = RowSource(
                sheet_id=resolved["source_sheet_id"], row_id=record["source_row_id"]
            )
            target = (
                None
                if record["target_row_id"] is None
                else RowSource(
                    sheet_id=resolved["target_sheet_id"], row_id=record["target_row_id"]
                )
            )
            item = SemanticMatch(
                SemanticMatchValues.model_validate(record), parent, target
            )
            self._matches[parent] = item
            self._values[parent] = dict(record)
            self.sources.add(parent)
            self.source_roles[parent] = "edge_source"
            if target is not None:
                self.sources.add(target)
                self.source_roles[target] = "edge_target"
            items.append(item)
        self.facts.append(
            {
                "kind": "semantic_join_link_source",
                **{
                    key: resolved[key]
                    for key in (
                        "source_receipt_id",
                        "source_run_id",
                        "source_op_ids",
                        "source_sheet_id",
                        "source_sheet_name",
                        "target_sheet_id",
                        "target_sheet_name",
                        "source_column_id",
                        "source_column_name",
                        "target_column_id",
                        "target_column_name",
                    )
                },
                "source_action_kind": resolved["source_action_kind"],
                "requested_row_ids": resolved["selected_row_ids"],
                "source_row_ids": resolved["selected_row_ids"],
                "materialized_source_row_ids": resolved["parent_row_ids"],
                "target_row_ids": [row["target_row_id"] for row in resolved["records"]],
                "include_unmatched": include_unmatched,
                "snapshot_hash": _source_snapshot(resolved),
                "may_feed": ["export.work_log"],
            }
        )
        self._reads.append(resolved)
        return tuple(items)

    def validate_lineage(
        self, sources: tuple[RowSource, ...], parent: RowSource | None
    ) -> None:
        match = self._matches.get(parent)
        if match is None or set(sources) != (
            {match.source, match.target} if match.target is not None else {match.source}
        ):
            raise TableError(
                "invalid_input_ref",
                "Link rows must retain their admitted source and matching target",
            )

    def publication_evidence(self, write, lineages) -> list[ReceiptEvidence]:
        edges = [
            {
                "child_row_id": row_id,
                "parent_row_id": lineage.parent.row_id,
                **self._values[lineage.parent],
            }
            for row_id, lineage in zip(write.row_ids, lineages, strict=True)
        ]
        evidence = [
            ReceiptEvidence(
                ref={
                    "kind": "semantic_link_table_edges",
                    "sheet_id": write.sheet_id,
                    "child_row_ids": write.row_ids,
                    "source_row_ids": [edge["source_row_id"] for edge in edges],
                    "target_row_ids": [edge["target_row_id"] for edge in edges],
                    "edges": edges,
                    "op_id": write.op_id,
                },
                retention="pinned",
            )
        ]
        for read in self._reads:
            evidence.extend(
                [
                    ReceiptEvidence(
                        ref={
                            "kind": "semantic_join_output_cells",
                            "source_receipt_id": read["source_receipt_id"],
                            "run_id": read["source_run_id"],
                            "sheet_id": read["source_sheet_id"],
                            "row_ids": read["selected_row_ids"],
                            "output_columns": read["output_refs"],
                        },
                        retention="pinned",
                    ),
                    ReceiptEvidence(ref=read["review_refs"], retention="pinned"),
                ]
            )
        return evidence
