"""Run preparation for the map runner (extracted from ``MapRunner._prepare_validated``):
materialize a validated spec into output columns and an op/run row (fresh launch)
or repoint an existing run's dispatch scope (resume) — the writing half of
``_prepare``, which stays in the engine as the orchestrating facade (savepoint
handling for atomic-output-column recipes)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from frisket.ops.base import Recipe
from frisket.ops.builtin import prompt_hash_of
from frisket.engine.runner.column_retirement import (
    retire_dropped_output_columns,
)
from frisket.engine.runner.validation import _ValidatedSpec
from frisket.engine.store import Project
from frisket.engine.store.output_families import OutputFamilyStore
from frisket.engine.store.result_generations import (
    GenerationDeclarationConflict,
    GenerationSealedError,
    GenerationStateError,
    ResultGenerationStore,
    RunOutputGeneration,
)
from frisket.engine.store.runs import RunResultStore

PREPARED_ATOMIC_OUTPUT_COLUMNS_KEY = "prepared_atomic_output_columns"


@dataclass
class PreparedRun:
    recipe: Recipe
    sheet_id: int
    col_map: dict[str, int]
    row_ids: list[int]
    out_cols: dict[str, int]
    output_fields: tuple[dict[str, Any], ...]
    created_output_column_ids: frozenset[int]
    op_id: int
    run_id: int
    row_count: int
    run_scope_run_id: int | None = None


def _created_output_column_ids(project: Project, op_id: int) -> frozenset[int]:
    row = project.db.execute(
        "SELECT undo_info FROM ops WHERE id=?",
        (op_id,),
    ).fetchone()
    if row is None:
        return frozenset()
    try:
        undo_info = json.loads(row["undo_info"] or "{}")
    except (TypeError, ValueError):
        return frozenset()
    return frozenset(int(value) for value in undo_info.get("created_columns", ()))


def persistable_spec(spec: dict) -> dict:
    return {key: value for key, value in spec.items() if key != "confirmed"}


def prepared_atomic_output_columns_for_resume(
    project: Project,
    *,
    run_row: Any,
    sheet_id: int,
    fields: list[dict[str, Any]],
) -> dict[str, int]:
    """Validate and return the exact family recorded at fresh prepare."""

    error = "prepared atomic output family changed before resume"
    op = project.db.execute(
        "SELECT undo_info FROM ops WHERE id=?", (int(run_row["op_id"]),)
    ).fetchone()
    try:
        info = json.loads(op["undo_info"] or "{}") if op is not None else {}
    except (TypeError, json.JSONDecodeError):
        raise ValueError(error) from None
    prepared = (
        info.get(PREPARED_ATOMIC_OUTPUT_COLUMNS_KEY) if isinstance(info, dict) else None
    )
    expected = {str(field["name"]): field for field in fields}
    if not isinstance(prepared, dict) or set(prepared) != set(expected):
        raise ValueError(error)
    if any(
        not isinstance(column_id, int) or isinstance(column_id, bool) or column_id <= 0
        for column_id in prepared.values()
    ) or len(set(prepared.values())) != len(prepared):
        raise ValueError(error)

    placeholders = ",".join("?" for _ in prepared)
    rows = project.db.execute(
        "SELECT id,sheet_id,name,type,format,ai_generated,hidden FROM columns "
        f"WHERE id IN ({placeholders})",
        tuple(prepared.values()),
    ).fetchall()
    rows_by_id = {int(row["id"]): row for row in rows}
    for name, column_id in prepared.items():
        row = rows_by_id.get(column_id)
        field = expected[name]
        if (
            row is None
            or int(row["sheet_id"]) != sheet_id
            or str(row["name"]) != name
            or bool(row["hidden"])
            or not bool(row["ai_generated"])
            or str(row["type"]) != str(field["column_type"])
            or row["format"] != field.get("format")
        ):
            raise ValueError(error)
    return {name: int(column_id) for name, column_id in prepared.items()}


def _validate_managed_resume(
    project: Project,
    run_store: RunResultStore,
    *,
    run_id: int,
    sheet_id: int,
    fields: list[dict[str, Any]],
    bindings: list[RunOutputGeneration],
    explicit_resume_scope: list[int] | None,
) -> dict[str, int]:
    if not bindings:
        return {}
    if any(binding.state == "sealed" for binding in bindings):
        raise GenerationSealedError(f"generation-managed run {run_id} is sealed")
    if explicit_resume_scope is not None:
        stored_scope = run_store.run_row_scope_if_present(run_id)
        if stored_scope is None or stored_scope != [
            int(row_id) for row_id in explicit_resume_scope
        ]:
            raise GenerationStateError(
                f"generation-managed run {run_id} cannot change its row scope"
            )

    column_ids = sorted(binding.column_id for binding in bindings)
    placeholders = ",".join("?" for _ in column_ids)
    rows = project.db.execute(
        "SELECT id,sheet_id,name,type,format,semantic_type,ai_generated,hidden,"
        "default_hidden FROM columns "
        f"WHERE id IN ({placeholders}) ORDER BY id",
        column_ids,
    ).fetchall()
    fields_by_name = {str(field["name"]): field for field in fields}
    if (
        len(fields_by_name) != len(fields)
        or len(fields) != len(bindings)
        or len(rows) != len(bindings)
    ):
        raise GenerationStateError(
            f"generation-managed run {run_id} changed its output descriptors"
        )
    operation = project.db.execute(
        "SELECT ops.id,ops.spec,ops.undo_info FROM runs JOIN ops ON ops.id=runs.op_id "
        "WHERE runs.id=?",
        (run_id,),
    ).fetchone()
    # A coupled action hides only its fresh columns until final publication.
    # Require that exact staging state; do not excuse arbitrary visibility drift.
    staged_columns = (
        _created_output_column_ids(project, int(operation["id"]))
        if operation is not None
        and json.loads(operation["spec"]).get("deferred_publication") is True
        else frozenset()
    )
    operation_info = (
        json.loads(operation["undo_info"] or "{}") if operation is not None else {}
    )
    pending_descriptor_changes = ResultGenerationStore._pending_descriptor_changes(
        operation_info if isinstance(operation_info, dict) else {}
    )
    for row in rows:
        field = fields_by_name.get(str(row["name"]))
        pending_descriptor_change = pending_descriptor_changes.get(str(int(row["id"])))
        descriptor_matches = (
            {
                "type": str(row["type"]),
                "format": row["format"],
                "semantic_type": row["semantic_type"],
            }
            == {
                "type": str(field["column_type"]),
                "format": field.get("format"),
                "semantic_type": field.get("semantic_type"),
            }
            if field
            else False
        )
        if field is not None and pending_descriptor_change is not None:
            descriptor_matches = pending_descriptor_change == {
                "before": {
                    "type": str(row["type"]),
                    "format": row["format"],
                    "semantic_type": row["semantic_type"],
                },
                "after": {
                    "type": str(field["column_type"]),
                    "format": field.get("format"),
                    "semantic_type": field.get("semantic_type"),
                },
            }
        if field is None or (
            int(row["sheet_id"]) != int(sheet_id)
            or not bool(row["ai_generated"])
            or not descriptor_matches
            or bool(row["hidden"])
            != (int(row["id"]) in staged_columns or bool(field.get("hidden", False)))
            or bool(row["default_hidden"]) != bool(field.get("default_hidden", False))
        ):
            raise GenerationStateError(
                f"generation-managed run {run_id} changed its output descriptors"
            )
    return {str(row["name"]): int(row["id"]) for row in rows}


def _validate_managed_recipe_identity(
    *,
    run_id: int,
    run_row: Any,
    recipe: Recipe,
    action_kind: str,
) -> None:
    if str(run_row["action_kind"]) != action_kind or str(
        run_row["action_version"]
    ) != str(recipe.version):
        raise GenerationStateError(
            f"generation-managed run {run_id} recipe identity changed before recovery"
        )


def _require_compatible_hidden_managed_revival(
    project: Project,
    *,
    sheet_id: int,
    field: dict[str, Any],
) -> None:
    """Refuse a hidden managed name before ``add_column`` can mutate it."""

    row = project.db.execute(
        "SELECT id,type,format,semantic_type,ai_generated FROM columns "
        "WHERE sheet_id=? AND name=? AND hidden=1",
        (int(sheet_id), str(field["name"])),
    ).fetchone()
    if row is None:
        return
    history = project.db.execute(
        "SELECT compatibility_key FROM run_output_generations "
        "WHERE column_id=? ORDER BY run_id DESC",
        (int(row["id"]),),
    ).fetchall()
    if not history:
        raise GenerationDeclarationConflict(
            f"hidden output {field['name']!r} has no generation history"
        )

    # Local import avoids making preparation/result declaration a module-load
    # cycle while still using the substrate's one compatibility descriptor.
    from frisket.engine.runner.result_generations import _compatibility_key

    expected_key = _compatibility_key(field=field)
    if (
        not bool(row["ai_generated"])
        or str(row["type"]) != str(field["column_type"])
        or row["format"] != field.get("format")
        or row["semantic_type"] != field.get("semantic_type")
        or any(binding["compatibility_key"] != expected_key for binding in history)
    ):
        raise GenerationDeclarationConflict(
            f"hidden generation-managed output {field['name']!r} is incompatible "
            "with the declared output contract"
        )


def _require_existing_ai_generation_history(
    project: Project,
    *,
    sheet_id: int,
    fields: list[dict[str, Any]],
    resume_run_id: int | None,
) -> None:
    """Refuse implicit adoption before any output reuse can mutate columns."""

    names = [str(field["name"]) for field in fields]
    if not names:
        return
    placeholders = ",".join("?" for _ in names)
    rows = project.db.execute(
        "SELECT id,current_run_id FROM columns "
        f"WHERE sheet_id=? AND ai_generated=1 AND name IN ({placeholders})",
        (int(sheet_id), *names),
    ).fetchall()
    for row in rows:
        has_history = project.db.execute(
            "SELECT 1 FROM run_output_generations WHERE column_id=? LIMIT 1",
            (int(row["id"]),),
        ).fetchone()
        if has_history is not None:
            continue
        precreated_open_run = bool(
            resume_run_id is not None
            and row["current_run_id"] == resume_run_id
            and project.db.execute(
                "SELECT 1 FROM results WHERE run_id=? LIMIT 1",
                (resume_run_id,),
            ).fetchone()
            is None
        )
        if not precreated_open_run:
            raise GenerationDeclarationConflict(
                f"AI output column {int(row['id'])} has no generation history "
                "and cannot enter exact-head replacement"
            )


def prepare_validated(
    project: Project,
    run_store: RunResultStore,
    spec: dict,
    *,
    validated: _ValidatedSpec,
    resume_run_id: int | None,
    defer_commits: bool,
) -> PreparedRun:
    recipe = validated.recipe
    est = validated.est
    sheet_id = validated.sheet_id
    columns = validated.columns
    col_map = validated.col_map
    row_ids = validated.row_ids
    has_stored_run_scope = validated.has_stored_run_scope
    stored_run_scope_count = validated.stored_run_scope_count
    explicit_resume_scope = validated.explicit_resume_scope
    output_fields = validated.output_fields

    if resume_run_id is None and "row_ids" in spec:
        from frisket.engine.runner.validation import InvalidTargetRows, target_rows

        live_row_ids = target_rows(project, spec)
        if set(live_row_ids) != set(row_ids):
            raise InvalidTargetRows(
                sorted(set(live_row_ids).symmetric_difference(row_ids))
            )

    run_row = None
    if resume_run_id is not None:
        run_row = run_store.get_run(resume_run_id)
        if run_row is None:
            raise ValueError(f"run {resume_run_id} does not exist")
        managed_bindings = ResultGenerationStore(project).bindings_for_run(
            resume_run_id
        )
        if managed_bindings:
            _validate_managed_recipe_identity(
                run_id=resume_run_id,
                run_row=run_row,
                recipe=recipe,
                action_kind=spec["action_kind"],
            )
        managed_out_cols = _validate_managed_resume(
            project,
            run_store,
            run_id=resume_run_id,
            sheet_id=sheet_id,
            fields=output_fields,
            bindings=managed_bindings,
            explicit_resume_scope=explicit_resume_scope,
        )
    else:
        managed_bindings = []
        managed_out_cols = {}

    _require_existing_ai_generation_history(
        project,
        sheet_id=sheet_id,
        fields=output_fields,
        resume_run_id=resume_run_id,
    )

    # output columns (created once; reused on re-run of same names)
    out_cols: dict[str, int] = dict(managed_out_cols)
    created_columns: list[int] = []
    field_names = {str(field["name"]) for field in output_fields}
    raw_target_preconditions = spec.get("output_target_preconditions", {})
    if not isinstance(raw_target_preconditions, dict) or any(
        not isinstance(name, str)
        or (column_id is not None and (type(column_id) is not int or column_id <= 0))
        for name, column_id in raw_target_preconditions.items()
    ):
        raise ValueError(
            "output_target_preconditions must map output names to positive ids or null"
        )
    if raw_target_preconditions and set(raw_target_preconditions) != field_names:
        raise ValueError(
            "output target preconditions must cover every output descriptor"
        )
    output_column_ids_by_name = {
        str(name): int(column_id)
        for name, column_id in raw_target_preconditions.items()
        if column_id is not None
    }
    if (
        resume_run_id is None
        and spec.get("action_kind") == "map.ner"
        and "row_ids" in spec
        and not spec.get("overwrite")
    ):
        from frisket.engine.runner.validation import OutputColumnExists

        output_names = [str(field["name"]) for field in output_fields]
        placeholders = ",".join("?" for _ in output_names)
        collisions = project.db.execute(
            "SELECT name FROM columns WHERE sheet_id=? AND hidden=0 "
            f"AND name IN ({placeholders})",
            [sheet_id, *output_names],
        ).fetchall()
        if collisions:
            raise OutputColumnExists([str(row["name"]) for row in collisions])
    if recipe.atomic_output_columns and not managed_bindings:
        if run_row is not None:
            out_cols = prepared_atomic_output_columns_for_resume(
                project,
                run_row=run_row,
                sheet_id=sheet_id,
                fields=output_fields,
            )
        else:
            out_cols, created_columns = OutputFamilyStore(project).create_or_reuse(
                sheet_id=sheet_id,
                fields=output_fields,
            )
    for f in [] if recipe.atomic_output_columns or managed_bindings else output_fields:
        output_name = str(f["name"])
        expected_column_id = output_column_ids_by_name.get(output_name)
        target_precondition = raw_target_preconditions.get(output_name)
        existing = next(
            (
                c
                for c in columns
                if (
                    int(c["id"]) == expected_column_id
                    if expected_column_id is not None
                    else c["name"] == f["name"]
                )
            ),
            None,
        )
        if expected_column_id is not None and existing is None:
            exact_target = project.get_column(expected_column_id)
            if (
                exact_target is not None
                and int(exact_target["sheet_id"]) == sheet_id
                and str(exact_target["name"]) == output_name
                and bool(exact_target["hidden"])
            ):
                _require_compatible_hidden_managed_revival(
                    project,
                    sheet_id=sheet_id,
                    field=f,
                )
                revived_column_id = project.add_column(
                    sheet_id,
                    output_name,
                    type=f["column_type"],
                    ai_generated=True,
                    format=f.get("format"),
                    hidden=bool(f.get("hidden", False)),
                    default_hidden=bool(f.get("default_hidden", False)),
                    semantic_type=f.get("semantic_type"),
                    commit=not defer_commits,
                )
                if revived_column_id != expected_column_id:
                    raise ValueError(
                        f"replacement output {output_name!r} revived as column id "
                        f"{revived_column_id}, expected {expected_column_id}"
                    )
                out_cols[output_name] = revived_column_id
                created_columns.append(revived_column_id)
                continue
        if expected_column_id is not None and (
            existing is None or str(existing["name"]) != str(f["name"])
        ):
            raise ValueError(
                f"replacement output {f['name']!r} no longer resolves to "
                f"column id {expected_column_id}"
            )
        if (
            output_name in raw_target_preconditions
            and target_precondition is None
            and existing is not None
        ):
            raise ValueError(
                f"new output target {output_name!r} no longer has an absent precondition"
            )
        if existing:
            if not existing["ai_generated"] and not spec.get("overwrite"):
                raise ValueError(
                    f"output name '{f['name']}' collides with a source "
                    "column — AI output would hide original data. "
                    "Rename the field (or pass overwrite=true)."
                )
            out_cols[f["name"]] = existing["id"]
            generation_managed = (
                project.db.execute(
                    "SELECT 1 FROM run_output_generations WHERE column_id=? LIMIT 1",
                    (int(existing["id"]),),
                ).fetchone()
                is not None
            )
            if generation_managed:
                declared_semantic_type = f.get("semantic_type")
                existing_semantic_type = (
                    existing["semantic_type"]
                    if "semantic_type" in existing.keys()
                    else None
                )
                incompatible = {
                    "column_type": (existing["type"], f["column_type"]),
                    "format": (existing["format"], f.get("format")),
                    "semantic_type": (
                        existing_semantic_type,
                        declared_semantic_type,
                    ),
                }
                changed = {
                    key: values
                    for key, values in incompatible.items()
                    if values[0] != values[1]
                }
                allowed_descriptor_change = (
                    changed
                    and spec.get("replace_existing") is True
                    and "row_ids" not in spec
                    and not bool(existing["hidden"])
                )
                if changed and not allowed_descriptor_change:
                    raise ValueError(
                        f"output column {f['name']!r} is generation-managed and "
                        "keeps the output contract it was born with; this run "
                        f"declares a different one: {changed!r}. Write to a "
                        "new output name to produce the new type, or re-run "
                        "with the original settings."
                    )
            # Unmanaged reused outputs converge to the declared descriptor.
            if (
                not generation_managed
                and existing["ai_generated"]
                and existing["type"] != f["column_type"]
            ):
                project.set_column_type(
                    existing["id"],
                    f["column_type"],
                    commit=not defer_commits,
                )
            declared_format = f.get("format")
            if (
                not generation_managed
                and existing["ai_generated"]
                and existing["format"] != declared_format
            ):
                project.set_column_format(
                    existing["id"],
                    declared_format,
                    commit=not defer_commits,
                )
            declared_semantic_type = f.get("semantic_type")
            existing_semantic_type = (
                existing["semantic_type"]
                if "semantic_type" in existing.keys()
                else None
            )
            if (
                existing["ai_generated"]
                and not generation_managed
                and existing_semantic_type != declared_semantic_type
            ):
                project.set_column_semantic_type(
                    existing["id"],
                    declared_semantic_type,
                    commit=not defer_commits,
                )
        else:
            _require_compatible_hidden_managed_revival(
                project,
                sheet_id=sheet_id,
                field=f,
            )
            cid = project.add_column(
                sheet_id,
                f["name"],
                type=f["column_type"],
                ai_generated=True,
                format=f.get("format"),
                hidden=bool(f.get("hidden", False)),
                default_hidden=bool(f.get("default_hidden", False)),
                semantic_type=f.get("semantic_type"),
                commit=not defer_commits,
            )
            out_cols[f["name"]] = cid
            created_columns.append(cid)

    if resume_run_id is not None:
        run_id = resume_run_id
        assert run_row is not None
        op_id = run_row["op_id"]
        created_columns = list(_created_output_column_ids(project, int(op_id)))
        output_column_ids = sorted(out_cols.values())
        if managed_bindings and output_column_ids != sorted(
            binding.column_id for binding in managed_bindings
        ):
            raise GenerationStateError(
                f"generation-managed run {run_id} changed its output descriptors"
            )
        if has_stored_run_scope and explicit_resume_scope is None:
            row_ids = []
        else:
            if output_column_ids:
                done_rows = run_store.completed_result_row_ids(
                    run_id, output_column_ids
                )
            else:
                done_rows = set()
            if has_stored_run_scope and explicit_resume_scope is not None:
                assert stored_run_scope_count is not None
                if len(row_ids) != stored_run_scope_count:
                    run_store.record_run_row_scope(run_id, row_ids)
                    run_store.set_total_rows(run_id, len(row_ids))
            if row_ids:
                row_ids = [r for r in row_ids if r not in done_rows]
            if (
                not has_stored_run_scope
                and row_ids
                and run_row["completed_rows"] >= run_row["total_rows"]
            ):
                run_store.increment_total_rows(run_id, len(row_ids))
    else:
        persisted_spec = persistable_spec(spec)
        op_id = project.append_op(
            "map",
            persisted_spec,
            label=f"{recipe.name}: {', '.join(out_cols)}",
            commit=not defer_commits,
        )
        undo_info: dict[str, Any] = {}
        if recipe.atomic_output_columns:
            # Persist ids because names may change before a queued resume.
            undo_info[PREPARED_ATOMIC_OUTPUT_COLUMNS_KEY] = dict(out_cols)
        if created_columns:
            undo_info["created_columns"] = created_columns
        if undo_info:
            project.set_undo_info(
                op_id,
                undo_info,
                commit=not defer_commits,
            )
        run_id = run_store.start_run(
            op_id,
            sheet_id,
            spec["action_kind"],
            action_version=recipe.version,
            model=recipe.run_provenance_model(spec),
            prompt_hash=prompt_hash_of(recipe, persisted_spec),
            params=persisted_spec,
            total_rows=len(row_ids),
            cost_estimate=est["cost"] if est is not None else None,
            row_ids=row_ids,
            commit=not defer_commits,
        )
        # Point only fresh columns early for streaming; reused columns retain
        # their prior values until terminal publication.
        for created_cid in created_columns:
            # Managed history publishes its compatibility pointer atomically.
            has_managed_history = (
                project.db.execute(
                    "SELECT 1 FROM run_output_generations WHERE column_id=? LIMIT 1",
                    (int(created_cid),),
                ).fetchone()
                is not None
            )
            if not has_managed_history:
                run_store.point_column_at_run(op_id, created_cid, run_id, commit=False)
        if created_columns and not defer_commits:
            project.db.commit()
        # Retire dropped output roles instead of exposing stale values.
        retire_dropped_output_columns(
            project,
            op_id=op_id,
            recipe=recipe,
            spec=spec,
            columns=columns,
            current_output_names=set(out_cols),
            commit=not defer_commits,
        )
    run_scope_run_id = run_id if run_store.has_run_row_scope(run_id) else None
    if run_scope_run_id is not None and resume_run_id is not None and not row_ids:
        row_count = run_store.pending_run_row_scope_count(
            run_scope_run_id, sorted(out_cols.values())
        )
    else:
        row_count = len(row_ids)
    return PreparedRun(
        recipe=recipe,
        sheet_id=sheet_id,
        col_map=col_map,
        row_ids=row_ids,
        out_cols=out_cols,
        output_fields=tuple(dict(field) for field in output_fields),
        created_output_column_ids=frozenset(created_columns).intersection(
            out_cols.values()
        ),
        op_id=op_id,
        run_id=run_id,
        row_count=row_count,
        run_scope_run_id=run_scope_run_id,
    )
