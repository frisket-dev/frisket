"""Reserved typed semantic join with one source/child publication transaction."""

from __future__ import annotations

import json
from dataclasses import replace
from types import MappingProxyType

from frisket.contracts.action import ActionError, ReceiptEvidence, ReceiptIO
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_reservations import (
    _finalize_reserved_action_receipt,
    _output_claim_token,
    _receipt_for_idempotency,
)
from frisket.engine.executor.map_rows_action import (
    _TypedExecutionEnvelope,
    _typed_map_rows_plan,
    _typed_model_cost_error,
    _typed_plan_error,
    _typed_receipt,
    build_typed_map_rows_plan,
    typed_request_hash,
)
from frisket.engine.executor.semantic_join_program import (
    SemanticJoinSourceChanged,
    semantic_join_references,
    validate_semantic_join_pins,
)
from frisket.engine.runner.row_inputs import row_source_snapshot
from frisket.engine.store.cell_writes import create_base_cell_producer
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.execution.attempt import StaleAttemptWriter, set_attempt_state


def _resolve(project, bound, spec, router, *, admission=True):
    from frisket.semantic import embedder_is_remote, resolve_embedder
    from frisket.engine.runner.validation import assert_provider_spend_cap

    existing = _receipt_for_idempotency(project, bound.request.idempotency_key)
    plan = build_typed_map_rows_plan(
        project,
        bound,
        _admit_output_targets=admission,
        _allow_existing_outputs=bool(
            existing is not None and existing["status"] == "queued"
        ),
    )
    source, target, carry = semantic_join_references(
        bound.action.definition.run, bound.params
    )
    sheet_id = bound.request.scope.sheet_id
    if target.sheet_id == sheet_id:
        raise ValueError("semantic join requires a different target sheet")
    sheets = {sheet["id"]: dict(sheet) for sheet in project.sheets()}
    if target.sheet_id not in sheets:
        raise ValueError("semantic join target sheet is missing")
    target_column = next(
        (
            dict(col)
            for col in project.columns(target.sheet_id)
            if col["name"] == target.column
        ),
        None,
    )
    if target_column is None:
        raise ValueError("semantic join target column is missing")
    target_values = project.get_values(target.sheet_id, target_column["id"])
    if not any(value not in (None, "") for value in target_values.values()):
        raise ValueError("semantic join target column has no values")
    child_name = bound.request.sheet_name
    if not child_name or (
        admission and any(sheet["name"] == child_name for sheet in sheets.values())
    ):
        raise ValueError("semantic join needs an unused result sheet name")
    backend = resolve_embedder(router, allow_remote=True)
    if backend is None:
        raise ValueError("semantic join embedding backend is unavailable")
    if embedder_is_remote(backend[1]):
        from frisket.ai.llm.types import provider_from_model_id

        assert_provider_spend_cap(project, provider_from_model_id(backend[1]))
    source_columns = {col["name"]: dict(col) for col in project.columns(sheet_id)}
    row_ids = project.visible_row_ids(sheet_id, bound.request.scope.row_ids)
    target_rows = project.visible_row_ids(target.sheet_id)
    child_names = {
        "source": bound.request.output_names.get("source", "source"),
        **{
            f"carry.{ref.name}": bound.request.output_names.get(
                f"carry.{ref.name}", f"carry.{ref.name}"
            )
            for ref in carry
        },
        **dict(plan.output_names),
    }
    if len(child_names.values()) != len(set(child_names.values())):
        raise ValueError("semantic join child output names must be distinct")
    state = {
        "source": {
            "sheet_id": sheet_id,
            "name": sheets[sheet_id]["name"],
            "row_ids": row_ids,
            "columns": dict(plan.source_column_ids),
            "snapshot": row_source_snapshot(
                project, sheet_id, plan.source_column_ids, row_ids=row_ids
            ),
        },
        "target": {
            "sheet_id": target.sheet_id,
            "name": sheets[target.sheet_id]["name"],
            "row_ids": target_rows,
            "columns": {target.column: target_column["id"]},
            "snapshot": row_source_snapshot(
                project, target.sheet_id, {target.column: target_column["id"]}
            ),
        },
        "source_column": source_columns[source.name],
        "target_column": target_column,
        "carry_columns": [source_columns[ref.name] for ref in carry],
        "child_name": child_name,
        "child_names": child_names,
        "embedding_model": backend[1],
        "input_column_ids": dict(plan.source_column_ids),
        "input_column_types": dict(plan.source_column_types),
    }
    spec.clear()
    spec.update(plan.spec_dict())
    spec.update(
        semantic_join=state,
        engine=backend[1],
        deferred_publication=True,
        idempotency_key=bound.request.idempotency_key,
    )
    return {
        "semantic_join": state,
        "resume_run_id": _semantic_resume_run_id(project, bound, state),
        "input_column_ids": dict(plan.source_column_ids),
        "input_column_types": dict(plan.source_column_types),
        "output_names": dict(plan.output_names),
        "output_target_preconditions": dict(plan.output_target_preconditions),
    }


