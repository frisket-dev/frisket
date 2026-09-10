"""Generate the reserved-maprunner wiring from an `@op` declaration.

This is where the real ceremony collapse lives (the catalog barely collapses — see
declaration.py). The hand-written family module spells out runner_spec/resolve/
precheck/replay/write by hand; here they are derived.

This file contains the pure, DB-free derivations — `runner_spec_fn`,
`reservation_kind`, and the receipt output-ref `kind` discriminator — proven
byte-equal to the hand-written ones. The DB-dependent resolve/precheck/write +
the DB-dependent resolve/precheck/write paths live elsewhere.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    ActionSpec,
    Receipt,
    ReceiptIO,
)
from frisket.sdk.capture import (
    CapturedFacts,
    MixedOriginInputProvenanceUnsupported,
    _input_source_run_id,
    capture_facts,
    maprunner_output_refs,
)
from frisket.sdk.declaration import Op
from frisket.sdk.envelope import StandardReceipt
from frisket.sdk.provenance import Provenance
from frisket.sdk.replay import output_columns_replay_error, replay_expected_row_ids

if TYPE_CHECKING:
    from frisket.engine.executor.action_inventory import _ReservedMaprunnerActionSpec


def build_runner_spec_fn(decl: Op) -> Callable[[Any], dict[str, Any]]:
    """The MapRunner spec the executor feeds to the canonical action runner.

    Generic over the declared source descriptor + passthrough policy. Reproduces
    the hand-written `_map_<op>_runner_spec` exactly.
    """

    def runner_spec_fn(params: Any) -> dict[str, Any]:
        selection = decl.inputs.select(params)
        input_columns = list(selection.names())
        # The public action kind is the runner's sole dispatch identity.
        spec: dict[str, Any] = {"action_kind": decl.kind}
        sheet_id = getattr(params, "sheet_id", None)
        if sheet_id is not None:
            spec["sheet_id"] = sheet_id
        for param, policy in decl.passthrough:
            value = getattr(params, param)
            if policy == "supplied":
                # Supplied versus absent is semantic: only a field
                # the author actually set rides — a materialized pydantic
                # default would make every runner spec carry the knob, and
                # the per-target ability checker would then refuse targets
                # (gateway/moss) whose wire never had it.
                if param in getattr(params, "model_fields_set", ()):
                    spec[param] = value
                continue
            if _include(policy, value):
                spec[param] = value
        if decl.runner_spec_extra is not None:
            spec.update(decl.runner_spec_extra(params))
        # Apply descriptor-owned source facts last so passthrough or an old
        # runner_spec_extra cannot become a second source of input identity.
        for param, value in selection.params().items():
            if param != "input_columns":
                spec[param] = value
        # Role-named inputs (Fields) ride through their role params instead.
        if not decl.omit_empty_input_columns:
            spec["input_columns"] = input_columns
        return spec

    return runner_spec_fn


def _include(policy: str, value: Any) -> bool:
    if policy == "always":
        return True
    if policy == "truthy":
        return bool(value)
    if policy == "not_none":
        return value is not None
    raise ValueError(f"unknown passthrough include policy: {policy!r}")


def _scope_field(decl: Op, params: Any, field: str) -> str:
    if decl.row_scope_policy is not None:
        if field == "sheet_id":
            return "row_scope.sheet_id"
        return "row_scope.selector.membership.row_ids"
    if hasattr(params, field):
        return f"params.{field}"
    if field == "sheet_id":
        return "row_scope.sheet_id"
    return "row_scope.selector.membership.row_ids"


def _scope_value(runner_spec: dict[str, Any], field: str) -> Any:
    """Read canonical row scope after action-envelope projection."""

    return runner_spec.get(field)


def _missing_runner_rows(
    project: Any,
    *,
    decl: Op,
    sheet_id: int,
    runner_spec: dict[str, Any],
) -> list[int]:
    row_ids = _scope_value(runner_spec, decl.row_ids_attr)
    if row_ids is None:
        return []
    found = set(project.visible_row_ids(sheet_id, list(row_ids)))
    return sorted(set(row_ids) - found)


def _rich_input_provenance_error(
    project: Any,
    *,
    decl: Op,
    params: Any,
    sheet_id: int,
    runner_spec: dict[str, Any],
    input_column_ids: dict[str, int],
) -> ActionError | None:
    """Gate a column-level provenance shape before any action effect."""

    if not decl.rich_input_columns:
        return None
    requested_row_ids = _scope_value(runner_spec, decl.row_ids_attr)
    row_ids = (
        project.visible_row_ids(sheet_id)
        if requested_row_ids is None
        else list(requested_row_ids)
    )
    for name, column_id in input_column_ids.items():
        column = project.db.execute(
            "SELECT current_run_id FROM columns WHERE id=?", (column_id,)
        ).fetchone()
        if column is None:
            continue
        try:
            _input_source_run_id(
                project,
                sheet_id=sheet_id,
                column_id=column_id,
                row_ids=row_ids,
                legacy_current_run_id=column["current_run_id"],
            )
        except MixedOriginInputProvenanceUnsupported as exc:
            return ActionError(
                code="invalid_input_ref",
                message=(
                    f"{decl.kind} cannot represent mixed-origin input column provenance"
                ),
                action_kind=decl.kind,
                field=decl.inputs.select(params).field(),
                details=exc.action_error_details(column=name),
            )
    return None


def _resolve_via_descriptor(
    decl: Op,
    project: Any,
    params: Any,
    sheet: Any,
    runner_spec: dict[str, Any],
) -> Any:
    """Resolve from the declarative `inputs=` descriptor (frisket.sdk.inputs):
    validate the declared columns exist + (optionally) type-match, and the row scope.
    Retires the per-op resolve_override for divergent input models. Role-named
    selections key the result by role; the other modes key it by column name."""
    from frisket.contracts.actions.source_inputs import (
        InputSelectionError,
        SourceResolutionError,
        resolve_source_columns,
    )

    try:
        selection = decl.inputs.select(params)
    except InputSelectionError as exc:
        return ActionError(
            code="invalid_input_ref",
            message=f"{decl.kind} source selection is invalid",
            action_kind=decl.kind,
            field=exc.field,
            details={"reason": str(exc)},
        )

    try:
        sources = resolve_source_columns(
            project,
            sheet_id=int(sheet["id"]),
            selected=selection,
            params=params,
        )
    except SourceResolutionError as exc:
        if exc.kind == "required_ai":
            return ActionError(
                code="column_not_ai_generated",
                message=f"{decl.kind} answer to grade must be an AI-generated column",
                action_kind=decl.kind,
                field=exc.field,
                details=exc.details,
            )
        details = exc.details
        if selection.source.mode == "fields" and exc.kind == "type":
            bad = exc.details["columns"]
            if len(bad) == 1:
                details = {
                    "column": bad[0]["name"],
                    "type": bad[0]["type"],
                    "accepted_column_types": exc.details["accepted_column_types"],
                }
        return ActionError(
            code="invalid_input_ref",
            message=(
                f"{decl.kind} input column does not exist on the sheet"
                if exc.kind == "missing"
                else f"{decl.kind} input column type is not accepted"
            ),
            action_kind=decl.kind,
            field=exc.field,
            details=details,
        )

    if selection.source.mode == "fields":
        input_column_ids = dict(sources.by_source)
        input_column_types = dict(sources.column_types_by_source)
    else:
        input_column_ids = sources.column_ids
        input_column_types = sources.column_types

    missing_rows = _missing_runner_rows(
        project,
        decl=decl,
        sheet_id=int(sheet["id"]),
        runner_spec=runner_spec,
    )
    if missing_rows:
        return ActionError(
            code="invalid_input_ref",
            message=f"{decl.kind} row_ids must belong to the target sheet",
            action_kind=decl.kind,
            field=_scope_field(decl, params, decl.row_ids_attr),
            details={"missing": missing_rows},
        )
    return {
        "sheet_id": int(sheet["id"]),
        "input_column_ids": input_column_ids,
        "input_column_types": input_column_types,
    }


def build_resolve_fn(decl: Op) -> Callable[[Any, Any, dict[str, Any]], Any]:
    """Validate sheet visibility, input column(s), and row scope.

    Replaces the per-op `_resolve_map_<op>_inputs` (one of the ~180 lines
    duplicated across every map op). Error messages are templated from the kind +
    param name, matching the hand-written ones. Returns the resolved payload
    `{sheet_id, input_column_ids: {name: id}}`.
    """

    def resolve_fn(project: Any, params: Any, runner_spec: dict[str, Any]) -> Any:
        sheet_id = _scope_value(runner_spec, "sheet_id")
        if type(sheet_id) is not int:
            return ActionError(
                code="invalid_input_ref",
                message=f"{decl.kind} requires a sheet_id",
                action_kind=decl.kind,
                field=_scope_field(decl, params, "sheet_id"),
            )
        sheet = project.db.execute(
            "SELECT * FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
        ).fetchone()
        if sheet is None:
            return ActionError(
                code="invalid_input_ref",
                message=f"{decl.kind} sheet_id does not identify a visible sheet",
                action_kind=decl.kind,
                field=_scope_field(decl, params, "sheet_id"),
            )
        resolved = _resolve_via_descriptor(decl, project, params, sheet, runner_spec)
        if isinstance(resolved, ActionError):
            return resolved
        provenance_error = _rich_input_provenance_error(
            project,
            decl=decl,
            params=params,
            sheet_id=int(sheet["id"]),
            runner_spec=runner_spec,
            input_column_ids=resolved["input_column_ids"],
        )
        if provenance_error is not None:
            return provenance_error
        # No-op unless the op declares the project-aware augmentation hook.
        extra_error = _apply_runner_spec_resolve_extra(
            decl, project, params, resolved, runner_spec
        )
        if extra_error is not None:
            return extra_error
        return resolved

    return resolve_fn


def _apply_runner_spec_resolve_extra(
    decl: Op,
    project: Any,
    params: Any,
    resolved: dict[str, Any],
    runner_spec: dict[str, Any],
) -> ActionError | None:
    """Project-aware runner_spec augmentation (an explicit merge step, per external
    review — not an implicit reliance on dict identity). The op's
    `runner_spec_resolve_extra(project, params, resolved, runner_spec)` returns spec data
    that needs the store at resolve time (e.g. map.judge's traced upstream prompt); it is
    merged into the runner_spec the executor threads to the runner through
    ``action_reservations``. No-op for ops that don't declare it."""
    if decl.runner_spec_resolve_extra is None:
        return None
    extra = decl.runner_spec_resolve_extra(project, params, resolved, runner_spec)
    if isinstance(extra, ActionError):
        return extra
    if extra:
        runner_spec.update(extra)
    return None


def overwrite_exempt_collisions(
    rows: list[Any], runner_spec: dict[str, Any]
) -> list[Any]:
    """Filter output-column-collision rows down to the ones that still block a
    precheck once the caller's overwrite intent is applied.

    An explicit ``overwrite_existing`` output intent, threaded from the
    frontend's "Overwrite existing column" button through
    ``_apply_action_envelope_to_runner_spec`` as ``runner_spec["overwrite"]``,
    reuses a colliding AI-generated output column — the same rule
    ``MapRunner._prepare`` applies. Source (non-AI) columns are never silently
    overwritten, so they still collide even under overwrite.

    Every hand-written ``precheck_override`` across the action families shares
    this rule via this helper instead
    of re-deriving it, so the "overwrite existing column" confirmation actually
    takes effect regardless of which family's precheck runs it. ``rows`` must
    carry an ``ai_generated`` column from the caller's SELECT.
    """
    if not bool(runner_spec.get("overwrite")):
        return rows
    return [row for row in rows if not row["ai_generated"]]


def build_precheck_fn(decl: Op) -> Callable[..., Any]:
    """Refuse to overwrite an existing output column. Replaces the per-op
    `_precheck_map_<op>_output`."""

    def precheck_fn(
        project: Any,
        params: Any,
        runner_spec: dict[str, Any],
        *,
        output_fields: list[dict[str, Any]] | None = None,
    ) -> Any:
        from frisket.engine.runner.validation import recipe_for_spec

        fields = [
            dict(field)
            for field in (
                output_fields
                if output_fields is not None
                else recipe_for_spec(runner_spec).output_fields(runner_spec)
            )
        ]
        output_names = [field["name"] for field in fields]
        fields_by_name = {str(field["name"]): field for field in fields}
        placeholders = ",".join("?" for _ in output_names)
        rows = project.db.execute(
            f"SELECT id, name, ai_generated, type, semantic_type, format FROM columns "
            f"WHERE sheet_id=? AND hidden=0 AND name IN ({placeholders})",
            [runner_spec["sheet_id"], *output_names],
        ).fetchall()
        if output_names:
            rows = overwrite_exempt_collisions(rows, runner_spec)
            from frisket.engine.runner.result_generations import (
                _compatibility_key,
            )

            hidden_managed_incompatible = []
            for row in project.db.execute(
                f"SELECT id,name,type,semantic_type,format,current_run_id "
                "FROM columns "
                f"WHERE sheet_id=? AND hidden=1 AND ai_generated=1 "
                f"AND name IN ({placeholders})",
                [runner_spec["sheet_id"], *output_names],
            ).fetchall():
                managed_history = project.db.execute(
                    "SELECT compatibility_key FROM run_output_generations "
                    "WHERE column_id=? ORDER BY run_id DESC",
                    (int(row["id"]),),
                ).fetchall()
                if not managed_history:
                    hidden_managed_incompatible.append(row)
                    continue
                field = fields_by_name[str(row["name"])]
                expected_key = _compatibility_key(field=field)
                if (
                    any(
                        binding["compatibility_key"] != expected_key
                        for binding in managed_history
                    )
                    or row["type"] != field["column_type"]
                    or row["semantic_type"] != field.get("semantic_type")
                    or row["format"] != field.get("format")
                ):
                    hidden_managed_incompatible.append(row)
            rows = [*rows, *hidden_managed_incompatible]
            if bool(runner_spec.get("overwrite")):
                managed_incompatible = []
                for row in project.db.execute(
                    f"SELECT id, name, type, semantic_type, format, current_run_id "
                    "FROM columns "
                    f"WHERE sheet_id=? AND hidden=0 AND ai_generated=1 "
                    f"AND name IN ({placeholders})",
                    [runner_spec["sheet_id"], *output_names],
                ).fetchall():
                    managed_history = project.db.execute(
                        "SELECT run_id,compatibility_key "
                        "FROM run_output_generations "
                        "WHERE column_id=? ORDER BY run_id DESC",
                        (int(row["id"]),),
                    ).fetchall()
                    field = fields_by_name[str(row["name"])]
                    expected_key = _compatibility_key(field=field)
                    no_generation_history = not managed_history
                    current_binding = (
                        None
                        if row["current_run_id"] is None
                        else project.db.execute(
                            "SELECT 1 FROM run_output_generations "
                            "WHERE run_id=? AND column_id=?",
                            (int(row["current_run_id"]), int(row["id"])),
                        ).fetchone()
                    )
                    intervening_legacy_base = (
                        bool(managed_history)
                        and row["current_run_id"] is not None
                        and current_binding is None
                    )
                    managed_target_refused = bool(managed_history) and (
                        any(
                            binding["compatibility_key"] != expected_key
                            for binding in managed_history
                        )
                        or (
                            row["type"] != field["column_type"]
                            or row["semantic_type"] != field.get("semantic_type")
                            or row["format"] != field.get("format")
                        )
                    )
                    if (
                        no_generation_history
                        or intervening_legacy_base
                        or managed_target_refused
                    ):
                        managed_incompatible.append(row)
                rows = [*rows, *managed_incompatible]
        if rows:
            message = (
                f"{decl.kind} outputs would overwrite existing columns"
                if decl.multi_output
                else f"{decl.kind} output would overwrite an existing column"
            )
            return ActionError(
                code="output_column_exists",
                message=message,
                action_kind=decl.kind,
                field=f"params.{decl.output_name_attr}",
                details={"columns": sorted(row["name"] for row in rows)},
            )
        return None

    return precheck_fn


def reservation_kind(decl: Op) -> str:
    return f"{decl.underscored}_idempotency_reservation"


def output_ref_kind(decl: Op) -> str:
    # e.g. "map_classify_output_column" — the discriminator the replay helper and
    # provenance UI key on.
    return f"{decl.underscored}_output_column"


def build_replay_fn(decl: Op) -> Callable[[Any, Any, Receipt, ActionSpec], Any]:
    out_kind = output_ref_kind(decl)
    # Scope is contract-derived, not a separate knob: an op whose
    # `accepted_run_statuses` tolerates anything besides a clean "completed"
    # run (the model five, ner, ...) trusts the receipt's own recorded row set
    # (scope="receipt") instead of demanding the receipt cover the CURRENT
    # full expected scope — a partial run's replay must not be penalized for
    # rows it never touched. Those ops also keep their own map_error_code on
    # the wire (today's light checker's contract); completed-only ops keep the
    # default "stale_replay".
    partial_tolerant = decl.accepted_run_statuses != ("completed",)

    def replay_error_fn(
        project: Any,
        params: Any,
        receipt: Receipt,
        action: ActionSpec,
    ) -> Any:
        if partial_tolerant:
            return output_columns_replay_error(
                project,
                receipt,
                output_kind=out_kind,
                action_kind=decl.kind,
                scope="receipt",
                error_code=decl.map_error_code,
                allow_empty_rows=decl.kind == "map.ner",
            )
        if decl.row_scope_policy is not None:
            if action.row_scope is None:
                raise ValueError(f"{decl.kind} replay requires its canonical row scope")
            sheet_id = int(action.row_scope.sheet_id)
            selector = action.row_scope.selector
            requested_row_ids = (
                list(selector.membership.row_ids)
                if selector.kind == "exact_membership"
                else None
            )
        else:
            sheet_id = int(params.sheet_id)
            requested_row_ids = getattr(params, decl.row_ids_attr, None)
        return output_columns_replay_error(
            project,
            receipt,
            output_kind=out_kind,
            action_kind=decl.kind,
            scope="expected",
            expected_row_ids=replay_expected_row_ids(
                project,
                sheet_id=sheet_id,
                requested_row_ids=requested_row_ids,
            ),
        )

    return replay_error_fn


def _present_with_resolved(
    present: Any,
    project: Any,
    facts: Any,
    base_output_refs: list[dict[str, Any]],
    resolved: Any,
) -> Any:
    """Call an op's present(), threading ``resolved`` only when it opts in.

    Op receipt builders that need resolved input facts accept a ``resolved``
    keyword (or **kwargs); the rest keep the three-arg signature unchanged.
    """
    try:
        params = inspect.signature(present).parameters
    except (TypeError, ValueError):
        # Some callables (builtins/C-extensions) have no introspectable signature;
        # fall back to the stable three-arg call rather than crash.
        return present(project, facts, base_output_refs)
    resolved_param = params.get("resolved")
    accepts = (
        resolved_param is not None
        and resolved_param.kind is not inspect.Parameter.POSITIONAL_ONLY
    ) or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    if accepts:
        return present(project, facts, base_output_refs, resolved=resolved)
    return present(project, facts, base_output_refs)


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

    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action_kind,
        run_id=facts.run_id,
        op_ids=[facts.op_id],
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
        evidence=provenance.evidence,
        errors=provenance.errors,
        warnings=provenance.warnings,
    )


