"""Refresh a grouped summary through its original model owner and keyed rows."""

from dataclasses import replace
import json

from frisket.contracts.action import ActionError
from frisket.engine.store.changes import ChangeStore
from frisket.engine.store.current_value_windows import read_current_column
from frisket.engine.store.derived_updates import (
    DerivedRefreshRow,
    reconcile_into_open_change,
    refresh_config,
)
from frisket.engine.store.keyed_updates import observation, reconcile_fields
from frisket.engine.store.materialization import (
    AggregateSheetWrite,
    materialized_row_sources_ref,
)


def validate_target(project, params):
    target = params.refresh_target
    sheet = project.db.execute(
        "SELECT * FROM sheets WHERE id=? AND hidden=0", (target.sheet_id,)
    ).fetchone()
    if sheet is None or sheet["parent_op_id"] != target.parent_op_id:
        return ActionError(
            code="invalid_input_ref",
            message="The grouped sheet is unavailable or its derivation changed",
            action_kind=params.action_kind,
        )
    from datetime import datetime, timezone
    from frisket.engine.store.leases import lease_held

    if lease_held(
        sheet["refresh_claim_token"],
        sheet["refresh_lease_expires_at"],
        datetime.now(timezone.utc),
    ):
        return ActionError(
            code="sheet_refresh_busy",
            message="Another refresh is already in progress",
            action_kind=params.action_kind,
        )
    parent = project.db.execute(
        "SELECT * FROM ops WHERE id=?", (target.parent_op_id,)
    ).fetchone()
    config = refresh_config(parent)
    names = {
        params.group_column_name,
        params.row_count_column_name,
        params.summary_column_name,
    }
    if (
        config is None
        or config.get("kind") != "group"
        or set(config["columns"]) != names
    ):
        return ActionError(
            code="invalid_input_ref",
            message="The grouped sheet no longer has its declared output fields",
            action_kind=params.action_kind,
        )
    for column_id in config["columns"].values():
        if (
            project.db.execute(
                "SELECT 1 FROM columns WHERE id=? AND sheet_id=? AND active=1",
                (column_id, target.sheet_id),
            ).fetchone()
            is None
        ):
            return ActionError(
                code="invalid_input_ref",
                message="Restore the grouped output columns before refreshing",
                action_kind=params.action_kind,
            )
    return None


def prepare_refresh(project, sheet, parent_op, request, outer_bound):
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.engine.executor.group_summary_action import (
        prepare_group_summary_action,
    )
    from frisket.engine.executor.group_summary_plan import GroupRefreshTarget

    target = GroupRefreshTarget(
        sheet_id=int(sheet["id"]),
        parent_op_id=int(parent_op["id"]),
        policy=getattr(outer_bound.params, "update_policy", "keep_changes"),
    )
    request = request.model_copy(
        update={
            "params": {
                key: value
                for key, value in request.params.items()
                if key != "params_hash"
            },
            "sheet_name": str(sheet["name"]),
        }
    )
    saved = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(request.action_id), request
    )
    prepared = prepare_group_summary_action(project, saved, refresh_target=target)
    operation = prepared.operation.model_copy(
        update={
            "action_kind": outer_bound.action.action_id,
            "request": outer_bound.request.model_dump(
                mode="json", exclude={"confirmation"}
            ),
        }
    )
    return replace(prepared, operation=operation, creates_sheet=False)


def publish_group_refresh(
    project, params, *, op_id, op_spec, resolved, computations, columns
):
    """Publish metadata, keeping actual model results in the normal result store.

    Returns the ordinary materialization facts and per-row publication decisions.
    Baselines for summaries advance later, after their successful result is saved.
    """
    target = params.refresh_target
    refusal = validate_target(project, params)
    if refusal is not None:
        raise ValueError(refusal.message)
    parent = project.db.execute(
        "SELECT * FROM ops WHERE id=?", (target.parent_op_id,)
    ).fetchone()
    config = refresh_config(parent)
    column_ids = {name: int(cid) for name, cid in config["columns"].items()}
    summary_id = column_ids[params.summary_column_name]
    ChangeStore(project).start(op_id)
    op_spec = {
        **op_spec,
        "sheet_id": target.sheet_id,
        "policy": target.policy,
        "derived_refresh": config,
    }
    project.db.execute("UPDATE ops SET spec=? WHERE id=?", (json.dumps(op_spec), op_id))
    # Summary fields are deliberately absent: they belong to immutable results,
    # never a second source-cell copy. Metadata still shares the same Change.
    rows = [
        DerivedRefreshRow(
            key=str(item["group"]["identity_key"]),
            values={
                params.group_column_name: item["group"]["name"],
                params.row_count_column_name: len(item["group"]["source_row_ids"]),
            },
            sources=tuple(
                (int(r), "aggregate_source") for r in item["group"]["source_row_ids"]
            ),
        )
        for item in computations
    ]
    row_ids = reconcile_into_open_change(
        project.db,
        op_id=op_id,
        sheet_id=target.sheet_id,
        config=config,
        columns=columns,
        rows=rows,
        policy=target.policy,
    )
    current = read_current_column(
        project.db,
        sheet_id=target.sheet_id,
        column_id=summary_id,
        row_ids=row_ids,
        bounded=False,
        preserve_invalid=True,
        include_hidden=True,
    )
    publish = {}
    field = str(summary_id)
    for row_id, item in zip(row_ids, computations, strict=True):
        baseline = observation(project.db, config["relationship_id"], row_id)
        cell = current.get(row_id)
        values = {} if cell is None or not cell.value_present else {field: cell.value}
        hidden = bool(
            project.db.execute(
                "SELECT hidden FROM rows WHERE id=?", (row_id,)
            ).fetchone()[0]
        )
        # Failures must not destroy a manual correction. With an unchanged
        # working value, they publish the normal explicit unsuccessful outcome.
        proposal = {field: item.get("summary")}
        changes, _ = reconcile_fields(
            values, proposal, baseline, target.policy, deleted=hidden
        )
        publish[row_id] = field in changes or (
            not hidden and field not in (baseline or {})
        )
    memberships = [
        dict(row)
        for row in project.db.execute(
            "SELECT materialized_row_id,source_row_id,source_sheet_id,role FROM materialized_row_sources WHERE op_id=?",
            (op_id,),
        )
    ]
    info = json.loads(
        project.db.execute("SELECT undo_info FROM ops WHERE id=?", (op_id,)).fetchone()[
            0
        ]
        or "{}"
    )
    write = AggregateSheetWrite(
        op_id=op_id,
        sheet_id=target.sheet_id,
        column_ids=column_ids,
        row_ids=row_ids,
        source_row_ids_by_row=[
            list(item["group"]["source_row_ids"]) for item in computations
        ],
        membership_rows=memberships,
        undo_info=info,
        materialized_sheet_ref={
            "kind": "materialized_sheet",
            "sheet_id": target.sheet_id,
            "op_id": op_id,
        },
        materialized_column_refs={},
        materialized_rows_ref={},
        materialized_row_sources_ref=materialized_row_sources_ref(
            op_id=op_id, rows=memberships, include_rows=False
        ),
    )
    return write, publish
