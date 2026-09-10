from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from frisket.engine.store.runs import FAILURE_OUTCOMES, outcome_sql_list
from frisket.sdk.declaration import Op
from frisket.sdk.replay import output_column_result_value_hash


class MixedOriginInputProvenanceUnsupported(ValueError):
    """A column-level receipt ref cannot represent multiple result origins."""

    code = "mixed_origin_input_provenance_unsupported"

    def __init__(
        self,
        column_id: int,
        origin_run_ids: list[int],
        *,
        missing_head_row_ids: list[int] | None = None,
        divergent_edit_row_ids: list[int] | None = None,
    ):
        self.column_id = int(column_id)
        self.origin_run_ids = [int(run_id) for run_id in origin_run_ids]
        self.missing_head_row_ids = [
            int(row_id) for row_id in (missing_head_row_ids or [])
        ]
        self.divergent_edit_row_ids = [
            int(row_id) for row_id in (divergent_edit_row_ids or [])
        ]
        super().__init__(
            "input column provenance cannot represent exact scoped origins for "
            f"column {self.column_id}: runs={self.origin_run_ids}, "
            f"headless_rows={self.missing_head_row_ids}, "
            f"edited_rows={self.divergent_edit_row_ids}"
        )

    def action_error_details(self, *, column: str) -> dict[str, Any]:
        """Stable public refusal details for action resolve boundaries."""

        return {
            "reason": self.code,
            "column": str(column),
            "column_id": self.column_id,
            "origin_run_ids": self.origin_run_ids,
            "missing_head_row_ids": self.missing_head_row_ids,
            "divergent_edit_row_ids": self.divergent_edit_row_ids,
        }


@dataclass(frozen=True)
class OutputFact:
    """One generated output column: the recipe field def + the live column."""

    field: dict[str, Any]
    name: str
    column_id: int
    type: str
    value_hash: str


@dataclass(frozen=True)
class CapturedFacts:
    run: Any  # raw `runs` row, for op-specific status/error helpers
    run_id: int
    params_hash: str
    op_id: int
    sheet_id: int
    total_rows: int
    completed_rows: int
    failed_rows: int
    # NULL means this run's provider cost is NOT KNOWN (one live call whose
    # cost the provider never reported).  It is deliberately not collapsed to
    # 0.0 here: this is the first boundary the honest unknown crosses, and
    # coercing it makes a real paid call render identically to a free local
    # one in every receipt downstream.  Consumers publish the None.
    cost_actual: float | None
    model: str
    prompt_hash: Any
    run_status: str
    row_ids: list[int]
    failed_row_ids: list[int]
    output_facts: list[OutputFact]
    missing_outputs: list[str]
    input_column_ids: dict[str, int]
    input_column_types: dict[str, str]
    # rich input-column metadata (ordered), present when decl.rich_input_columns.
    input_columns_rich: list[dict[str, Any]]
    runner_spec: dict[str, Any]
    # Neutral provider facts for any op that performed a provider effect.
    # Historically these were loaded only for declarations marked
    # ``model_backed``; external_map actions can now emit the same durable
    # fact shape, so receipt projection must not hide them by declaration
    # archetype.
    model_calls: list[Any]
    model_call_ids: list[int]


def maprunner_output_refs(
    facts: CapturedFacts, *, output_kind: str
) -> list[dict[str, Any]]:
    """Present captured output columns in the shared receipt-reference shape."""

    return [
        {
            "kind": output_kind,
            "name": fact.name,
            "type": fact.type,
            **({"format": fact.field["format"]} if "format" in fact.field else {}),
            "sheet_id": facts.sheet_id,
            "column_id": fact.column_id,
            "run_id": facts.run_id,
            "op_id": facts.op_id,
            "row_ids": facts.row_ids,
            "value_hash": fact.value_hash,
        }
        for fact in facts.output_facts
    ]