def build_receipt_and_outputs(
    decl: Op,
    project: Any,
    action: Any,
    *,
    runner_spec: dict[str, Any],
    params_hash: str,
    project_id: str,
    action_id: str,
    receipt_id: str,
    run_id: int,
    input_column_ids: dict[str, int],
    input_column_types: dict[str, str],
    resolved: Any = None,
) -> Any:
    """B/B′: run guard -> capture -> present -> project. Returns
    `(receipt, outputs, op_id, row_ids)` on success, or a failed `ActionResult`.

    Shared by the reserved write skeleton and the queued sync/finalize paths so the
    receipt content is produced one way for every lifecycle. ``resolved`` is the
    threaded ResolvedAction; present() opts in to it by accepting a ``resolved``
    keyword (or **kwargs).
    """
    from frisket.engine.executor.action_support import _failed_result

    out_kind = output_ref_kind(decl)
    # present(project, facts, base_output_refs, [resolved]) -> Provenance; a
    # StandardReceipt declares the envelope's fields and binds to the decl here.
    present = decl.receipt
    if isinstance(present, StandardReceipt):
        present = present.bind(decl)

    run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None or run["status"] not in decl.accepted_run_statuses:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code=decl.map_error_code,
                message=f"{decl.kind} run did not complete",
                action_kind=action.kind,
                details={"run_status": run["status"] if run else None},
            ),
        )
    facts = capture_facts(
        project,
        decl,
        runner_spec=runner_spec,
        run_id=run_id,
        params_hash=params_hash,
        input_column_ids=input_column_ids,
        input_column_types=input_column_types,
    )
    if facts.missing_outputs:
        noun = "output columns" if decl.multi_output else "output column"
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code=decl.map_error_code,
                message=f"{decl.kind} run did not create the expected {noun}",
                action_kind=decl.kind,
                details=(
                    {"columns": facts.missing_outputs}
                    if decl.multi_output
                    else {"column": facts.missing_outputs[0]}
                ),
            ),
        )
    op_id = facts.op_id
    sheet_id = facts.sheet_id
    row_ids = facts.row_ids
    base_output_refs = maprunner_output_refs(facts, output_kind=out_kind)
    prov = _present_with_resolved(present, project, facts, base_output_refs, resolved)
    receipt = build_receipt_from_provenance(
        prov,
        facts,
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        idempotency_key=action.idempotency_key,
    )
    outputs = [
        ActionOutput(
            kind="column",
            name=ref["name"],
            sheet_id=sheet_id,
            column_id=ref["column_id"],
            row_ids=row_ids,
            ref=ref,
        )
        for ref in prov.output_refs
    ]
    # Named-result outputs (list column -> child table seam) follow the columns, in
    # the order the op's present() advertised them.
    outputs.extend(
        ActionOutput(
            kind="named_result",
            name=ref["route"],
            sheet_id=ref["sheet_id"],
            column_id=ref["column_id"],
            row_ids=list(ref.get("row_ids") or []),
            ref=ref,
        )
        for ref in prov.named_result_refs
    )
    return receipt, outputs, op_id, row_ids


