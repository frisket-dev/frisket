"""Typed refresh authoring, admission, and atomicity beyond the builtin handler."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry, action
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.sheets import RefreshParams
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    RefreshedSheet,
    SheetRefresher,
)
from frisket.engine.executor import joined_tables_read
from frisket.engine.executor.sheet_refresh_action import run_typed_sheet_refresh_action
from frisket.engine.store.receipts import ReceiptStore

from test_sheet_refresh import _build
from test_multi_parent_sheet_refresh import _seed_join, _run


class _DerivedParams(ActionParams):
    reference: str
    offset: int = 0


def _derived_refresh(params: _DerivedParams, sheets: SheetRefresher) -> RefreshedSheet:
    result = sheets.refresh(
        int(params.reference.removeprefix("sheet:")) + params.offset
    )
    result.sheet_id = 999999
    result.refreshed_row_count = 999999
    return result


def _bound(handler, params, *, key="custom-refresh", confirmation=None):
    declaration = action(
        name="refresh",
        title="Refresh",
        description="Refresh a derived target.",
        category=ActionCategory.CONVERT,
        run=handler,
    )
    registered = ActionRegistry(
        (ActionNamespace("custom", actions=(declaration,)),)
    ).get("custom.refresh")
    return BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id="custom.refresh",
            scope={"kind": "project"},
            params=params,
            idempotency_key=key,
            confirmation=confirmation,
        ),
    )


def _snapshot(project):
    # All rows, memberships, grounding, descriptors, watermarks, ops and receipts.
    return tuple(project.db.iterdump())


def test_refresh_contract_is_semantic_and_conditionally_confirmable():
    registered = ACTION_REGISTRY.get("sheet.refresh")
    assert registered.definition.run.capabilities == (SheetRefresher,)
    assert set(registered.definition.run.params_model.model_fields) == {"sheet_id"}
    assert registered.catalog_entry()["cost_policy"] == {
        "kind": "none",
        "requires_confirmation": True,
    }
    for bad in (True, "1", 0, -1):
        with pytest.raises(ValidationError):
            RefreshParams(sheet_id=bad)
    with pytest.raises(ValidationError):
        RefreshParams(sheet_id=1, confirmed=True)


def test_derived_argument_and_mutated_return_cannot_forge_refresh_receipt(tmp_path):
    project, _, _, child_id, _ = _build(tmp_path)
    try:
        bound = _bound(
            _derived_refresh,
            {
                "reference": f"sheet:{child_id - 1}",
                "offset": 1,
            },
        )
        result = run_typed_sheet_refresh_action(project, "p-ref", bound)
        assert result.status == "completed", result.errors
        assert result.action.kind == "custom.refresh"
        assert result.outputs[0].sheet_id == child_id
        receipt = json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()[0]
        )
        assert receipt["inputs"][0]["ref"]["sheet_id"] == child_id
        assert receipt["evidence"][0]["ref"]["refreshed_row_count"] == 3
        snapshot = _snapshot(project)
        replay = run_typed_sheet_refresh_action(project, "p-ref", bound)
        assert replay.receipt_id == result.receipt_id
        assert _snapshot(project) == snapshot
    finally:
        project.close()


@pytest.mark.parametrize("failure", ["runtime_error", "wrong_return", "second_call"])
@pytest.mark.parametrize("family", ["list", "join"])
def test_post_call_failure_rolls_back_the_complete_refresh(
    tmp_path, failure, family, caplog
):
    caplog.set_level("DEBUG", logger="frisket.executor")
    if family == "list":
        project, _, _, child_id, _ = _build(tmp_path)
    else:
        seeded = _seed_join(tmp_path / "join.frisket")
        project, child_id = seeded["project"], seeded["child_id"]
    try:

        def handler(params: _DerivedParams, sheets: SheetRefresher) -> RefreshedSheet:
            refreshed = sheets.refresh(int(params.reference))
            if failure == "runtime_error":
                raise RuntimeError("private post-handler detail")
            if failure == "second_call":
                return sheets.refresh(int(params.reference))
            assert refreshed.sheet_id == child_id
            return None

        bound = _bound(handler, {"reference": str(child_id)})
        before = _snapshot(project)
        for _ in range(2):
            failed = run_typed_sheet_refresh_action(project, "p-ref", bound)
            assert failed.status == "failed"
            assert failed.errors[0].code == "project_write_failed"
            assert "private" not in failed.errors[0].message
            assert "private post-handler detail" not in caplog.text
            assert _snapshot(project) == before
    finally:
        project.close()


@pytest.mark.parametrize(
    "interruption", [KeyboardInterrupt, SystemExit, asyncio.CancelledError]
)
def test_post_refresh_cancellation_rolls_back_and_closes_transaction(
    tmp_path, interruption
):
    project, _, _, child_id, _ = _build(tmp_path)
    try:

        def handler(params: _DerivedParams, sheets: SheetRefresher) -> RefreshedSheet:
            sheets.refresh(int(params.reference))
            raise interruption("interrupted after refresh")

        bound = _bound(handler, {"reference": str(child_id)})
        before = _snapshot(project)
        cursor_before = project.op_cursor
        with pytest.raises(interruption, match="interrupted after refresh"):
            run_typed_sheet_refresh_action(project, "p-ref", bound)
        observed = {
            "transaction_open": project.db.in_transaction,
            "database_unchanged": _snapshot(project) == before,
            "cursor_before": cursor_before,
            "cursor_after": project.op_cursor,
        }
        assert not project.db.in_transaction, observed
        assert _snapshot(project) == before
        # A caller's later commit must not publish the interrupted refresh.
        project.db.commit()
        assert _snapshot(project) == before
    finally:
        project.close()


def test_join_receipt_failure_restores_all_rows_memberships_and_watermarks(
    tmp_path, monkeypatch
):
    seeded = _seed_join(tmp_path / "join.frisket")
    project, child_id = seeded["project"], seeded["child_id"]
    try:
        before = _snapshot(project)

        def fail_receipt(*args, **kwargs):
            raise RuntimeError("receipt unavailable")

        monkeypatch.setattr(ReceiptStore, "insert_completed", fail_receipt)
        failed = run_typed_sheet_refresh_action(
            project,
            "p-ref",
            _bound(_derived_refresh, {"reference": f"sheet:{child_id}"}),
        )
        assert failed.status == "failed"
        assert _snapshot(project) == before
    finally:
        project.close()


def test_join_confirmation_is_current_refresh_target_and_input_bound(
    tmp_path, monkeypatch
):
    seeded = _seed_join(tmp_path / "join.frisket")
    project, child_id = seeded["project"], seeded["child_id"]
    try:
        parent_op_id = project.db.execute(
            "SELECT parent_op_id FROM sheets WHERE id=?", (child_id,)
        ).fetchone()[0]
        saved = json.loads(
            project.db.execute(
                "SELECT spec FROM ops WHERE id=?", (parent_op_id,)
            ).fetchone()[0]
        )
        second = {
            "action_id": "derive.join",
            "scope": saved["scope"],
            "sheet_name": "Second",
            "idempotency_key": "second-join",
            "params": saved["params"],
        }
        created = _run(project, second)
        assert created.status == "completed", created.errors
        second_id = created.outputs[0].sheet_id
        project.db.execute(
            "UPDATE sheets SET parent_op_id=? WHERE id=?", (parent_op_id, second_id)
        )

        saved["params"]["max_output_rows"] = 1
        original_quote = _run(
            project,
            {
                **second,
                "sheet_name": "Original quote",
                "idempotency_key": "original-quote",
            },
        )
        assert original_quote.status == "needs_confirmation", original_quote.errors
        original_token = original_quote.errors[0].details["promise_set_hash"]
        saved["confirmation"] = original_token
        project.db.execute(
            "UPDATE ops SET spec=? WHERE id=?", (json.dumps(saved), parent_op_id)
        )
        project.db.commit()
        selected = child_id

        def handler(params: _DerivedParams, sheets: SheetRefresher) -> RefreshedSheet:
            # Same canonical request, different ACTUAL operation argument.
            return sheets.refresh(selected)

        def refresh(token=None):
            return run_typed_sheet_refresh_action(
                project,
                "p-ref",
                _bound(
                    handler,
                    {"reference": "current-selection"},
                    confirmation=token,
                ),
            )

        before = _snapshot(project)
        gated = refresh()
        assert gated.status == "needs_confirmation"
        token = gated.errors[0].details["promise_set_hash"]
        assert token != original_token
        assert refresh(original_token).status == "needs_confirmation"
        assert _snapshot(project) == before

        selected = second_id
        other = refresh(token)
        assert other.status == "needs_confirmation"
        assert other.errors[0].details["promise_set_hash"] != token
        assert _snapshot(project) == before
        selected = child_id

        # An unmatched inner-join input changes admission even though projected
        # output count and top fan-out keys remain unchanged.
        project.add_rows(
            seeded["left_id"],
            [{"state_fips": 99, "state_name": "Unmatched"}],
            seeded["left_cols"],
        )
        project.db.commit()
        after_edit = _snapshot(project)
        build_records = joined_tables_read.iter_join_records

        def forbid_expansion(*args, **kwargs):
            pytest.fail("fan-out rows expanded before refresh confirmation")

        monkeypatch.setattr(joined_tables_read, "iter_join_records", forbid_expansion)
        stale = refresh(token)
        assert stale.status == "needs_confirmation"
        assert (
            stale.errors[0].details["estimated_rows"]
            == gated.errors[0].details["estimated_rows"]
        )
        assert stale.errors[0].details["promise_set_hash"] != token
        assert _snapshot(project) == after_edit
        monkeypatch.setattr(joined_tables_read, "iter_join_records", build_records)
        confirmed = refresh(stale.errors[0].details["promise_set_hash"])
        assert confirmed.status == "completed", confirmed.errors
        assert confirmed.outputs[0].sheet_id == child_id
    finally:
        project.close()
