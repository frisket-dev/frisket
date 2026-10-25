"""Read-only context and admission facts for generic action comparison.

The browser owns temporary sessions.  This service only reconstructs one
canonical seed, reads a bounded current sample, and revalidates the same facts
before the existing preview executor may spend or start work.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from frisket.actions.core import ActionComparisonKind
from frisket.actions.system import typed_action_for_request
from frisket.actions.types import ActionRequest, discover_references
from frisket.authoring.workbench.installed_actions import bind_installed_action
from frisket.contracts.http.action_compare import (
    ActionCompareContextRequest,
    ActionDraft,
)
from frisket.engine.executor import resolve_map_preview
from frisket.engine.executor.actions import MapPreviewPlan
from frisket.engine.executor.map_rows_action import (
    bound_typed_program_request_from_runner_spec,
    normalized_typed_request_identity,
    typed_request_hash,
)
from frisket.engine.runner.preview import PREVIEW_MAX_ROWS
from frisket.engine.runner.result_generations import _compatibility_key
from frisket.engine.store.current_value_windows import (
    PREVIEW_CODEPOINT_LIMIT,
    current_text_metadata,
    read_current_column,
    read_current_text,
)
from frisket.engine.store.receipts import FINISHED_RECEIPT_STATUSES, ReceiptStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.retrieval_lifecycle import read_binding_revisions
from frisket.server.route_errors import RouteError
from frisket.server.workspace import Workspace


ACTION_COMPARE_SCHEMA_VERSION = "frisket.action_compare_context.v1"
MAX_CHALLENGERS = 3
COMPARE_CONTEXT_AGGREGATE_CELL_BYTE_LIMIT = 512 * 1024


class ActionCompareRouteError(RouteError):
    pass


class ActionCompareService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def context(self, project_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        try:
            request = ActionCompareContextRequest.model_validate(raw)
        except ValidationError as exc:
            raise ActionCompareRouteError(400, str(exc)) from exc
        project = self._workspace.get(project_id)
        if request.source.kind == "draft":
            seed_draft = request.source.draft.model_dump(mode="json", exclude_none=True)
            bound, draft, plan = _resolve_draft(project, seed_draft)
            basis: dict[str, Any] = {"kind": "draft", "draft": draft}
            baseline_mode = "preview"
            bindings = []
        else:
            bound, draft, plan, bindings = _resolve_run_draft(
                project,
                run_id=request.source.run_id,
                column_id=request.source.column_id,
            )
            basis = {
                "kind": "run",
                "run_id": request.source.run_id,
                "column_id": request.source.column_id,
            }
            baseline_mode = "current_values"

        sheet_id = int(draft["scope"]["sheet_id"])
        sample_row_ids = _require_sample_rows(
            project, sheet_id, list(request.sample_row_ids)
        )
        comparison = bound.action.definition.comparison
        available = (
            comparison is not None
            and comparison.kind is ActionComparisonKind.ROW_OVERLAY
            and isinstance(plan, MapPreviewPlan)
        )
        reason = None
        if not available:
            reason = {
                "code": "comparison_not_supported",
                "message": "This action is not enabled for row comparison.",
            }

        source_columns = _source_columns(project, bound, plan)
        output_columns = _output_columns(plan, bindings)
        replacement_targets = [
            {
                "logical_key": output["logical_key"],
                "current_name": output["current_name"],
                "column_id": output["column_id"],
            }
            for output in output_columns
            if output["column_id"] is not None
        ]
        locked_params = sorted(
            {
                *(
                    str(item["param"])
                    for item in (
                        bound.action.catalog_entry()["ui_hints"].get(
                            "source_requirements"
                        )
                        or []
                    )
                ),
                *(comparison.structural_params if comparison is not None else ()),
            }
        )
        output_signature = _output_signature(output_columns)
        source_rows, baseline_rows, origin_summary, sample_fingerprint = (
            _sample_payload(
                project,
                sheet_id=sheet_id,
                row_ids=sample_row_ids,
                source_columns=source_columns,
                output_columns=output_columns,
                include_baseline=baseline_mode == "current_values",
                baseline_seed_run_id=(
                    request.source.run_id if request.source.kind == "run" else None
                ),
            )
        )
        sheet = project.db.execute(
            "SELECT id,name FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
        ).fetchone()
        if sheet is None:
            raise ActionCompareRouteError(404, "comparison sheet was not found")
        return {
            "schema_version": ACTION_COMPARE_SCHEMA_VERSION,
            "available": available,
            "reason": reason,
            "limits": {
                "max_sample_rows": PREVIEW_MAX_ROWS,
                "max_challengers": MAX_CHALLENGERS,
            },
            "baseline_mode": baseline_mode,
            "comparison_basis": basis,
            "draft": draft,
            "sheet": {"id": sheet_id, "name": str(sheet["name"])},
            "sample_row_ids": sample_row_ids,
            "sample_fingerprint": sample_fingerprint,
            "locked_params": locked_params,
            "source_columns": source_columns,
            "output_columns": output_columns,
            "expected_output_signature": output_signature,
            "replacement_targets": replacement_targets,
            "source_rows": source_rows,
            "baseline_rows": baseline_rows,
            "origin_summary": origin_summary,
        }

    def validate_preview_admission(
        self,
        project_id: str,
        *,
        action: Mapping[str, Any],
        comparison: Mapping[str, Any],
        plan: MapPreviewPlan,
    ) -> dict[str, Any] | None:
        """Return a public refusal before prepare_preview/receipt/provider work."""

        scope = action.get("scope")
        row_ids = scope.get("row_ids") if isinstance(scope, Mapping) else None
        if not isinstance(row_ids, list) or not row_ids:
            return _refusal(
                "comparison_sample_required",
                "Action comparison requires the explicit bounded context sample.",
            )
        try:
            context = self.context(
                project_id,
                {
                    "source": comparison["basis"],
                    "sample_row_ids": row_ids,
                },
            )
        except (KeyError, TypeError, ValueError, ActionCompareRouteError) as exc:
            return _refusal("invalid_comparison_basis", str(exc))
        if not context["available"]:
            return _refusal(context["reason"]["code"], context["reason"]["message"])
        if action.get("action_id") != context["draft"]["action_id"]:
            return _refusal(
                "comparison_action_mismatch",
                "Every comparison variant must use the same action family.",
            )
        if (
            comparison.get("expected_sample_fingerprint")
            != context["sample_fingerprint"]
        ):
            return _refusal(
                "comparison_stale",
                "The comparison sample changed; refresh it before running.",
            )
        candidate_outputs = _output_columns(plan, [])
        candidate_signature = _output_signature(candidate_outputs)
        if (
            comparison.get("expected_output_signature")
            != context["expected_output_signature"]
            or candidate_signature != context["expected_output_signature"]
        ):
            return _refusal(
                "comparison_output_mismatch",
                "These settings change the comparison output structure.",
            )
        if dict(plan.runner_spec.get("output_names") or {}) != dict(
            context["draft"]["output_names"]
        ):
            return _refusal(
                "comparison_output_mismatch",
                "These settings change the comparison output names.",
            )
        source_params = {
            str(item["param"])
            for item in (
                typed_action_for_request(
                    {
                        **context["draft"],
                        "idempotency_key": "action-compare-admission",
                    }
                )
                .action.catalog_entry()["ui_hints"]
                .get("source_requirements")
                or []
            )
        }
        candidate_params = dict(plan.runner_spec.get("params") or {})
        if any(
            candidate_params.get(name) != context["draft"]["params"].get(name)
            for name in source_params
        ):
            return _refusal(
                "comparison_source_mismatch",
                "These settings change the comparison source inputs.",
            )
        expected_sources = [
            (item["column_id"], item["name"]) for item in context["source_columns"]
        ]
        candidate_sources = sorted(
            (int(column_id), str(name))
            for name, column_id in plan.source_column_ids.items()
        )
        if sorted(expected_sources) != candidate_sources:
            return _refusal(
                "comparison_source_mismatch",
                "These settings change the comparison source columns.",
            )
        if list(plan.runner_spec.get("row_ids") or ()) != context["sample_row_ids"]:
            return _refusal(
                "comparison_sample_mismatch",
                "Every comparison variant must use the same sample rows.",
            )
        return None


def _resolve_draft(project, raw_draft: Mapping[str, Any]):
    try:
        draft_model = ActionDraft.model_validate(raw_draft)
        request = ActionRequest.model_validate(
            {
                **draft_model.model_dump(mode="json", exclude_none=True),
                "idempotency_key": "action-compare-context",
            }
        )
        bound = bind_installed_action(project, request)
        if bound is None:
            bound = typed_action_for_request(request.model_dump(mode="json"))
        canonical = normalized_typed_request_identity(bound)
        canonical.pop("replace_existing", None)
        canonical.pop("expected_output_columns", None)
        canonical.pop("implementation_identity", None)
        body = {**canonical, "idempotency_key": "action-compare-context"}
        plan = resolve_map_preview(project, body)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ActionCompareRouteError(
            409, "This action draft cannot be compared with the current action."
        ) from exc
    if not isinstance(plan, MapPreviewPlan):
        message = getattr(plan, "message", "This action does not support row preview.")
        raise ActionCompareRouteError(409, message)
    canonical["output_names"] = dict(plan.runner_spec.get("output_names") or {})
    canonical["scope"] = draft_model.scope.model_dump(mode="json", exclude_none=True)
    return bound, canonical, plan


def _resolve_run_draft(project, *, run_id: int, column_id: int):
    run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise ActionCompareRouteError(404, "comparison run was not found")
    bindings = ResultGenerationStore(project).bindings_for_run(run_id)
    if not bindings:
        raise ActionCompareRouteError(
            409, "Legacy runs without output-family bindings cannot be compared."
        )
    if column_id not in {binding.column_id for binding in bindings}:
        raise ActionCompareRouteError(404, "selected column is not part of this run")
    try:
        runner_spec = json.loads(run["params"] or "{}")
        if not isinstance(runner_spec, dict):
            raise ValueError("stored run spec is not an object")
        bound = bound_typed_program_request_from_runner_spec(
            runner_spec, project=project
        )
        if bound is None:
            raise ValueError("unsupported action family")
        receipt = ReceiptStore(project).latest_for_run_action_statuses(
            run_id, bound.action.action_id, FINISHED_RECEIPT_STATUSES
        )
        if receipt is None or receipt.params_hash != typed_request_hash(bound):
            raise ValueError("stored action identity does not match its producer")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ActionCompareRouteError(
            409, "This run cannot be reconstructed with the current action."
        ) from exc

    columns = {
        int(column["id"]): column
        for column in project.columns(int(run["sheet_id"]), include_hidden=True)
    }
    if any(binding.column_id not in columns for binding in bindings):
        raise ActionCompareRouteError(
            409, "A comparison target was retired or removed."
        )
    stored_names = runner_spec.get("output_names")
    if not isinstance(stored_names, dict):
        raise ActionCompareRouteError(409, "Stored output names are unavailable.")
    by_role = {binding.output_role: binding for binding in bindings}
    if set(by_role) != {str(role) for role in stored_names.values()}:
        raise ActionCompareRouteError(409, "Stored output bindings are incoherent.")
    current_names = {
        str(logical): str(columns[by_role[str(role)].column_id]["name"])
        for logical, role in stored_names.items()
    }
    canonical = normalized_typed_request_identity(bound)
    canonical.pop("replace_existing", None)
    canonical.pop("expected_output_columns", None)
    canonical.pop("implementation_identity", None)
    canonical["output_names"] = current_names
    canonical["scope"] = {
        "kind": "sheet_rows",
        "sheet_id": int(run["sheet_id"]),
    }
    rebound, canonical, plan = _resolve_draft(project, canonical)
    plan_by_name = {str(field["name"]): field for field in plan.output_fields}
    for role, binding in by_role.items():
        field = plan_by_name.get(str(columns[binding.column_id]["name"]))
        column = columns[binding.column_id]
        if (
            field is None
            or str(field["column_type"]) != str(column["type"])
            or field.get("format") != column["format"]
            or bool(field.get("hidden", False)) != bool(column["hidden"])
            or _compatibility_key(field=field) != binding.compatibility_key
        ):
            raise ActionCompareRouteError(
                409, "The current output descriptors no longer match this run."
            )
    bindings_by_logical = {
        str(logical): by_role[str(role)] for logical, role in stored_names.items()
    }
    return rebound, canonical, plan, bindings_by_logical


def _require_sample_rows(project, sheet_id: int, row_ids: list[int]) -> list[int]:
    if len(row_ids) > PREVIEW_MAX_ROWS:
        raise ActionCompareRouteError(400, "comparison sample exceeds the preview cap")
    visible = project.visible_row_ids(sheet_id, row_ids)
    missing = sorted(set(row_ids) - set(visible))
    if missing:
        raise ActionCompareRouteError(
            400, f"comparison sample contains rows outside the sheet: {missing}"
        )
    if len(set(row_ids)) != len(row_ids):
        raise ActionCompareRouteError(400, "comparison sample rows must be unique")
    return visible


def _source_columns(project, bound, plan: MapPreviewPlan) -> list[dict[str, Any]]:
    catalog = bound.action.catalog_entry()["ui_hints"]
    requirements = catalog.get("source_requirements") or []
    params_by_column: dict[str, str] = {}
    for requirement in requirements:
        param = str(requirement["param"])
        value = getattr(bound.params, param)
        for reference in discover_references(value):
            params_by_column.setdefault(reference.column, param)
    columns_by_name = {
        str(column["name"]): column
        for column in project.columns(
            int(plan.runner_spec["sheet_id"]), include_hidden=True
        )
    }
    return [
        {
            "param": params_by_column.get(name, name),
            "column_id": int(column_id),
            "name": str(name),
            "column_type": str(columns_by_name[name]["type"]),
            "format": columns_by_name[name]["format"],
        }
        for name, column_id in plan.source_column_ids.items()
    ]


def _output_columns(plan: MapPreviewPlan, bindings) -> list[dict[str, Any]]:
    names = dict(plan.runner_spec.get("output_names") or {})
    logical_by_name = {str(name): str(logical) for logical, name in names.items()}
    bindings_by_logical = dict(bindings) if isinstance(bindings, Mapping) else {}
    outputs = []
    for field in plan.output_fields:
        current_name = str(field["name"])
        logical = logical_by_name.get(current_name, current_name)
        binding = bindings_by_logical.get(logical)
        outputs.append(
            {
                "logical_key": logical,
                "column_id": None if binding is None else binding.column_id,
                "current_name": current_name,
                "column_type": str(field["column_type"]),
                "format": field.get("format"),
                "hidden": bool(field.get("hidden", False)),
                "schema": dict(field.get("schema") or {}),
            }
        )
    return outputs


def _strip_descriptions(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_descriptions(child)
            for key, child in value.items()
            if key != "description"
        }
    if isinstance(value, list):
        return [_strip_descriptions(item) for item in value]
    return value


def _output_signature(outputs: list[dict[str, Any]]) -> str:
    semantic = [
        {
            "logical_key": output["logical_key"],
            "column_type": output["column_type"],
            "format": output["format"],
            "hidden": output["hidden"],
            "schema": _strip_descriptions(output["schema"]),
        }
        for output in outputs
    ]
    return _digest(sorted(semantic, key=lambda item: item["logical_key"]))


def _sample_payload(
    project,
    *,
    sheet_id: int,
    row_ids: list[int],
    source_columns: list[dict[str, Any]],
    output_columns: list[dict[str, Any]],
    include_baseline: bool,
    baseline_seed_run_id: int | None,
):
    source_rows = {str(row_id): {} for row_id in row_ids}
    baseline_rows = (
        {str(row_id): {} for row_id in row_ids} if include_baseline else None
    )
    mixed_run_ids: set[int] = set()
    includes_manual_edits = False
    includes_reject_clear = False
    fingerprint_columns = [*source_columns]
    if include_baseline:
        fingerprint_columns.extend(
            output for output in output_columns if output["column_id"] is not None
        )
    descriptor_facts = []
    aggregate_bytes = 0
    with project.read_snapshot() as snapshot:
        pairs = [
            (row_id, int(column["column_id"]))
            for column in fingerprint_columns
            for row_id in row_ids
        ]
        revisions = read_binding_revisions(snapshot.db, pairs)
        for column in fingerprint_columns:
            descriptor_facts.append(
                (
                    int(column["column_id"]),
                    column.get("name", column.get("current_name")),
                    column["column_type"],
                    column.get("format"),
                )
            )
        for column in source_columns:
            reads = read_current_column(
                snapshot.db,
                sheet_id=sheet_id,
                column_id=int(column["column_id"]),
                row_ids=row_ids,
                bounded=True,
            )
            for row_id in row_ids:
                cell = _compare_cell(
                    snapshot.db, reads[row_id], row_id, int(column["column_id"])
                )
                cell, aggregate_bytes = _bounded_aggregate_cell(
                    snapshot.db,
                    cell,
                    sheet_id=sheet_id,
                    row_id=row_id,
                    column_id=int(column["column_id"]),
                    aggregate_bytes=aggregate_bytes,
                )
                source_rows[str(row_id)][column["name"]] = cell
        if include_baseline and baseline_rows is not None:
            for output in output_columns:
                column_id = int(output["column_id"])
                reads = read_current_column(
                    snapshot.db,
                    sheet_id=sheet_id,
                    column_id=column_id,
                    row_ids=row_ids,
                    bounded=True,
                    include_hidden=True,
                )
                for row_id in row_ids:
                    cell = _compare_cell(snapshot.db, reads[row_id], row_id, column_id)
                    cell, aggregate_bytes = _bounded_aggregate_cell(
                        snapshot.db,
                        cell,
                        sheet_id=sheet_id,
                        row_id=row_id,
                        column_id=column_id,
                        aggregate_bytes=aggregate_bytes,
                        include_hidden=bool(output["hidden"]),
                    )
                    baseline_rows[str(row_id)][output["logical_key"]] = cell
                    origin = cell["origin"]
                    if origin["run_id"] is not None:
                        mixed_run_ids.add(int(origin["run_id"]))
                    includes_manual_edits |= origin["kind"] == "manual_edit"
                    includes_reject_clear |= origin["review_decision"] == "reject_clear"
    fingerprint = _digest(
        {
            "sheet_id": sheet_id,
            "row_ids": row_ids,
            "descriptors": sorted(descriptor_facts),
            "revisions": [
                [row_id, column_id, revisions.get((row_id, column_id))]
                for row_id, column_id in sorted(pairs)
            ],
        }
    )
    return (
        source_rows,
        baseline_rows,
        {
            "mixed_runs": (
                baseline_seed_run_id is not None
                and any(run_id != baseline_seed_run_id for run_id in mixed_run_ids)
            ),
            "includes_manual_edits": includes_manual_edits,
            "includes_reject_clear": includes_reject_clear,
        },
        fingerprint,
    )


def _compare_cell(db, read, row_id: int, column_id: int) -> dict[str, Any]:
    ref = dict(read.value_ref)
    review_decision = None
    if ref.get("kind") == "manual_edit" and ref.get("op_id") is not None:
        row = db.execute(
            "SELECT json_extract(spec,'$.params.decision') decision "
            "FROM ops WHERE id=?",
            (int(ref["op_id"]),),
        ).fetchone()
        review_decision = (
            "reject_clear"
            if row is not None and row["decision"] == "reject_clear"
            else "edit"
        )
    origin = {
        "kind": str(ref.get("kind") or "missing"),
        "run_id": ref.get("run_id"),
        "op_id": ref.get("op_id"),
        "review_decision": review_decision,
    }
    cell: dict[str, Any] = {"origin": origin}
    result_error = None
    if origin["kind"] == "run_result" and origin["run_id"] is not None:
        result = db.execute(
            "SELECT publication_effect,error FROM results "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            (int(origin["run_id"]), row_id, column_id),
        ).fetchone()
        if result is not None and result["publication_effect"] == "publish_error":
            result_error = str(result["error"] or "This row failed.")
    if result_error is not None:
        cell.update({"state": "error", "error": result_error})
    elif read.bounded_value is not None:
        bounded = read.bounded_value
        cell.update(
            {
                "state": "value",
                "bounded_value": {
                    "kind": "current_cell",
                    "preview": bounded.preview,
                    "value_kind": bounded.value_kind,
                    "total_characters": bounded.total_characters,
                    "total_bytes": bounded.total_bytes,
                    "retrieval_revision": bounded.retrieval_revision,
                    "truncated": True,
                    "row_id": row_id,
                    "column_id": column_id,
                },
            }
        )
    elif origin["kind"] == "missing" or not read.value_present:
        cell["state"] = "missing"
    elif read.value is None:
        cell.update({"state": "null", "value": None})
    else:
        cell.update({"state": "value", "value": read.value})
    return cell


def _bounded_aggregate_cell(
    db,
    cell: dict[str, Any],
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    aggregate_bytes: int,
    include_hidden: bool = False,
) -> tuple[dict[str, Any], int]:
    """Keep the complete context-cell payload under one conservative budget."""

    encoded_size = len(
        json.dumps(cell, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if aggregate_bytes + encoded_size <= COMPARE_CONTEXT_AGGREGATE_CELL_BYTE_LIMIT:
        return cell, aggregate_bytes + encoded_size
    bounded = cell.get("bounded_value")
    if isinstance(bounded, dict):
        bounded["preview"] = bounded["preview"][:256]
    elif cell.get("state") == "value" and isinstance(
        cell.get("value"), (str, dict, list)
    ):
        metadata = current_text_metadata(
            db,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            include_hidden=include_hidden,
        )
        cell.pop("value", None)
        cell["bounded_value"] = {
            "kind": "current_cell",
            "preview": read_current_text(
                db,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=column_id,
                metadata=metadata,
                start=0,
                limit=min(PREVIEW_CODEPOINT_LIMIT, 256),
                include_hidden=include_hidden,
            ),
            "value_kind": metadata.value_kind,
            "total_characters": metadata.total_characters,
            "total_bytes": metadata.total_bytes,
            "retrieval_revision": metadata.retrieval_revision,
            "truncated": True,
            "row_id": row_id,
            "column_id": column_id,
        }
    encoded_size = len(
        json.dumps(cell, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    return cell, aggregate_bytes + encoded_size


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _refusal(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


__all__ = ["ActionCompareRouteError", "ActionCompareService"]