def build_write_fn(
    decl: Op,
    spec_holder: dict[str, Any],
    *,
    before_commit: Callable[..., None] | None = None,
) -> Callable[..., Any]:
    """The generic write skeleton: everything except the op-specific receipt content
    (which comes from `decl.receipt`). Reproduces the hand-written
    `_write_map_<op>_receipt` exactly."""

    # Lazy import: avoids a cycle (executor -> maps -> this module -> executor).
    # By call time (spec build at family-module load) action_reservations is loaded.
    from frisket.engine.executor.action_reservations import (
        _direct_action_finalize_metadata,
        _finalize_direct_reserved_action_receipt,
        _reserved_maprunner_result_from_existing,
    )

    def write_fn(
        project: Any,
        action: Any,
        params: Any,
        *,
        runner_spec: dict[str, Any],
        params_hash: str,
        project_id: str,
        action_id: str,
        receipt_id: str,
        run_id: int,
        resolved: Any,
        require_running_status: bool = True,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
    ) -> Any:
        # Input column facts come from the threaded ResolvedAction. The QUEUED
        # worker-finalize path carries only an op's declared payload codecs (e.g.
        # input_column_ids) and not input_column_types; the sync path carries both.
        facts = resolved.facts
        input_column_ids = facts["input_column_ids"]
        input_column_types = facts.get("input_column_types") or {}
        built = build_receipt_and_outputs(
            decl,
            project,
            action,
            runner_spec=runner_spec,
            params_hash=params_hash,
            project_id=project_id,
            action_id=action_id,
            receipt_id=receipt_id,
            run_id=run_id,
            input_column_ids=input_column_ids,
            input_column_types=input_column_types,
            resolved=resolved,
        )
        if not isinstance(built, tuple):
            return built  # failed ActionResult
        receipt, outputs, op_id, row_ids = built
        finalize_result = _finalize_direct_reserved_action_receipt(
            project,
            action,
            params_hash=params_hash,
            project_id=project_id,
            receipt=receipt,
            finalize=_direct_action_finalize_metadata(
                decl.kind,
                result_from_existing_fn=_reserved_maprunner_result_from_existing(
                    spec_holder["spec"]
                ),
                result_from_existing_kwargs=(
                    {"params": params} if decl.finalize_passes_params else None
                ),
                require_running_status=require_running_status,
            ),
            before_commit=(
                lambda: (
                    before_commit(
                        project=project,
                        action=action,
                        params=params,
                        runner_spec=runner_spec,
                        receipt=receipt,
                        outputs=outputs,
                        op_id=op_id,
                        row_ids=row_ids,
                        run_id=run_id,
                        resolved=resolved,
                        writer_attempt_id=writer_attempt_id,
                        claim_token=claim_token,
                    )
                    if before_commit is not None
                    else None
                )
            ),
        )
        if finalize_result is not None:
            return finalize_result
        return ActionResult(
            action=ActionIdentity(kind=action.kind, action_id=action_id),
            status=receipt.status,
            project_id=project_id,
            run_id=run_id,
            op_ids=[op_id],
            outputs=outputs,
            receipt_id=receipt_id,
            errors=receipt.errors,
            warnings=receipt.warnings,
        )

    return write_fn