def _replay_error(project, receipt):
    from frisket.sdk.replay import output_columns_replay_error

    if receipt.status in {"queued", "running"}:
        return None
    if receipt.status in {"failed", "cancelled"} and not any(
        item.ref.get("kind") == "semantic_join_output_column"
        for item in receipt.outputs
    ):
        return None
    try:
        stored = project.db.execute(
            "SELECT ops.spec FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.id=?",
            (receipt.run_id,),
        ).fetchone()
        state = json.loads(stored["spec"])["semantic_join"]
        validate_semantic_join_pins(
            project,
            state,
            replaced_source_columns={
                item.ref["column_id"]
                for item in receipt.outputs
                if item.ref.get("kind") == "semantic_join_output_column"
            },
        )
    except (KeyError, TypeError, ValueError) as exc:
        return ActionError(
            code="stale_replay", message=str(exc), action_kind=receipt.action_kind
        )
    error = output_columns_replay_error(
        project,
        receipt,
        output_kind="semantic_join_output_column",
        action_kind=receipt.action_kind,
        scope="receipt",
        allow_empty_rows=True,
    )
    if error is not None:
        return error
    for output in receipt.outputs:
        ref = output.ref
        if ref.get("kind") == "semantic_join_link_sheet":
            row = project.db.execute(
                "SELECT name FROM sheets WHERE id=? AND hidden=0", (ref["sheet_id"],)
            ).fetchone()
            if row is None or row["name"] != ref["name"]:
                return ActionError(
                    code="stale_replay",
                    message="semantic join result sheet changed",
                    action_kind=receipt.action_kind,
                )
        elif ref.get("kind") == "semantic_join_link_edges":
            actual = project.visible_row_ids(
                ref["child_sheet_id"], ref["child_row_ids"]
            )
            if actual != ref["child_row_ids"]:
                return ActionError(
                    code="stale_replay",
                    message="semantic join linked rows changed",
                    action_kind=receipt.action_kind,
                )
    return None


def validate_semantic_join_child(project, state, origin_receipt):
    refs = [item.ref for item in origin_receipt.outputs]
    children = [ref for ref in refs if ref.get("kind") == "semantic_join_link_sheet"]
    if len(children) != 1:
        raise ValueError("semantic backfill origin must name its one linked child")
    child = children[0]
    current = project.db.execute(
        "SELECT * FROM sheets WHERE id=? AND hidden=0", (child["sheet_id"],)
    ).fetchone()
    if (
        current is None
        or current["name"] != state["child_name"]
        or current["parent_sheet_id"] != state["source"]["sheet_id"]
    ):
        raise ValueError(
            "semantic backfill child no longer matches its originating receipt"
        )
    original = {
        ref["column_id"]
        for ref in refs
        if ref.get("kind") == "semantic_join_output_column"
    }
    actual = {
        col["id"]
        for col in project.columns(state["source"]["sheet_id"], include_hidden=True)
        if col["name"]
        in {
            ref["name"]
            for ref in refs
            if ref.get("kind") == "semantic_join_output_column"
        }
    }
    if original != actual:
        raise ValueError("semantic backfill source outputs differ from its origin")
    if {col["name"] for col in project.columns(child["sheet_id"])} != set(
        state["child_names"].values()
    ):
        raise ValueError("semantic backfill child columns changed")
    return child["sheet_id"]


