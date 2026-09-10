"""MapRunner-owned declaration of result generations and output contracts."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from frisket.engine.runner.preparation import (
    PreparedRun,
    _created_output_column_ids,
    _validate_managed_recipe_identity,
)
from frisket.engine.store import Project
from frisket.engine.store.result_generations import (
    GenerationDeclarationConflict,
    ResultGenerationStore,
)
from frisket.engine.store.runs import RunResultStore
from frisket.execution.runtime_binding import ExecutionRouteVerificationFailed
from frisket.ops.base import Recipe


_COMPATIBILITY_SCHEMA_VERSION = "frisket.output_compatibility.v1"


def _compatibility_key(
    *,
    field: dict[str, Any],
) -> str:
    """Hash only the consumer-readable value contract.

    Producer/action identity remains immutable provenance on the owning run,
    but it does not make two identically represented columns incompatible.
    Likewise, an output role is a binding within one run rather than part of
    how a cell value is decoded or interpreted.
    """

    descriptor = {
        "schema_version": _COMPATIBILITY_SCHEMA_VERSION,
        "column_type": str(field["column_type"]),
        "semantic_type": field.get("semantic_type"),
        "format": field.get("format"),
        "schema": field.get("schema"),
    }
    encoded = json.dumps(
        descriptor,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _write_mode_for_prepared_column(
    project: Project,
    prepared: PreparedRun,
    column_id: int,
) -> str:
    """Separate lifecycle creation from publication-history creation.

    Undo hides an op-created column, and a later action revives that same
    physical id as one of its own ``created_columns`` for visibility/undo.
    That is not a fresh publication substrate when immutable generations
    already exist on the id: it must stage a successor and publish only at
    seal. Excluding the recovering run keeps an originally-fresh ``create``
    declaration exactly idempotent after a crash.
    """

    if int(column_id) not in prepared.created_output_column_ids:
        return "replace_scope"
    prior_generation = project.db.execute(
        "SELECT 1 FROM run_output_generations WHERE column_id=? AND run_id<>? LIMIT 1",
        (int(column_id), int(prepared.run_id)),
    ).fetchone()
    return "replace_scope" if prior_generation is not None else "create"


def declare_prepared_outputs(
    project: Project,
    run_store: RunResultStore,
    prepared: PreparedRun,
    *,
    claim_token: str | None,
    defer_publication: bool = False,
) -> None:
    """Declare one exact output binding group before any result is written.

    Declarations compose into a single transaction so a multi-output producer
    can never recover with only a prefix of its output plan installed.
    """

    if not prepared.out_cols:
        return
    generations = ResultGenerationStore(project)
    if not isinstance(claim_token, str) or not claim_token:
        raise ExecutionRouteVerificationFailed(
            "stale_head",
            "the prepared dispatch carries no output claim token",
        )
    if run_store.get_run(prepared.run_id) is None:
        raise ValueError(f"run {prepared.run_id} does not exist")
    fields = {str(field["name"]): dict(field) for field in prepared.output_fields}
    if set(fields) != set(prepared.out_cols):
        raise RuntimeError("prepared output descriptors do not match output columns")
    write_modes = {
        str(output_role): _write_mode_for_prepared_column(
            project, prepared, int(column_id)
        )
        for output_role, column_id in prepared.out_cols.items()
    }
    descriptor_changing: dict[str, bool] = {}
    for output_role, column_id in prepared.out_cols.items():
        column = project.db.execute(
            "SELECT type,format,semantic_type FROM columns WHERE id=?",
            (int(column_id),),
        ).fetchone()
        field = fields[str(output_role)]
        descriptor_changing[str(output_role)] = column is not None and {
            "type": str(column["type"]),
            "format": column["format"],
            "semantic_type": column["semantic_type"],
        } != {
            "type": str(field["column_type"]),
            "format": field.get("format"),
            "semantic_type": field.get("semantic_type"),
        }
    stage_output_group = defer_publication or any(descriptor_changing.values())

    started_transaction = not project.db.in_transaction
    if started_transaction:
        project.db.execute("BEGIN IMMEDIATE")
    savepoint = f"declare_result_generations_{uuid.uuid4().hex}"
    project.db.execute(f"SAVEPOINT {savepoint}")
    try:
        existing_bindings = generations.bindings_for_run(prepared.run_id)
        prepared_roles = {
            (str(output_role), int(column_id))
            for output_role, column_id in prepared.out_cols.items()
        }
        if existing_bindings:
            existing_roles = {
                (binding.output_role, binding.column_id)
                for binding in existing_bindings
            }
            if existing_roles != prepared_roles:
                raise GenerationDeclarationConflict(
                    f"run {prepared.run_id} recovered with a different output "
                    "declaration set"
                )
            for binding in existing_bindings:
                field = fields[binding.output_role]
                expected_mode = write_modes[binding.output_role]
                if (
                    binding.compatibility_key != _compatibility_key(field=field)
                    or binding.write_mode != expected_mode
                ):
                    raise GenerationDeclarationConflict(
                        f"run {prepared.run_id} recovered with changed immutable "
                        f"output descriptor {binding.output_role!r}"
                    )
        # A scalar-only base has no exact heads to preserve for untargeted
        # rows. Refuse before inserting any marker: readers must continue to
        # treat the column as wholly legacy rather than observe a headless
        # managed transition.
        for output_role, column_id in prepared.out_cols.items():
            if write_modes[output_role] == "create":
                continue
            column = project.db.execute(
                "SELECT current_run_id FROM columns WHERE id=?",
                (int(column_id),),
            ).fetchone()
            if column is not None and column["current_run_id"] is not None:
                current_run_id = int(column["current_run_id"])
                if not generations.is_generation_managed(int(column_id)):
                    raise GenerationDeclarationConflict(
                        f"output column {int(column_id)} has scalar-only generated "
                        "history and cannot enter exact-head replacement"
                    )
                if generations.get_binding(current_run_id, int(column_id)) is None:
                    raise GenerationDeclarationConflict(
                        f"output column {int(column_id)} currently points at legacy "
                        f"run {current_run_id} after managed publication"
                    )
        for output_role, column_id in prepared.out_cols.items():
            field = fields[output_role]
            generations.declare(
                prepared.run_id,
                int(column_id),
                output_role=output_role,
                compatibility_key=_compatibility_key(field=field),
                write_mode=write_modes[output_role],
                claim_token=claim_token,
                target_descriptor={
                    "type": str(field["column_type"]),
                    "format": field.get("format"),
                    "semantic_type": field.get("semantic_type"),
                },
                defer_publication=stage_output_group,
                commit=False,
            )
        if any(descriptor_changing.values()):
            # A descriptor-changing replacement publishes as one whole output group.
            # Fresh siblings must not become visible while the replacement
            # target still exposes its previous descriptor and values.
            project.db.executemany(
                "UPDATE columns SET hidden=1 WHERE id=?",
                ((column_id,) for column_id in prepared.created_output_column_ids),
            )
    except BaseException:
        project.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        project.db.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started_transaction:
            project.db.rollback()
        raise
    project.db.execute(f"RELEASE SAVEPOINT {savepoint}")
    if started_transaction:
        project.db.commit()


def declare_bound_run_outputs(
    project: Project,
    *,
    run_id: int,
    output_fields: list[dict[str, Any]],
    program: Recipe | None = None,
    claim_token: str,
    defer_publication: bool = False,
) -> None:
    """Declare a just-bound prepared run before external terminalizers see it."""

    run_store = RunResultStore(project)
    run_row = run_store.get_run(int(run_id))
    if run_row is None:
        raise ValueError(f"run {run_id} does not exist")
    try:
        spec = json.loads(run_row["params"] or "{}")
    except (TypeError, ValueError):
        raise ValueError(f"run {run_id} has invalid persisted params") from None
    if not isinstance(spec, dict):
        raise ValueError(f"run {run_id} has invalid persisted params")

    from frisket.engine.runner.validation import recipe_for_spec

    recipe = program if program is not None else recipe_for_spec(spec)
    rows = project.db.execute(
        "SELECT output_name,column_id FROM output_column_claims "
        "WHERE claim_token=? AND run_id=? AND status='active' "
        "ORDER BY output_name",
        (claim_token, int(run_id)),
    ).fetchall()
    out_cols = {
        str(row["output_name"]): int(row["column_id"])
        for row in rows
        if row["column_id"] is not None
    }
    field_names = {str(field["name"]) for field in output_fields}
    if len(out_cols) != len(rows) or set(out_cols) != field_names:
        raise ValueError(
            f"run {run_id} bound claim does not match its frozen output descriptors"
        )
    _validate_managed_recipe_identity(
        run_id=int(run_id),
        run_row=run_row,
        recipe=recipe,
        action_kind=str(spec["action_kind"]),
    )
    created_ids = _created_output_column_ids(project, int(run_row["op_id"]))
    declare_prepared_outputs(
        project,
        run_store,
        PreparedRun(
            recipe=recipe,
            sheet_id=int(run_row["sheet_id"]),
            col_map={},
            row_ids=[],
            out_cols=out_cols,
            output_fields=tuple(dict(field) for field in output_fields),
            created_output_column_ids=created_ids.intersection(out_cols.values()),
            op_id=int(run_row["op_id"]),
            run_id=int(run_id),
            row_count=int(run_row["total_rows"] or 0),
        ),
        claim_token=claim_token,
        defer_publication=defer_publication,
    )