def build_reserved_spec(
    decl: Op,
    *,
    before_commit: Callable[..., None] | None = None,
) -> _ReservedMaprunnerActionSpec:
    """Assemble the whole reserved-maprunner spec from one `@op` declaration.

    This is the ceremony collapse: ~200 lines of per-op resolve/precheck/
    runner_spec/replay/write boilerplate (plus ~180 duplicated replay lines) become
    this call.
    """
    from frisket.engine.executor.action_inventory import _ReservedMaprunnerActionSpec
    from frisket.engine.executor.action_support import (
        _model_cost_requires_confirmation_error,
        _params_hash_without_confirmed,
    )

    # Cost confirmation (additive; non-confirming ops leave these None). Three independent
    # shapes: requires_confirmation (model cost-gated) strips confirmed from the hash, threads
    # it to the runner, AND installs the model cost-gate error; threads_confirmed (local
    # confirm-gated ops, e.g. media to_markdown/video_frames/extract_faces) does the first two
    # WITHOUT the cost gate (a CostGate stays the op's own map_error_code); needs_confirmation
    # (external ops) is a preflight gate instead.
    #
    # A FOURTH shape, derived rather than flagged: an op whose declared cost is
    # UNPRICEABLE (`CostPolicy(kind="unknown")` — api_call, fetch_url,
    # ytdlp_download, research.answer). Its estimate can never produce a number,
    # so the runner gates it on an unknown cost every time; without threading and
    # a cost-gate error that op would be permanently UNRUNNABLE (the gate asks
    # for a `confirmed` its params model forbids) or, as it was before, silently
    # priced at $0.00 and never gated at all. One declaration drives all three
    # wires, so a new unpriceable op cannot be declared half-wired.
    unpriceable = decl.cost.kind == "unknown"
    threads_confirmation = (
        decl.requires_confirmation
        or decl.threads_confirmed
        or decl.needs_confirmation_error is not None
        or unpriceable
    )
    confirmation: dict[str, Any] = {}
    if threads_confirmation:
        confirmation["params_hash_fn"] = _params_hash_without_confirmed
        # Every confirmation shape threads the caller's REAL consent to the
        # runner: the needs_confirmation preflight ops
        # (geocode/census/web_search) previously relied on the runtime
        # seams' blanket confirmed=True fallback, which disarmed the
        # MapRunner cost gate; those seams now default False, and this
        # threads the actual params.confirmed instead.
        confirmation["confirmed_fn"] = lambda params: params.confirmed
    if decl.requires_confirmation or unpriceable:
        confirmation["cost_gate_error_fn"] = _model_cost_requires_confirmation_error
    # OP-UNIQUE cost-gate error (run.backfill): a fresh scoped op that pairs
    # threads_confirmed with its own CostGate->ActionError so the gate surfaces
    # as the 402 needs_confirmation envelope (marks needs_confirmation=True),
    # not the requires_confirmation model gate's status="failed" direct path.
    if decl.cost_gate_error_override is not None:
        confirmation["cost_gate_error_fn"] = decl.cost_gate_error_override
    if decl.needs_confirmation_error is not None:
        confirmation["needs_confirmation_error_fn"] = decl.needs_confirmation_error

        def external_cost_gate_error(action: ActionSpec, exc: Any) -> ActionError:
            from frisket.engine.executor.action_support import _claims_gate_details

            params = decl.params_model.model_validate(action.params)
            base = decl.needs_confirmation_error(action, params)
            return base.model_copy(
                update={
                    "needs_confirmation": True,
                    "details": {
                        **dict(base.details),
                        "reason": (
                            "unknown_estimate"
                            if exc.estimate is None
                            else "external_metered"
                        ),
                        "estimate": exc.estimate_details or exc.estimate,
                        **_claims_gate_details(exc),
                    },
                }
            )

        # The MapRunner is the single 402 authority: it binds the exact row
        # scope and full estimate into the hash. The declaration's preflight
        # error supplies the public, action-specific code/copy only after that
        # gate fires; a boolean-only early gate cannot mint a trustworthy hash.
        confirmation["cost_gate_error_fn"] = external_cost_gate_error

    holder: dict[str, Any] = {}
    # OP-UNIQUE write hook (map.extract grounding): a (decl, holder) -> write_fn factory
    # so it can read holder["spec"] for result_from_existing, exactly like the generic
    # skeleton. Defaults to the generic build_write_fn for every other op.
    write_fn = (
        decl.write_override(decl, holder)
        if decl.write_override is not None
        else build_write_fn(decl, holder, before_commit=before_commit)
    )
    spec = _ReservedMaprunnerActionSpec(
        kind=decl.kind,
        params_model=decl.params_model,
        runner_spec_fn=build_runner_spec_fn(decl),
        # TRANSITIONAL: external ops (geocode) provide their own resolve/replay.
        resolve_fn=decl.resolve_override or build_resolve_fn(decl),
        precheck_fn=decl.precheck_override or build_precheck_fn(decl),
        write_fn=write_fn,
        reservation_kind=reservation_kind(decl),
        resume_run_id_fn=decl.resume_run_id_fn,
        replay_error_fn=decl.replay_override or build_replay_fn(decl),
        map_error_code=decl.map_error_code,
        log_missed_delete=decl.log_missed_delete,
        **confirmation,
    )
    holder["spec"] = spec
    return spec