def _materialize(
    project, state, plan, run, *, existing_child_id=None, copied_values=None
):
    """Caller owns both the SQLite transaction and the exact writer fence."""
    sheet_id = state["source"]["sheet_id"]
    target_id = state["target"]["sheet_id"]
    names = state["child_names"]
    output_ids = {
        binding.output_role: binding.column_id
        for binding in ResultGenerationStore(project).bindings_for_run(run["id"])
    }
    values = {
        key: project.get_values(
            sheet_id, output_ids[name], row_ids=state["source"]["row_ids"]
        )
        for key, name in plan.output_names.items()
    }
    copied = {
        "source": state["source_column"],
        **{f"carry.{col['name']}": col for col in state["carry_columns"]},
    }
    for key, col in copied.items():
        values[key] = project.get_values(
            sheet_id, col["id"], row_ids=state["source"]["row_ids"]
        )
        if existing_child_id is not None:
            previous_columns = {
                column["name"]: column["id"]
                for column in project.columns(existing_child_id)
            }
            previous_values = project.get_values(
                existing_child_id, previous_columns[names[key]]
            )
            for row in project.db.execute(
                "SELECT id,parent_row_id FROM rows WHERE sheet_id=? AND hidden=0",
                (existing_child_id,),
            ):
                values[key][row["parent_row_id"]] = previous_values.get(row["id"])
        if copied_values is not None:
            values[key].update(copied_values[key])
    op_id = project.append_op(
        "derive",
        {
            "from_sheet": sheet_id,
            "target_sheet": target_id,
            "child": state["child_name"],
            "action_kind": plan.action.action_id,
            "reads": [
                {
                    "kind": "semantic_join_link_source",
                    "source_sheet_id": sheet_id,
                    "target_sheet_id": target_id,
                }
            ],
        },
        label=f"semantic join {state['source']['name']} → {state['target']['name']}",
        commit=False,
    )
    producer_id = create_base_cell_producer(
        project.db, stage_id=f"op:{op_id}", op_id=op_id
    )
    child_id = existing_child_id
    old_rows = []
    if child_id is None:
        child_id = project.add_sheet(
            state["child_name"],
            parent_sheet_id=sheet_id,
            parent_op_id=op_id,
            commit=False,
        )
    else:
        old_rows = project.visible_row_ids(child_id)
        project.db.execute(
            "UPDATE rows SET hidden=1 WHERE sheet_id=? AND hidden=0", (child_id,)
        )
    types = {key: col["type"] for key, col in copied.items()}
    types.update(match_value="text", match_score="number", matched_row_id="integer")
    columns = (
        {col["name"]: col["id"] for col in project.columns(child_id)}
        if existing_child_id is not None
        else {
            names[key]: project.add_column(child_id, names[key], type=typ, commit=False)
            for key, typ in types.items()
        }
    )
    parents = [
        rid
        for rid in state["source"]["row_ids"]
        if values["matched_row_id"].get(rid) is not None
    ]
    records = [
        {names[key]: data.get(rid) for key, data in values.items()} for rid in parents
    ]
    child_rows = (
        project.add_rows(
            child_id,
            records,
            columns,
            parent_row_ids=parents,
            producer_id=producer_id,
            commit=False,
        )
        if records
        else []
    )
    project.set_undo_info(
        op_id,
        {
            "created_rows": child_rows,
            "deleted_rows": old_rows,
            **({"created_sheets": [child_id]} if existing_child_id is None else {}),
        },
        commit=False,
    )
    edges = [
        {
            "child_row_id": child,
            "source_row_id": parent,
            "parent_row_id": parent,
            "target_row_id": values["matched_row_id"][parent],
            "matched_row_id": values["matched_row_id"][parent],
            "score": values["match_score"].get(parent),
            "value": values["match_value"].get(parent),
        }
        for child, parent in zip(child_rows, parents, strict=True)
    ]
    return (
        op_id,
        child_id,
        {
            "kind": "semantic_join_link_edges",
            "sheet_id": sheet_id,
            "target_sheet_id": target_id,
            "child_sheet_id": child_id,
            "op_id": op_id,
            "child_row_ids": child_rows,
            "source_row_ids": parents,
            "target_row_ids": [edge["target_row_id"] for edge in edges],
            "edges": edges,
        },
    )