def _output_columns_by_name(
    project: Any,
    *,
    run_id: int,
    sheet_id: int,
    output_names: list[str],
    allow_open_generation: bool = False,
) -> dict[str, Any]:
    """Discover outputs from one authority lane.

    A run with any result-generation binding is wholly generation-managed: its
    exact sealed output-role bindings identify its columns. Runs with no
    bindings retain the legacy ``current_run_id`` lookup unchanged.
    """

    if not output_names:
        return {}

    from frisket.engine.store.result_generations import (
        GenerationStateError,
        ResultGenerationStore,
    )

    generations = ResultGenerationStore(project)
    bindings = generations.bindings_for_run(run_id)
    if not bindings:
        placeholders = ",".join("?" for _ in output_names)
        return {
            str(row["name"]): row
            for row in project.db.execute(
                f"SELECT * FROM columns WHERE sheet_id=? AND current_run_id=? "
                f"AND name IN ({placeholders})",
                [sheet_id, run_id, *output_names],
            ).fetchall()
        }

    unsealed = [
        binding.output_role for binding in bindings if binding.state != "sealed"
    ]
    if unsealed and not allow_open_generation:
        raise GenerationStateError(
            f"run {run_id} output receipt capture requires sealed bindings, not "
            f"open roles {sorted(unsealed)}"
        )

    bindings_by_role = {binding.output_role: binding for binding in bindings}
    expected_roles = set(output_names)
    binding_roles = set(bindings_by_role)
    if binding_roles != expected_roles:
        raise GenerationStateError(
            f"run {run_id} output binding roles do not match its recipe fields: "
            f"missing={sorted(expected_roles - binding_roles)}, "
            f"unexpected={sorted(binding_roles - expected_roles)}"
        )
    expected_bindings = [bindings_by_role[name] for name in output_names]
    column_ids = sorted({binding.column_id for binding in expected_bindings})
    placeholders = ",".join("?" for _ in column_ids)
    columns_by_id = {
        int(row["id"]): row
        for row in project.db.execute(
            f"SELECT * FROM columns WHERE sheet_id=? AND id IN ({placeholders})",
            [sheet_id, *column_ids],
        ).fetchall()
    }
    columns_by_name: dict[str, Any] = {}
    for binding in expected_bindings:
        column = columns_by_id.get(binding.column_id)
        if column is None or str(column["name"]) != binding.output_role:
            continue
        columns_by_name[binding.output_role] = column
    return columns_by_name


def _input_source_run_id(
    project: Any,
    *,
    sheet_id: int,
    column_id: int,
    row_ids: list[int],
    legacy_current_run_id: Any,
) -> int | None:
    """Resolve rich input provenance without consulting a managed scalar."""

    from frisket.engine.store.result_generations import ResultGenerationStore

    generations = ResultGenerationStore(project)
    if not generations.is_generation_managed(column_id):
        return int(legacy_current_run_id) if legacy_current_run_id is not None else None
    ordered_row_ids = list(dict.fromkeys(int(row_id) for row_id in row_ids))
    if not ordered_row_ids:
        return None
    heads = generations.read_cell_heads(column_id, row_ids=ordered_row_ids)
    # Two distinct origins are sufficient to prove that the receipt's single
    # ``source_run_id`` field is not representationally adequate. Keep error
    # details bounded even for a large mixed-generation input scope.
    origin_run_ids = sorted({head.run_id for head in heads.values()}, reverse=True)[:2]
    missing_head_row_ids = [row_id for row_id in ordered_row_ids if row_id not in heads]

    # The public resolver owns the exact-head/static/manual overlay ordering.
    # A divergent manual overlay has no run id and therefore cannot be
    # collapsed into this receipt's column-level source_run_id.
    underlying_values, _underlying_refs = project.get_values_with_refs(
        sheet_id,
        column_id,
        row_ids=ordered_row_ids,
        apply_edits=False,
    )
    live_values, live_refs = project.get_values_with_refs(
        sheet_id,
        column_id,
        row_ids=ordered_row_ids,
        apply_edits=True,
    )
    divergent_edit_row_ids = [
        row_id
        for row_id in ordered_row_ids
        if live_refs.get(row_id, {}).get("kind") == "manual_edit"
        and live_values.get(row_id) != underlying_values.get(row_id)
    ]

    # Zero generated heads is honestly representable as a baseline/no-run
    # input. Any partial coverage, multiple origins, or divergent edit overlay
    # needs per-cell refs which this receipt shape does not carry.
    if not heads and not divergent_edit_row_ids:
        return None
    if missing_head_row_ids or len(origin_run_ids) != 1 or divergent_edit_row_ids:
        raise MixedOriginInputProvenanceUnsupported(
            column_id,
            origin_run_ids,
            missing_head_row_ids=missing_head_row_ids,
            divergent_edit_row_ids=divergent_edit_row_ids,
        )
    return origin_run_ids[0]


def capture_facts(
    project: Any,
    decl: Op,
    *,
    runner_spec: dict[str, Any],
    run_id: int,
    params_hash: str,
    input_column_ids: dict[str, int],
    input_column_types: dict[str, str],
    allow_open_generation: bool = False,
) -> CapturedFacts:
    from frisket.engine.runner.validation import recipe_for_spec

    return capture_maprunner_facts(
        project,
        runner_spec=runner_spec,
        output_fields=recipe_for_spec(runner_spec).output_fields(runner_spec),
        run_id=run_id,
        params_hash=params_hash,
        input_column_ids=input_column_ids,
        input_column_types=input_column_types,
        rich_input_columns=decl.rich_input_columns,
        allow_open_generation=allow_open_generation,
    )


