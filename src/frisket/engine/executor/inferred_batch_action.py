"""One-pass scalar batch inference, followed by ordinary managed publication."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import inspect
import json
import math
import pickle
import tempfile
from dataclasses import replace
from functools import partial
from types import MappingProxyType

from frisket.actions.types import Row, Rows, RowError, RowResult
from frisket.contracts.action import ActionError, ActionIdentity, ActionResult
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_support import _failed_result
from frisket.engine.executor.map_rows_action import (
    TypedMapRowsPlanError,
    _TypedMapBatchProgram,
    _TypedExecutionEnvelope,
    _require_expected_output_columns,
    _typed_plan_error,
    _typed_receipt,
    _typed_replay_error,
    build_typed_map_rows_plan,
    materialized_row_output_fields,
    resolve_row_output_targets,
    typed_request_hash,
    typed_map_rows_replay_result,
)


def scalar_kind(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise ValueError("Formula returned a non-finite number")
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, dt.datetime):
        return "datetime"
    if isinstance(value, dt.date):
        return "date"
    raise ValueError(
        "Formula must return a scalar text, number, boolean, date, datetime or blank"
    )


def _source_digest(project, plan, row_ids):
    """Hash bounded reads, including cell provenance and source identities."""
    digest = hashlib.sha256()
    digest.update(json.dumps(dict(plan.source_column_ids), sort_keys=True).encode())
    for name, column_id in plan.source_column_ids.items():
        column = project.get_column(column_id)
        digest.update(
            json.dumps(
                {key: column[key] for key in ("id", "name", "type", "active")},
                sort_keys=True,
                default=str,
            ).encode()
        )
        for start in range(0, len(row_ids), 1000):
            ids = row_ids[start : start + 1000]
            values = project.get_values_with_refs(
                plan.request.scope.sheet_id, column_id, row_ids=ids
            )
            digest.update(
                json.dumps(
                    values, sort_keys=True, default=str, allow_nan=False
                ).encode()
            )
    digest.update(json.dumps(row_ids).encode())
    return digest.hexdigest()


class StagedScopeChanged(Exception):
    """Staged values no longer cover the exact admitted row scope."""


class _StagedBatchProgram(_TypedMapBatchProgram):
    allow_all_empty_input = True
    defer_generation_seal = True
    keep_zero_success_output_columns = True

    def bind_stage(self, spool, batch_size):
        self._spool = spool
        self.batch_size = batch_size
        self._staged_rows_remaining = 0

    def close_stage(self):
        if not self._spool.closed:
            self._spool.close()

    async def execute_batch(self, values_by_row, spec, ctx):
        del spec, ctx
        result = {}
        try:
            for expected_id in values_by_row:
                try:
                    row_id, error, value, warnings = pickle.load(self._spool)
                except EOFError as exc:
                    raise StagedScopeChanged(
                        "staged row scope changed before publication"
                    ) from exc
                if row_id != expected_id:
                    raise StagedScopeChanged(
                        "staged row scope changed before publication"
                    )
                produced = (
                    RowError(*error)
                    if error
                    else RowResult(
                        output=self._terminal.output_model.model_construct(
                            **{self._resolved_output_fields[0].key: value}
                        ),
                        warnings=warnings,
                    )
                )
                result[row_id] = (
                    self._row_error_publication(produced)
                    if error
                    else self._prepare_publication(produced)
                )
                self._staged_rows_remaining -= 1
        except BaseException:
            self.close_stage()
            raise
        if self._staged_rows_remaining == 0:
            self.close_stage()
        return result


def stage_plan(project, plan, spool, *, should_cancel=lambda: False):
    from frisket.engine.runner.validation import target_rows

    row_ids = target_rows(project, plan.spec_dict())
    before = _source_digest(project, plan, row_ids)
    kinds = {}
    successes = 0
    terminal = plan.action.definition.run
    key = plan.program._resolved_output_fields[0].key
    for start in range(0, len(row_ids), terminal.batch_size):
        if should_cancel():
            raise InterruptedError("Formula cancelled before publication")
        ids = row_ids[start : start + terminal.batch_size]
        columns = {
            name: project.get_values(
                plan.request.scope.sheet_id, column_id, row_ids=ids
            )
            for name, column_id in plan.source_column_ids.items()
        }
        values = {}
        for row_id in ids:
            row = {}
            for name, cells in columns.items():
                value = cells.get(row_id)
                if plan.source_column_types[name] in {
                    "date",
                    "datetime",
                } and value not in (None, ""):
                    try:
                        value = (
                            dt.datetime.fromisoformat(value)
                            if "T" in value or " " in value
                            else dt.date.fromisoformat(value)
                        )
                    except (TypeError, ValueError):
                        raise ValueError(
                            f"Invalid date in column {name!r}, row {row_id}"
                        ) from None
                row[name] = value
            values[row_id] = Row(row)
        produced = terminal.handler(plan.program._params, Rows(values))
        if inspect.isawaitable(produced):
            produced = asyncio.run(produced)
        if set(produced) != set(ids):
            raise ValueError(
                "inferred batch must return exactly one result for each input row"
            )
        for row_id in ids:
            outcome = produced[row_id]
            if isinstance(outcome, RowError):
                pickle.dump((row_id, (outcome.code, outcome.message), None, ()), spool)
                continue
            if not isinstance(outcome, RowResult):
                raise TypeError("batch handler must return RowResult or RowError")
            output = terminal.output_model.model_validate(outcome.output)
            value = getattr(output, key)
            kind = scalar_kind(value)
            if kind:
                kinds.setdefault(kind, row_id)
            if len(kinds) > 1:
                examples = ", ".join(
                    f"{kind} (row {example})" for kind, example in kinds.items()
                )
                raise ValueError(
                    f"Formula returns mixed types: {examples}. Return one type, use a blank, or convert the numeric result to text."
                )
            successes += 1
            pickle.dump(
                (row_id, None, None if kind is None else value, outcome.warnings), spool
            )
    if not successes:
        raise ValueError(
            "Formula did not succeed on any row; no output column was created"
        )
    if should_cancel():
        raise InterruptedError("Formula cancelled before publication")
    if before != _source_digest(project, plan, row_ids):
        raise ValueError("Formula source values changed before publication")
    kind = next(iter(kinds), "text")
    field = replace(
        plan.program._resolved_output_fields[0],
        column_type=kind,
        schema={
            "type": {
                "text": "string",
                "number": "number",
                "boolean": "boolean",
                "date": "string",
                "datetime": "string",
            }[kind]
        },
    )
    fields = materialized_row_output_fields((field,), plan.output_names)
    program = _StagedBatchProgram(
        plan.action,
        plan.program._params,
        plan.output_names,
        (field,),
        fields,
        None,
        None,
    )
    program.bind_project(project)
    spool.seek(0)
    program.bind_stage(spool, terminal.batch_size)
    program._staged_rows_remaining = len(row_ids)
    staged = replace(
        plan,
        output_fields=fields,
        program=program,
        spec=MappingProxyType(
            {
                **plan.spec,
                "row_ids": row_ids,
                **(
                    {"inferred_batch_full_scope": True}
                    if plan.request.scope.row_ids is None
                    else {}
                ),
            }
        ),
    )
    return staged, row_ids, before


def run_inferred_batch(
    project,
    project_id,
    bound,
    router,
    map_runner_factory,
    *,
    reservation=None,
    should_cancel=lambda: False,
):
    from frisket.engine.executor.action_lifecycle import (
        _PreparedMapExecution,
        _run_reserved_maprunner_action,
    )
    from frisket.engine.executor.action_reservations import (
        _delete_reserved_action_receipt,
        _reserve_running_action_receipt,
    )

    queued_reservation = reservation is not None
    if reservation is None:
        replay = typed_map_rows_replay_result(project, project_id, bound)
        if replay is not None:
            return replay
    action = _TypedExecutionEnvelope(
        bound.action.action_id,
        bound.request.idempotency_key,
        MappingProxyType(dict(bound.request.params)),
    )
    params_hash = typed_request_hash(bound)
    if reservation is None:
        reservation = _reserve_running_action_receipt(
            project,
            action,
            params_hash=params_hash,
            project_id=project_id,
            reservation_kind="typed_map_rows_idempotency_reservation",
            replay_error_fn=_typed_replay_error(project, bound),
        )
        if isinstance(reservation, ActionResult):
            return reservation
    spool = None
    try:
        plan = build_typed_map_rows_plan(project, bound, _admit_output_targets=False)
        spool = tempfile.TemporaryFile()
        plan, row_ids, source_digest = stage_plan(
            project, plan, spool, should_cancel=should_cancel
        )
        # The output descriptor is known only after the one evaluation pass.
        # Admit replacement and expected-target preconditions against that
        # actual descriptor, before preparing a managed run.
        field = plan.program._resolved_output_fields[0]
        _, preconditions = resolve_row_output_targets(project, bound.request, (field,))
        _require_expected_output_columns(bound.request, preconditions)
        if project.visible_row_ids(bound.request.scope.sheet_id, row_ids) != row_ids:
            raise TypedMapRowsPlanError(
                "stale_input", "Formula row scope changed after staging"
            )
        if (
            bound.request.replace_existing
            and bound.request.scope.row_ids is None
            and project.visible_row_ids(bound.request.scope.sheet_id) != row_ids
        ):
            raise TypedMapRowsPlanError(
                "stale_input", "Formula row scope changed before replacement"
            )
        spec = {**plan.spec, "output_target_preconditions": preconditions}
        plan = replace(
            plan,
            output_target_preconditions=MappingProxyType(preconditions),
            spec=MappingProxyType(spec),
        )
    except Exception as exc:
        from frisket.engine.runner.validation import (
            InvalidTargetRows,
            InvalidTargetSheet,
        )

        if spool is not None:
            spool.close()
        if not queued_reservation:
            _delete_reserved_action_receipt(
                project, reservation["receipt_id"], action_kind=bound.action.action_id
            )
        if isinstance(exc, InterruptedError):
            return ActionResult(
                action=ActionIdentity(
                    kind=bound.action.action_id, action_id=reservation["action_id"]
                ),
                status="cancelled",
                project_id=project_id,
                errors=[
                    ActionError(
                        code="cancelled",
                        message=str(exc),
                        action_kind=bound.action.action_id,
                    )
                ],
            )
        if isinstance(exc, InvalidTargetSheet):
            error = ActionError(
                code="invalid_input_ref",
                message=str(exc),
                action_kind=bound.action.action_id,
                field="scope.sheet_id",
                details={"sheet_id": exc.sheet_id},
            )
        elif isinstance(exc, InvalidTargetRows):
            error = ActionError(
                code="invalid_input_ref",
                message=str(exc),
                action_kind=bound.action.action_id,
                field="scope.row_ids",
                details={"missing": exc.missing},
            )
        elif isinstance(exc, TypedMapRowsPlanError):
            error = _typed_plan_error(bound.action.action_id, exc)
        else:
            error = ActionError(
                code="project_write_failed"
                if isinstance(exc, OSError)
                else "invalid_params",
                message="Formula staging failed"
                if isinstance(exc, OSError)
                else str(exc),
                action_kind=bound.action.action_id,
            )
        return _failed_result(
            project_id=project_id,
            action_kind=bound.action.action_id,
            error=error,
        )

    def factory(current, current_router):
        runner = map_runner_factory(current, current_router)
        original = runner.should_cancel
        runner.should_cancel = lambda run_id: (
            should_cancel() or (original(run_id) if original else False)
        )
        runner.allow_action_lifecycle_only_recipes = True
        return runner

    try:
        return _run_reserved_maprunner_action(
            project,
            action,
            bound.params,
            project_id=project_id,
            router=router,
            map_runner_factory=factory,
            resolved_execution=_PreparedMapExecution(
                plan.spec,
                plan.output_fields,
                plan.program,
                params_hash,
                partial(
                    _typed_receipt,
                    project_id=project_id,
                    plan=plan,
                    params_hash=params_hash,
                ),
                _typed_replay_error(project, bound),
                "typed_map_rows_idempotency_reservation",
                reservation,
                queued_reservation_owner=queued_reservation,
            ),
            write_fn=partial(
                _publish,
                bound=bound,
                plan=plan,
                row_ids=row_ids,
                source_digest=source_digest,
                should_cancel=should_cancel,
            ),
            output_claim_error_field="output_names",
        )
    finally:
        spool.close()


def _publish(
    project,
    action,
    _params,
    *,
    bound,
    plan,
    row_ids,
    source_digest,
    should_cancel,
    **kwargs,
):
    from frisket.engine.executor.action_reservations import (
        _finalize_reserved_action_receipt,
    )
    from frisket.engine.executor.project_run_terminalization import (
        CurrentWriterTerminalAuthority,
        terminalize_project_run,
    )
    from frisket.engine.store.output_claims import OutputColumnClaimStore
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.store.result_generations import ResultGenerationStore
    from frisket.execution.attempt import StaleAttemptWriter, set_attempt_state

    run_id, receipt_id = kwargs["run_id"], kwargs["receipt_id"]
    writer, claim = kwargs["writer_attempt_id"], kwargs["claim_token"]
    project.db.execute("BEGIN IMMEDIATE")
    try:
        run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        claims = OutputColumnClaimStore(project)
        column_ids = claims.active_column_ids(claim_token=claim, run_id=run_id)
        claims.require_current_writer(
            project.db,
            run_id=run_id,
            writer_attempt_id=writer,
            claim_token=claim,
            output_column_ids=column_ids,
        )
        error = None
        cancelled = should_cancel() or run["status"] == "cancelled"
        if cancelled:
            error = ActionError(
                code="cancelled",
                message="Formula cancelled before publication",
                action_kind=action.kind,
            )
        elif run["status"] not in {"completed", "partial"}:
            error = ActionError(
                code="formula_failed",
                message="Formula failed before publication",
                action_kind=action.kind,
            )
        elif project.visible_row_ids(bound.request.scope.sheet_id, row_ids) != row_ids:
            error = ActionError(
                code="stale_input",
                message="Formula row scope changed after staging",
                action_kind=action.kind,
            )
        elif (
            bound.request.replace_existing
            and bound.request.scope.row_ids is None
            and project.visible_row_ids(bound.request.scope.sheet_id) != row_ids
        ):
            error = ActionError(
                code="stale_input",
                message="Formula row scope changed before replacement",
                action_kind=action.kind,
            )
        elif source_digest != _source_digest(project, plan, row_ids):
            error = ActionError(
                code="stale_input",
                message="Formula source values changed before publication",
                action_kind=action.kind,
            )
        if error:
            status = "cancelled" if cancelled else "failed"
            prior = ReceiptStore(project).parsed_by_id(receipt_id)
            receipt = prior.model_copy(
                update={"status": status, "outputs": [], "errors": [error]}
            )
            outcome = terminalize_project_run(
                project,
                run_id=run_id,
                receipt_id=receipt_id,
                status=status,
                authority=CurrentWriterTerminalAuthority(
                    writer_attempt_id=writer, claim_token=claim
                ),
                terminal_receipt=receipt,
                commit=False,
            )
            if outcome.disposition not in {"terminalized", "already_terminal"}:
                raise StaleAttemptWriter("Formula cancellation lost its current writer")
        else:
            ResultGenerationStore(project).seal(
                run_id,
                column_ids,
                claim_token=claim,
                terminal_disposition=run["status"],
                publish=True,
                commit=False,
            )
            op = project.db.execute(
                "SELECT undo_info FROM ops WHERE id=?", (run["op_id"],)
            ).fetchone()
            for column_id in json.loads(op["undo_info"] or "{}").get(
                "created_columns", []
            ):
                if column_id in column_ids:
                    project.db.execute(
                        "UPDATE columns SET hidden=0 WHERE id=?", (column_id,)
                    )
            receipt = _typed_receipt(
                project,
                run_id,
                kwargs["action_id"],
                receipt_id,
                project_id=kwargs["project_id"],
                plan=plan,
                params_hash=kwargs["params_hash"],
            )
            conflict = _finalize_reserved_action_receipt(
                project,
                action,
                params_hash=kwargs["params_hash"],
                project_id=kwargs["project_id"],
                receipt=receipt,
                reservation_lost_message="Formula reservation lost",
                update_failed_message="Formula receipt update failed",
                require_running_status=False,
                replay_error_fn=_typed_replay_error(project, bound),
                commit=False,
            )
            if conflict:
                project.db.rollback()
                return conflict
            set_attempt_state(project, writer, "effected", commit=False)
            if not claims.release(claim_token=claim, status="released", commit=False):
                raise StaleAttemptWriter("Formula publication lost its output claim")
        project.db.commit()
        return _result_from_receipt(receipt)
    except BaseException:
        project.db.rollback()
        raise