def finalize_semantic_join(
    project, action, _params, *, bound, receipt_fn=None, origin_receipt=None, **kwargs
):
    state = dict(kwargs["resolved"].facts["semantic_join"])
    run_id = int(kwargs["run_id"])
    claim = kwargs.get("claim_token")
    writer = kwargs.get("writer_attempt_id")
    receipt_id = kwargs["receipt_id"]
    if writer is None or claim != _output_claim_token(receipt_id):
        raise StaleAttemptWriter(
            "semantic join finalization needs its exact writer claim"
        )

    def abort(code, message):
        from frisket.engine.executor.project_run_terminalization import (
            CurrentWriterTerminalAuthority,
            terminalize_project_run,
        )
        from frisket.engine.store.receipts import ReceiptStore

        outcome = terminalize_project_run(
            project,
            run_id=run_id,
            receipt_id=receipt_id,
            status="failed",
            authority=CurrentWriterTerminalAuthority(
                writer_attempt_id=writer, claim_token=claim
            ),
            errors=[ActionError(code=code, message=message, action_kind=action.kind)],
        )
        if outcome.disposition not in {"terminalized", "already_terminal"}:
            raise StaleAttemptWriter(
                f"semantic join publication abort was refused: {outcome.reason}"
            )
        return _result_from_receipt(ReceiptStore(project).parsed_by_id(receipt_id))

    from frisket.ops.base import persisted_recipe_invocation_halt

    run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if halt := persisted_recipe_invocation_halt(run["params"]):
        return abort(*halt)
    plan = replace(
        _typed_map_rows_plan(bound),
        source_column_ids=MappingProxyType(state["input_column_ids"]),
        source_column_types=MappingProxyType(state["input_column_types"]),
    )
    try:
        project.db.execute("BEGIN IMMEDIATE")
        column_ids = OutputColumnClaimStore(project).active_column_ids(
            claim_token=claim, run_id=run_id
        )
        OutputColumnClaimStore.require_current_writer(
            project.db,
            run_id=run_id,
            writer_attempt_id=writer,
            claim_token=claim,
            output_column_ids=column_ids,
        )
        validate_semantic_join_pins(project, state)
        if origin_receipt is None and any(
            sheet["name"] == state["child_name"] for sheet in project.sheets()
        ):
            raise ValueError(
                "semantic join result sheet name was taken after admission"
            )
        run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run["status"] not in {"completed", "failed", "cancelled"}:
            raise ValueError(
                "semantic join rows have not reached a terminal checkpoint"
            )
        existing_child_id = (
            validate_semantic_join_child(project, state, origin_receipt)
            if origin_receipt is not None
            else None
        )
        if origin_receipt is not None:
            prior_output_ids = {
                item.ref["column_id"]
                for item in origin_receipt.outputs
                if item.ref.get("kind") == "semantic_join_output_column"
            }
            if prior_output_ids != set(column_ids):
                raise ValueError(
                    "semantic backfill must own its originating output columns"
                )
        copied_values = {
            key: project.get_values(
                state["source"]["sheet_id"],
                col["id"],
                row_ids=state["source"]["row_ids"],
            )
            for key, col in {
                "source": state["source_column"],
                **{f"carry.{col['name']}": col for col in state["carry_columns"]},
            }.items()
        }
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
        created = json.loads(op["undo_info"] or "{}").get("created_columns", [])
        for column_id in created:
            if column_id in column_ids and int(run["completed_rows"] or 0) > 0:
                project.db.execute(
                    "UPDATE columns SET hidden=0 WHERE id=?", (column_id,)
                )
        child_state = state
        if origin_receipt is not None:
            prior_rows = next(
                item.ref["source_row_ids"]
                for item in origin_receipt.outputs
                if item.ref.get("kind") == "semantic_join_link_edges"
            )
            child_state = {
                **state,
                "source": {
                    **state["source"],
                    "row_ids": sorted(
                        set(prior_rows) | set(state["source"]["row_ids"])
                    ),
                },
            }
        if existing_child_id is not None and not state["source"]["row_ids"]:
            edges = next(
                dict(item.ref)
                for item in origin_receipt.outputs
                if item.ref.get("kind") == "semantic_join_link_edges"
            )
            child, link_op = existing_child_id, edges["op_id"]
        else:
            link_op, child, edges = _materialize(
                project,
                child_state,
                plan,
                run,
                existing_child_id=existing_child_id,
                copied_values=copied_values,
            )
        semantic_receipt = _typed_receipt(
            project,
            run_id,
            kwargs["action_id"],
            receipt_id,
            project_id=kwargs["project_id"],
            plan=plan,
            params_hash=kwargs["params_hash"],
        )
        receipt = (
            receipt_fn(project, run_id, kwargs["action_id"], receipt_id)
            if receipt_fn is not None
            else semantic_receipt
        )
        roles = {
            name: ("target_row_id" if key == "matched_row_id" else key)
            for key, name in plan.output_names.items()
        }
        outputs = [
            item.model_copy(
                update={
                    "ref": {
                        **item.ref,
                        "kind": "semantic_join_output_column",
                        "role": roles[item.name],
                        "may_feed": ["review.decision", "derive.link_table"],
                    }
                }
            )
            for item in semantic_receipt.outputs
            if item.name in roles
        ]
        if receipt_fn is not None:
            outputs = [*receipt.outputs, *outputs]
        outputs += [
            ReceiptIO(
                name=state["child_name"],
                ref={
                    "kind": "semantic_join_link_sheet",
                    "name": state["child_name"],
                    "sheet_id": child,
                    "parent_sheet_id": state["source"]["sheet_id"],
                    "op_id": link_op,
                    "row_ids": edges["child_row_ids"],
                },
            ),
            ReceiptIO(name="link_edges", ref=edges),
        ]
        inputs = list(receipt.inputs)
        inputs.extend(
            ReceiptIO(
                name=f"carry_column.{column['name']}",
                ref={
                    "kind": "semantic_join_carry_column",
                    "role": "carry",
                    "sheet_id": state["source"]["sheet_id"],
                    "column_id": column["id"],
                    "name": column["name"],
                    "type": column["type"],
                    "row_ids": edges["source_row_ids"],
                },
            )
            for column in state["carry_columns"]
        )
        for role in ("source", "target"):
            facts = state[role]
            column = state[f"{role}_column"]
            inputs += [
                ReceiptIO(
                    name=f"{role}_sheet",
                    ref={
                        "kind": f"semantic_join_{role}_sheet",
                        "sheet_id": facts["sheet_id"],
                        "name": facts["name"],
                        "row_ids": facts["row_ids"],
                        **(
                            {
                                "run_id": run_id,
                                "op_id": run["op_id"],
                                "request_hash": kwargs["params_hash"],
                            }
                            if role == "source"
                            else {}
                        ),
                    },
                ),
                ReceiptIO(
                    name=f"{role}_column.{column['name']}",
                    ref={
                        "kind": f"semantic_join_{role}_column",
                        "sheet_id": facts["sheet_id"],
                        "column_id": column["id"],
                        "name": column["name"],
                        "type": column["type"],
                        "row_ids": facts["row_ids"],
                    },
                ),
            ]
        from frisket.engine.store.receipts import ReceiptStore

        match_evidence = [
            item
            for item in ReceiptStore(project).parsed_by_id(receipt_id).evidence
            if item.ref.get("kind")
            in {"semantic_join_embedding_backend", "semantic_join_thresholds"}
        ]
        receipt = receipt.model_copy(
            update={
                "inputs": inputs,
                "outputs": outputs,
                "op_ids": [run["op_id"], link_op],
                "evidence": [
                    *receipt.evidence,
                    *match_evidence,
                    ReceiptEvidence(ref=edges, retention="pinned"),
                ],
            }
        )
        result = _finalize_reserved_action_receipt(
            project,
            action,
            params_hash=kwargs["params_hash"],
            project_id=kwargs["project_id"],
            receipt=receipt,
            reservation_lost_message="semantic join reservation was lost",
            update_failed_message="semantic join receipt update failed",
            require_running_status=False,
            replay_error_fn=lambda prior: _replay_error(project, prior),
            commit=False,
        )
        if result is not None:
            project.db.rollback()
            return result
        set_attempt_state(project, writer, "effected", commit=False)
        if not OutputColumnClaimStore(project).release(
            claim_token=claim,
            status="failed" if receipt.status == "failed" else "released",
            commit=False,
        ):
            raise StaleAttemptWriter("semantic join lost its claim before publication")
        project.db.commit()
        return _result_from_receipt(receipt)
    except SemanticJoinSourceChanged as exc:
        project.db.rollback()
        return abort("stale_input", str(exc))
    except BaseException:
        project.db.rollback()
        raise


