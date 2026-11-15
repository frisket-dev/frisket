"""Refresh a grouped summary through its original model owner and keyed rows."""

from dataclasses import replace

from frisket.contracts.action import ActionError
from frisket.engine.store.derived_updates import (
    refresh_config,
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

    existing = project.db.execute(
        "SELECT id FROM receipts WHERE idempotency_key=?",
        (params.request["idempotency_key"],),
    ).fetchone()
    own_token = existing["id"] if existing is not None else None
    if sheet["refresh_claim_token"] != own_token and lease_held(
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


def prepare_refresh(project, sheet, parent_op, request, outer_bound, *, edition=None):
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
    from frisket.execution.action_admission import action_admission_error
    from frisket.engine.executor.group_summary_action import GroupSummaryRefused

    refusal = action_admission_error(project, saved.action, edition=edition)
    if refusal is not None:
        raise GroupSummaryRefused(refusal)
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