def capture_maprunner_facts(
    project: Any,
    *,
    runner_spec: dict[str, Any],
    output_fields: list[dict[str, Any]],
    run_id: int,
    params_hash: str,
    input_column_ids: dict[str, int],
    input_column_types: dict[str, str],
    target_row_ids: list[int] | None = None,
    rich_input_columns: bool = False,
    allow_open_generation: bool = False,
) -> CapturedFacts:
    """Capture a prepared program from explicit descriptors, without an ``Op``."""

    run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    op_id = int(run["op_id"])
    sheet_id = int(run["sheet_id"])
    has_scope = (
        project.db.execute(
            "SELECT 1 FROM run_scopes WHERE run_id=?", (run_id,)
        ).fetchone()
        is not None
    )
    scope_join = (
        "JOIN run_rows scope ON scope.run_id=res.run_id AND scope.row_id=res.row_id "
        if has_scope
        else ""
    )
    row_ids = (
        list(target_row_ids)
        if target_row_ids is not None
        else [
            int(row["row_id"])
            for row in project.db.execute(
                "SELECT DISTINCT res.row_id FROM results res "
                f"{scope_join}WHERE res.run_id=? ORDER BY res.row_id",
                (run_id,),
            ).fetchall()
        ]
    )
    failed_row_ids = [
        int(row["row_id"])
        for row in project.db.execute(
            "SELECT DISTINCT res.row_id FROM results res "
            f"{scope_join}WHERE res.run_id=? "
            f"AND res.outcome IN ({outcome_sql_list(FAILURE_OUTCOMES)}) "
            "ORDER BY res.row_id",
            (run_id,),
        ).fetchall()
    ]
    output_names = [field_def["name"] for field_def in output_fields]
    columns_by_name = _output_columns_by_name(
        project,
        run_id=run_id,
        sheet_id=sheet_id,
        output_names=output_names,
        allow_open_generation=allow_open_generation,
    )
    missing = [name for name in output_names if name not in columns_by_name]
    output_facts: list[OutputFact] = []
    for field_def in output_fields:
        name = field_def["name"]
        column = columns_by_name.get(name)
        if column is None:
            continue
        col_id = int(column["id"])
        # Receipts always carry a raw-result value hash now (replay-
        # unification-v1), including nondeterministic (LLM) output: it hashes
        # the current run's raw result, not a manual edit overlay that may
        # predate this run. Whether a hash mismatch is fatal on replay is a
        # function of the op's replay scope (sdk/replay.py), not of whether
        # the hash was captured.
        value_hash = output_column_result_value_hash(
            project, sheet_id=sheet_id, column_id=col_id, row_ids=row_ids
        )
        output_facts.append(
            OutputFact(
                field=field_def,
                name=name,
                column_id=col_id,
                type=column["type"],
                value_hash=value_hash,
            )
        )

    input_columns_rich: list[dict[str, Any]] = []
    if rich_input_columns:
        from frisket.engine.store.receipts import ReceiptStore

        for name, col_id in input_column_ids.items():
            col = project.db.execute(
                "SELECT * FROM columns WHERE id=?", (col_id,)
            ).fetchone()
            source_run_id = _input_source_run_id(
                project,
                sheet_id=sheet_id,
                column_id=col_id,
                row_ids=row_ids,
                legacy_current_run_id=col["current_run_id"],
            )
            source_receipt_id = (
                ReceiptStore(project).latest_id_for_run(source_run_id)
                if source_run_id is not None
                else None
            )
            input_columns_rich.append(
                {
                    "name": name,
                    "column_id": col_id,
                    "type": str(col["type"]),
                    "ai_generated": bool(col["ai_generated"]),
                    "source_run_id": source_run_id,
                    "source_receipt_id": source_receipt_id,
                }
            )

    from frisket.engine.store.runs import RunResultStore

    model_calls = RunResultStore(project).model_calls(run_id)
    model_call_ids = [row["id"] for row in model_calls]

    return CapturedFacts(
        run=run,
        run_id=run_id,
        params_hash=params_hash,
        op_id=op_id,
        sheet_id=sheet_id,
        total_rows=int(run["total_rows"] or 0),
        completed_rows=int(run["completed_rows"] or 0),
        failed_rows=int(run["failed_rows"] or 0),
        cost_actual=(None if run["cost_actual"] is None else float(run["cost_actual"])),
        model=str(run["model"] or ""),
        prompt_hash=run["prompt_hash"] if "prompt_hash" in run.keys() else None,
        run_status=run["status"],
        row_ids=row_ids,
        failed_row_ids=failed_row_ids,
        output_facts=output_facts,
        missing_outputs=missing,
        input_column_ids=input_column_ids,
        input_column_types=input_column_types,
        input_columns_rich=input_columns_rich,
        runner_spec=runner_spec,
        model_calls=model_calls,
        model_call_ids=model_call_ids,
    )