def typed_semantic_join_queue_spec(bound, *, initial_plan=None, semantic_state=None):
    from frisket.engine.runner import ProviderKeyRefusal
    from frisket.engine.executor.action_inventory import (
        _QueuedActionSpec,
        _queued_payload_codecs,
    )
    from functools import partial

    unresolved = initial_plan or _typed_map_rows_plan(bound)
    runner_spec = unresolved.spec_dict()
    runner_spec["idempotency_key"] = bound.request.idempotency_key
    if semantic_state is not None:
        runner_spec.update(
            semantic_join=semantic_state, engine=semantic_state["embedding_model"]
        )
    envelope = _TypedExecutionEnvelope(
        kind=bound.action.action_id,
        idempotency_key=bound.request.idempotency_key,
        params=MappingProxyType(dict(bound.request.params)),
    )

    def resolve(project, _params, spec, router):
        try:
            return _resolve(project, bound, spec, router)
        except ProviderKeyRefusal as exc:
            return ActionError(
                code=exc.error_code,
                message=exc.action_message(),
                action_kind=bound.action.action_id,
                field=exc.field,
                details=dict(exc.details),
            )
        except (TypeError, ValueError) as exc:
            return _typed_plan_error(bound.action.action_id, exc)

    def guard(project, payload, _params):
        try:
            stored = project.db.execute(
                "SELECT ops.spec FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.id=?",
                (payload["run_id"],),
            ).fetchone()
            state = json.loads(stored["spec"])["semantic_join"]
            if (
                state != payload["v1_semantic_join"]
                or state != payload["spec"]["semantic_join"]
            ):
                raise ValueError(
                    "queued semantic matching admission differs from its durable run"
                )
            validate_semantic_join_pins(project, state)
            bindings = ResultGenerationStore(project).bindings_for_run(
                payload["run_id"]
            )
            columns = {
                col["name"]: col["id"]
                for col in project.columns(
                    bound.request.scope.sheet_id, include_hidden=True
                )
            }
            if len(bindings) != len(bound.output_fields) or any(
                columns.get(binding.output_role) != binding.column_id
                for binding in bindings
            ):
                raise ValueError(
                    "queued semantic match outputs changed after admission"
                )
        except (KeyError, TypeError, ValueError) as exc:
            return ActionError(
                code="stale_input", message=str(exc), action_kind=bound.action.action_id
            )
        return None

    spec = _QueuedActionSpec(
        kind=bound.action.action_id,
        params_model=type(bound.params),
        completed_spec=None,
        runner_spec_fn=lambda _params: dict(runner_spec),
        resolve_fn=resolve,
        precheck_fn=lambda *args, **kwargs: None,
        reservation_kind="typed_semantic_join_reservation",
        queue_job_kind="typed_semantic_join_queue_job",
        payload_codecs=_queued_payload_codecs(
            "semantic_join",
            "input_column_ids",
            "input_column_types",
            "output_names",
            "output_target_preconditions",
        ),
        finalize_action=partial(finalize_semantic_join, bound=bound),
        pre_run_guard=guard,
        params_hash_fn=lambda _action: typed_request_hash(bound),
        confirmed_fn=lambda _params: bound.request.confirmation is not None,
        cost_gate_error_fn=_typed_model_cost_error,
        map_error_code="join_run_failed",
        output_claim_error_field="output_names",
        resolve_needs_router=True,
    )
    return envelope, spec, unresolved.program


def _semantic_resume_run_id(project, bound, state):
    """Keep paid row checkpoints reachable after a stale receipt is cleared."""
    from frisket.engine.executor.map_rows_action import (
        bound_typed_program_request_from_runner_spec,
    )

    matches = []
    for run in project.db.execute(
        "SELECT runs.id, ops.spec FROM runs JOIN ops ON ops.id=runs.op_id "
        "WHERE runs.action_kind=? AND runs.status='running'",
        (bound.action.action_id,),
    ).fetchall():
        stored = json.loads(run["spec"])
        if stored.get("idempotency_key") != bound.request.idempotency_key:
            continue
        original = bound_typed_program_request_from_runner_spec(stored)
        if (
            original is None
            or typed_request_hash(original) != typed_request_hash(bound)
            or stored.get("semantic_join") != state
        ):
            raise ValueError(
                "abandoned semantic join differs from this request or its inputs"
            )
        matches.append(int(run["id"]))
    if len(matches) > 1:
        raise ValueError("semantic join has ambiguous abandoned runs")
    return matches[0] if matches else None


def run_typed_semantic_join_action(
    project, project_id, bound, router, map_runner_factory
):
    from functools import partial
    from frisket.engine.executor.action_inventory import (
        _ReservedMaprunnerActionSpec,
        ExecutorContext,
        ExecutorDeps,
    )
    from frisket.engine.executor.action_lifecycle import (
        _run_reserved_maprunner_action_spec,
    )

    envelope, queued, _ = typed_semantic_join_queue_spec(bound)
    # A rolled-back child transaction retains its completed row checkpoints
    # and exact writer. Retry only publication, never the paid scorer.
    existing = _receipt_for_idempotency(project, bound.request.idempotency_key)
    if existing is not None and existing["status"] == "running":
        from frisket.contracts.action import Receipt
        from frisket.engine.executor.action_specs import ResolvedAction

        receipt = Receipt.model_validate(json.loads(existing["body"]))
        if (
            receipt.params_hash == typed_request_hash(bound)
            and receipt.run_id is not None
        ):
            run = project.db.execute(
                "SELECT runs.*,ops.spec AS runner_spec FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.id=?",
                (receipt.run_id,),
            ).fetchone()
            if (
                run is not None
                and run["status"] in {"completed", "failed", "cancelled"}
                and run["current_attempt_id"] is not None
                and any(
                    binding.state != "sealed"
                    for binding in ResultGenerationStore(project).bindings_for_run(
                        receipt.run_id
                    )
                )
            ):
                return finalize_semantic_join(
                    project,
                    envelope,
                    bound.params,
                    bound=bound,
                    run_id=receipt.run_id,
                    action_id=receipt.action_id,
                    receipt_id=receipt.receipt_id,
                    params_hash=receipt.params_hash,
                    project_id=project_id,
                    writer_attempt_id=run["current_attempt_id"],
                    claim_token=_output_claim_token(receipt.receipt_id),
                    resolved=ResolvedAction.from_resolve_dict(
                        {
                            "semantic_join": json.loads(run["runner_spec"])[
                                "semantic_join"
                            ]
                        }
                    ),
                )

    def factory(project, router):
        runner = map_runner_factory(project, router)
        runner.allow_action_lifecycle_only_recipes = True
        return runner

    spec = _ReservedMaprunnerActionSpec(
        kind=bound.action.action_id,
        params_model=type(bound.params),
        runner_spec_fn=queued.runner_spec_fn,
        resolve_fn=lambda project, params, spec: queued.resolve_fn(
            project, params, spec, router
        ),
        precheck_fn=queued.precheck_fn,
        write_fn=partial(finalize_semantic_join, bound=bound),
        reservation_kind=queued.reservation_kind,
        program_fn=lambda project, spec: _typed_map_rows_plan(bound).program,
        replay_error_fn=lambda project, params, receipt, action: _replay_error(
            project, receipt
        ),
        params_hash_fn=queued.params_hash_fn,
        confirmed_fn=queued.confirmed_fn,
        cost_gate_error_fn=queued.cost_gate_error_fn,
        resume_run_id_fn=lambda _project, _params, resolved: resolved["resume_run_id"],
        map_error_code="join_run_failed",
    )
    return _run_reserved_maprunner_action_spec(
        project,
        envelope,
        bound.params,
        spec=spec,
        ctx=ExecutorContext(
            project_id=project_id,
            deps=ExecutorDeps(router=router, map_runner_factory=factory),
        ),
        map_runner_factory=factory,
    )
