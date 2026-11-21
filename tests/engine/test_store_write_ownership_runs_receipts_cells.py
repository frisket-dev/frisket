"""Transaction ownership for core run, result, receipt, generation, cell,
descriptor, checkpoint, op-log, and preparation writes.

Each case checks committed state from a second SQLite connection:
- joining writes refuse without a caller transaction and roll back with one;
- owning writes refuse inside a caller's transaction and keep the caller's
  work uncommitted;
- ``ResultGenerationStore`` never commits a caller's transaction (regression
  for the nested-commit bug in ``_write_scope``);
- returned provider accounting survives ``ChangePublicationClosed`` on both
  owning and joining result publication without prematurely committing a
  caller-owned transaction; and
- multi-statement owning writes roll back atomically on mid-operation failure.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from frisket.contracts.action import Receipt
from frisket.engine.runner import preparation
from frisket.engine.runner.validation import _ValidatedSpec
from frisket.engine.store import op_log
from frisket.engine.store.changes import ChangePublicationClosed, ChangeStore
from frisket.engine.store.column_descriptors import set_descriptor
from frisket.engine.store.effect_checkpoints import (
    EffectCheckpointRef,
    EffectCheckpointStore,
    row_effect_ref,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.result_generations import (
    GenerationStateError,
    ResultGenerationStore,
)
from frisket.engine.store.runs import (
    ROW_EFFECT_CHECKPOINT_FAMILY,
    RunResultStore,
)
from frisket.engine.store.transactions import write_transaction
from frisket.ops.base import ActionProgram
from tests.helpers import run_writer_authority_fixture

CALLER_SHEET_NAME = "Caller edit"


class _MinimalProgram(ActionProgram):
    name = "test.ownership_program"
    version = "1"
    llm = False
    consumes_resolution = False

    def output_fields(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"name": "out", "column_type": "text"}]

    def source_columns(self, spec: dict[str, Any]) -> list[str]:
        return ["value"]


@pytest.fixture
def project(make_project):
    project = make_project("core-store-ownership")
    sheet_id = project.add_sheet("Source")
    column_id = project.add_column(sheet_id, "value")
    project.add_rows(sheet_id, [{"value": "original"}], {"value": column_id})
    try:
        yield project
    finally:
        if project.db.in_transaction:
            project.db.rollback()
        project.close()


def _sheet_id(project) -> int:
    return int(
        project.db.execute("SELECT id FROM sheets ORDER BY id LIMIT 1").fetchone()[0]
    )


def _column_id(project) -> int:
    return int(
        project.db.execute("SELECT id FROM columns ORDER BY id LIMIT 1").fetchone()[0]
    )


def _row_id(project) -> int:
    return int(
        project.db.execute("SELECT id FROM rows ORDER BY id LIMIT 1").fetchone()[0]
    )


def _committed(
    project, sql: str, params: tuple[Any, ...] = ()
) -> list[tuple[Any, ...]]:
    peer = sqlite3.connect(project.db_path)
    try:
        return peer.execute(sql, params).fetchall()
    finally:
        peer.close()


def _committed_count(project, table: str) -> int:
    return int(_committed(project, f"SELECT COUNT(*) FROM {table}")[0][0])


def _committed_sheet_name(project) -> str:
    return str(_committed(project, "SELECT name FROM sheets ORDER BY id LIMIT 1")[0][0])


def _open_caller_transaction(project) -> None:
    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute(
        "UPDATE sheets SET name=? WHERE id=?", (CALLER_SHEET_NAME, _sheet_id(project))
    )


def _assert_caller_work_kept_uncommitted(project) -> None:
    assert project.db.in_transaction
    assert _committed_sheet_name(project) == "Source"


def _assert_nothing_left_behind(project) -> None:
    assert not project.db.in_transaction
    project.db.commit()


def _refuse(project, statement: str, table: str) -> None:
    project.db.execute(
        f"CREATE TEMP TRIGGER refuse_{statement.lower()}_{table} "
        f"BEFORE {statement} ON main.{table} "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )


def _receipt(
    receipt_id: str = "rcpt_core_ownership", *, status: str = "running"
) -> Receipt:
    return Receipt(
        receipt_id=receipt_id,
        project_id="core-store-ownership",
        action_id="act_core_ownership",
        action_kind="map.template",
        status=status,
    )


def _seed_run(project) -> tuple[int, int]:
    op_id = project.append_change("map", {"action_kind": "map.template"})
    run_id = RunResultStore(project).start_run(
        op_id,
        _sheet_id(project),
        "map.template",
        row_ids=[_row_id(project)],
        total_rows=1,
    )
    return op_id, run_id


def _model_call(call_id: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "fact_version": "frisket.model-call-fact.v1",
        "capability": "llm.complete",
        "engine": "anthropic/test-model",
        "provider": "anthropic",
        "provider_kind": "chat_api",
        "credential_source": "platform_key",
        "provider_reported_cost_usd": 0.25,
        "provider_cost_usd": 0.25,
        "cost_source": "pricing_data",
        "units": {"tokens_in": 10, "tokens_out": 5},
    }


def _validated_spec(project) -> tuple[dict[str, Any], _ValidatedSpec]:
    sheet_id = _sheet_id(project)
    cols = project.columns(sheet_id)
    program = _MinimalProgram()
    spec: dict[str, Any] = {
        "action_kind": "test.ownership_program",
        "sheet_id": sheet_id,
    }
    validated = _ValidatedSpec(
        program=program,
        sheet_id=sheet_id,
        columns=cols,
        col_map={c["name"]: c["id"] for c in cols},
        row_ids=[_row_id(project)],
        output_fields=program.output_fields(spec),
        est=None,
        has_stored_run_scope=False,
        stored_run_scope_count=None,
        explicit_resume_scope=None,
    )
    return spec, validated


# -- joining writes refuse without a caller transaction ----------------------


JOINING_WRITES = {
    "runs.start_run_in_transaction": lambda p: RunResultStore(
        p
    ).start_run_in_transaction(
        p.append_change("map", {}), _sheet_id(p), "map.template"
    ),
    "runs.fail_result_value": lambda p: RunResultStore(p).fail_result_value(
        _seed_run(p)[1], _row_id(p), _column_id(p), "boom"
    ),
    "runs.write_results_in_transaction": lambda p: RunResultStore(
        p
    ).write_results_in_transaction(_seed_run(p)[1], []),
    "runs.publish_result_batch_in_transaction": lambda p: RunResultStore(
        p
    ).publish_result_batch_in_transaction(_seed_run(p)[1], []),
    "runs.write_returned_call_accounting_in_transaction": lambda p: RunResultStore(
        p
    ).write_returned_call_accounting_in_transaction(_seed_run(p)[1], []),
    "runs.write_unscoped_model_calls_in_transaction": lambda p: RunResultStore(
        p
    ).write_unscoped_model_calls_in_transaction([], row_id=None, column_id=None),
    "runs.consume_returned_row_effect_checkpoint": lambda p: RunResultStore(
        p
    ).consume_returned_row_effect_checkpoint(
        row_effect_ref(
            "cp-missing",
            run_id=_seed_run(p)[1],
            row_id=_row_id(p),
            action_kind="map.template",
            identity="id-1",
        ),
        batch=[],
    ),
    "runs.finish_run_in_transaction": lambda p: RunResultStore(
        p
    ).finish_run_in_transaction(_seed_run(p)[1], "completed"),
    "runs.request_cancel_in_transaction": lambda p: RunResultStore(
        p
    ).request_cancel_in_transaction(_seed_run(p)[1]),
    "runs.point_column_at_run": lambda p: RunResultStore(p).point_column_at_run(
        *_seed_run(p)[:1], _column_id(p), _seed_run(p)[1]
    ),
    "generations.declare": lambda p: ResultGenerationStore(p).declare(
        _seed_run(p)[1],
        _column_id(p),
        output_role="out",
        compatibility_key="k1",
        write_mode="create",
        claim_token="claim:none",
    ),
    "generations.seal": lambda p: ResultGenerationStore(p).seal(
        _seed_run(p)[1],
        [_column_id(p)],
        claim_token="claim:none",
        terminal_disposition="completed",
    ),
    "generations.rebuild_heads_from_key_table": lambda p: ResultGenerationStore(
        p
    ).rebuild_heads_from_key_table("temp_missing"),
    "generations.rebuild_heads": lambda p: ResultGenerationStore(p).rebuild_heads([]),
    "receipts.insert_in_transaction": lambda p: ReceiptStore(p).insert_in_transaction(
        _receipt()
    ),
    "receipts.insert_with_status": lambda p: ReceiptStore(p).insert_with_status(
        _receipt(), "running"
    ),
    "receipts.insert_running_in_transaction": lambda p: ReceiptStore(
        p
    ).insert_running_in_transaction(_receipt()),
    "receipts.insert_partial": lambda p: ReceiptStore(p).insert_partial(
        _receipt(status="partial")
    ),
    "receipts.insert_failed": lambda p: ReceiptStore(p).insert_failed(
        _receipt(status="failed")
    ),
    "receipts.insert_finished_in_transaction": lambda p: ReceiptStore(
        p
    ).insert_finished_in_transaction(_receipt(status="completed")),
    "receipts.update_body_status_in_transaction": lambda p: ReceiptStore(
        p
    ).update_body_status_in_transaction(_receipt()),
    "receipts.delete_running_in_transaction": lambda p: ReceiptStore(
        p
    ).delete_running_in_transaction("rcpt-none"),
    "cells.add_sheet_in_transaction": lambda p: p.add_sheet_in_transaction("Other"),
    "cells.add_column_in_transaction": lambda p: p.add_column_in_transaction(
        _sheet_id(p), "other"
    ),
    "cells.add_rows_in_transaction": lambda p: p.add_rows_in_transaction(
        _sheet_id(p), [], {"value": _column_id(p)}
    ),
    "cells.set_column_default_hidden": lambda p: p.set_column_default_hidden(
        _column_id(p), True
    ),
    "cells.set_column_type": lambda p: p.set_column_type(_column_id(p), "number"),
    "cells.set_column_format": lambda p: p.set_column_format(_column_id(p), "markdown"),
    "cells.set_column_semantic_type": lambda p: p.set_column_semantic_type(
        _column_id(p), "entity_mentions"
    ),
    "descriptors.set_descriptor": lambda p: set_descriptor(
        p, _column_id(p), "type", "number"
    ),
    "checkpoints.consume_and_retire_in_transaction": lambda p: EffectCheckpointStore(
        p.db
    ).consume_and_retire_in_transaction(
        EffectCheckpointRef(
            checkpoint_id="cp-none",
            family="test",
            group_key="1",
            unit_key="1",
            action_kind="map.template",
            identity="id-1",
        )
    ),
    "op_log.append_change_in_transaction": lambda p: (
        op_log.append_change_in_transaction(p, "edit", {})
    ),
    "project.append_change_in_transaction": lambda p: p.append_change_in_transaction(
        "edit", {}
    ),
    "project.set_meta_in_transaction": lambda p: p.set_meta_in_transaction("k", "v"),
    "preparation.prepare_validated": lambda p: preparation.prepare_validated(
        p,
        RunResultStore(p),
        _validated_spec(p)[0],
        validated=_validated_spec(p)[1],
        resume_run_id=None,
    ),
}


@pytest.mark.parametrize("write", sorted(JOINING_WRITES))
def test_joining_write_refuses_without_caller_transaction(project, write):
    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        JOINING_WRITES[write](project)

    assert not project.db.in_transaction


# -- owning writes refuse inside a caller's transaction ----------------------


def test_owning_writes_refuse_inside_caller_transaction_and_keep_it(project):
    sheet_id = _sheet_id(project)
    col_id = _column_id(project)
    row_id = _row_id(project)
    _op_id, run_id = _seed_run(project)

    ReceiptStore(project).insert_running(_receipt("rcpt_existing"))
    checkpoint_store = EffectCheckpointStore(project.db)
    owned_ref = EffectCheckpointRef(
        checkpoint_id="cp_owned",
        family="test",
        group_key="g",
        unit_key="u",
        action_kind="map.template",
        identity="id-1",
    )
    assert checkpoint_store.reserve(owned_ref) is True
    checkpoint_store.complete(owned_ref, payload={"ok": True})

    owning = {
        "runs.start_run": lambda: RunResultStore(project).start_run(
            1, sheet_id, "map.template"
        ),
        "runs.write_results": lambda: RunResultStore(project).write_results(
            run_id,
            [{"row_id": row_id, "column_id": col_id, "value": "x"}],
        ),
        "runs.write_results_with_model_calls": lambda: RunResultStore(
            project
        ).write_results(
            run_id,
            [
                {
                    "row_id": row_id,
                    "column_id": col_id,
                    "value": "x",
                    "model_calls": [_model_call("call-refused-inside-txn")],
                }
            ],
        ),
        "runs.publish_result_batch": lambda: RunResultStore(
            project
        ).publish_result_batch(
            run_id,
            [{"row_id": row_id, "column_id": col_id, "value": "x"}],
        ),
        "runs.publish_result_batch_with_evidence": lambda: RunResultStore(
            project
        ).publish_result_batch(
            run_id,
            [{"row_id": row_id, "column_id": col_id, "value": "x"}],
            citation_writer=lambda: None,
        ),
        "runs.write_receipt_row_outcomes": lambda: RunResultStore(
            project
        ).write_receipt_row_outcomes(
            "rcpt_existing",
            [{"row_id": row_id, "column_id": None, "outcome": "ok"}],
            writer_attempt_id="attempt-none",
        ),
        "runs.write_returned_call_accounting": lambda: RunResultStore(
            project
        ).write_returned_call_accounting(
            run_id,
            [{"row_id": row_id, "column_id": None, "model_calls": [_model_call("c1")]}],
        ),
        "runs.write_unscoped_model_calls": lambda: RunResultStore(
            project
        ).write_unscoped_model_calls([_model_call("c2")], row_id=None, column_id=None),
        "runs.finish_run": lambda: RunResultStore(project).finish_run(
            run_id, "completed"
        ),
        "runs.request_cancel": lambda: RunResultStore(project).request_cancel(run_id),
        "receipts.insert": lambda: ReceiptStore(project).insert(_receipt("rcpt_new")),
        "receipts.insert_running": lambda: ReceiptStore(project).insert_running(
            _receipt("rcpt_new_run")
        ),
        "receipts.insert_finished": lambda: ReceiptStore(project).insert_finished(
            _receipt("rcpt_new_fin", status="completed")
        ),
        "receipts.update_body_status": lambda: ReceiptStore(project).update_body_status(
            _receipt("rcpt_existing", status="completed"), require_status="running"
        ),
        "receipts.delete_running": lambda: ReceiptStore(project).delete_running(
            "rcpt_existing"
        ),
        "cells.add_sheet": lambda: project.add_sheet("Second"),
        "cells.add_column": lambda: project.add_column(sheet_id, "second"),
        "cells.add_rows": lambda: project.add_rows(
            sheet_id, [{"value": "second"}], {"value": col_id}
        ),
        "checkpoints.consume_and_retire": lambda: checkpoint_store.consume_and_retire(
            owned_ref
        ),
        "op_log.append_change": lambda: op_log.append_change(project, "edit", {}),
        "project.append_change": lambda: project.append_change("edit", {}),
        "project.set_meta": lambda: project.set_meta("probe", "value"),
    }

    for name, fn in owning.items():
        _open_caller_transaction(project)
        with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
            fn()
        _assert_caller_work_kept_uncommitted(project)
        assert _committed_count(project, "model_calls") == 0, name
        project.db.rollback()


# -- regression: ResultGenerationStore._write_scope never commits caller txn --


def test_result_generation_writes_never_commit_caller_transaction(project):
    sheet_id = _sheet_id(project)
    row_id = _row_id(project)
    output_col = project.add_column(sheet_id, "answer", ai_generated=True)
    _op_id, run_id = _seed_run(project)
    authority = run_writer_authority_fixture(
        project, run_id, output_column_ids={output_col}
    )
    assert authority.claim_token is not None
    generations = ResultGenerationStore(project)

    _open_caller_transaction(project)
    generations.declare(
        run_id,
        output_col,
        output_role="answer",
        compatibility_key="sha256:ownership-v1",
        write_mode="create",
        claim_token=authority.claim_token,
    )
    _assert_caller_work_kept_uncommitted(project)
    assert _committed_count(project, "run_output_generations") == 0

    RunResultStore(project).write_results_in_transaction(
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": output_col,
                "value": "hello",
                "publication_effect": "publish_value",
            }
        ],
        **authority.kwargs(),
    )
    generations.seal(
        run_id,
        [output_col],
        claim_token=authority.claim_token,
        terminal_disposition="completed",
    )
    _assert_caller_work_kept_uncommitted(project)
    assert _committed_count(project, "cell_result_heads") == 0

    generations.rebuild_heads([output_col])
    _assert_caller_work_kept_uncommitted(project)
    assert _committed_count(project, "cell_result_heads") == 0

    with generations.run_effect_key_table(run_id, [output_col]) as key_table:
        generations.rebuild_heads_from_key_table(key_table)
    _assert_caller_work_kept_uncommitted(project)
    assert _committed_count(project, "cell_result_heads") == 0

    project.db.rollback()
    assert not project.db.in_transaction
    assert _committed_sheet_name(project) == "Source"
    assert _committed_count(project, "run_output_generations") == 0
    assert _committed_count(project, "cell_result_heads") == 0
    assert _committed_count(project, "results") == 0


def test_result_generation_savepoint_failure_preserves_caller_transaction(project):
    sheet_id = _sheet_id(project)
    output_col = project.add_column(sheet_id, "answer", ai_generated=True)
    _op_id, run_id = _seed_run(project)
    authority = run_writer_authority_fixture(
        project, run_id, output_column_ids={output_col}
    )
    assert authority.claim_token is not None

    _open_caller_transaction(project)
    with pytest.raises(RuntimeError):
        ResultGenerationStore(project).declare(
            run_id,
            output_col,
            output_role="answer",
            compatibility_key="sha256:ownership-v1",
            write_mode="create",
            claim_token="claim:wrong-token",
        )
    # Caller's transaction and earlier write remain open and uncommitted.
    _assert_caller_work_kept_uncommitted(project)
    project.db.commit()
    assert _committed_sheet_name(project) == CALLER_SHEET_NAME
    assert _committed_count(project, "run_output_generations") == 0


# -- accounting survival across ChangePublicationClosed ----------------------


def test_write_results_and_publish_preserve_accounting_on_closed_change(project):
    sheet_id = _sheet_id(project)
    row_id = _row_id(project)
    output_col = project.add_column(sheet_id, "answer", ai_generated=True)
    op_id, run_id = _seed_run(project)
    authority = run_writer_authority_fixture(
        project, run_id, output_column_ids={output_col}
    )
    assert authority.claim_token is not None
    with write_transaction(project.db):
        ResultGenerationStore(project).declare(
            run_id,
            output_col,
            output_role="answer",
            compatibility_key="sha256:closed-v1",
            write_mode="create",
            claim_token=authority.claim_token,
        )
        ChangeStore(project).close(op_id)

    owned_batch = [
        {
            "row_id": row_id,
            "column_id": output_col,
            "value": "refused-owned",
            "publication_effect": "publish_value",
            "model_calls": [_model_call("call-closed-owned")],
        }
    ]
    with pytest.raises(GenerationStateError) as owned_exc:
        RunResultStore(project).write_results(
            run_id,
            owned_batch,
            **authority.kwargs(),
        )
    assert isinstance(owned_exc.value.__cause__, ChangePublicationClosed)
    assert not project.db.in_transaction
    assert _committed_count(project, "results") == 0
    assert _committed_count(project, "cell_result_heads") == 0
    assert {row[0] for row in _committed(project, "SELECT id FROM model_calls")} == {
        "call-closed-owned"
    }

    # Joining path keeps accounting in caller's transaction without committing it.
    joined_batch = [
        {
            "row_id": row_id,
            "column_id": output_col,
            "value": "refused-joined",
            "publication_effect": "publish_value",
            "model_calls": [_model_call("call-closed-joined")],
        }
    ]
    _open_caller_transaction(project)
    with pytest.raises(GenerationStateError) as joined_exc:
        RunResultStore(project).publish_result_batch_in_transaction(
            run_id,
            joined_batch,
            **authority.kwargs(),
        )
    assert isinstance(joined_exc.value.__cause__, ChangePublicationClosed)
    _assert_caller_work_kept_uncommitted(project)
    assert {row[0] for row in _committed(project, "SELECT id FROM model_calls")} == {
        "call-closed-owned"
    }
    in_txn_calls = {
        row[0] for row in project.db.execute("SELECT id FROM model_calls").fetchall()
    }
    assert in_txn_calls == {"call-closed-owned", "call-closed-joined"}
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    project.db.rollback()
    assert {row[0] for row in _committed(project, "SELECT id FROM model_calls")} == {
        "call-closed-owned"
    }


# -- joining writes across core modules roll back with caller ----------------


def test_joined_core_writes_roll_back_together_with_caller(project):
    sheet_id = _sheet_id(project)
    col_id = _column_id(project)
    row_id = _row_id(project)
    _op_id, run_id = _seed_run(project)
    ops_before = _committed_count(project, "ops")
    runs_before = _committed_count(project, "runs")
    rows_before = _committed_count(project, "rows")

    _open_caller_transaction(project)
    new_sheet = project.add_sheet_in_transaction("Joined")
    new_col = project.add_column_in_transaction(new_sheet, "c")
    project.add_rows_in_transaction(new_sheet, [{"c": "v"}], {"c": new_col})
    project.set_column_default_hidden(new_col, True)
    project.set_column_type(new_col, "number")
    project.set_column_format(new_col, "currency")
    project.set_column_semantic_type(new_col, "entity_mentions")
    project.set_meta_in_transaction("joined_key", "joined_val")
    joined_op = project.append_change_in_transaction("map", {"k": 1})
    joined_run = RunResultStore(project).start_run_in_transaction(
        joined_op, sheet_id, "map.template", row_ids=[row_id], total_rows=1
    )
    RunResultStore(project).point_column_at_run(joined_op, col_id, joined_run)
    RunResultStore(project).request_cancel_in_transaction(run_id)
    ReceiptStore(project).insert_finished_in_transaction(
        _receipt("rcpt_joined", status="completed")
    )
    spec, validated = _validated_spec(project)
    preparation.prepare_validated(
        project,
        RunResultStore(project),
        spec,
        validated=validated,
        resume_run_id=None,
    )

    _assert_caller_work_kept_uncommitted(project)
    project.db.rollback()

    assert not project.db.in_transaction
    assert _committed_count(project, "sheets") == 1
    assert _committed_count(project, "columns") == 1
    assert _committed_count(project, "rows") == rows_before
    assert _committed_count(project, "ops") == ops_before
    assert _committed_count(project, "runs") == runs_before
    assert _committed_count(project, "receipts") == 0
    assert _committed(project, "SELECT value FROM meta WHERE key='joined_key'") == []


# -- multi-statement owning writes roll back atomically on mid-op failure ----


def test_start_run_rolls_back_when_row_scope_insert_fails(project):
    sheet_id = _sheet_id(project)
    row_id = _row_id(project)
    op_id = project.append_change("map", {"action_kind": "map.template"})
    _refuse(project, "INSERT", "run_rows")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        RunResultStore(project).start_run(
            op_id, sheet_id, "map.template", row_ids=[row_id], total_rows=1
        )

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "runs") == 0
    assert _committed_count(project, "run_scopes") == 0


def test_add_rows_rolls_back_atomically_when_base_cell_insert_fails(project):
    sheet_id = _sheet_id(project)
    col_id = _column_id(project)
    rows_before = _committed_count(project, "rows")
    ops_before = _committed_count(project, "ops")
    _refuse(project, "INSERT", "cells")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        project.add_rows(sheet_id, [{"value": "atomic"}], {"value": col_id})

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "rows") == rows_before
    assert _committed_count(project, "ops") == ops_before


def test_request_cancel_rolls_back_when_change_close_fails(project):
    _op_id, run_id = _seed_run(project)
    _refuse(project, "UPDATE", "ops")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        RunResultStore(project).request_cancel(run_id)

    _assert_nothing_left_behind(project)
    assert _committed(
        project, "SELECT cancel_requested_at FROM runs WHERE id=?", (run_id,)
    ) == [(None,)]


def test_consume_and_retire_rolls_back_when_finalize_fails(project):
    store = EffectCheckpointStore(project.db)
    ref = EffectCheckpointRef(
        checkpoint_id="cp-atomic",
        family=ROW_EFFECT_CHECKPOINT_FAMILY,
        group_key="1",
        unit_key="1",
        action_kind="map.template",
        identity="id-atomic",
    )
    assert store.reserve(ref) is True
    store.complete(ref, payload={"ok": True})

    def failing_finalize(_cp: dict[str, Any]) -> None:
        project.db.execute("UPDATE sheets SET name='Should Roll Back'")
        raise RuntimeError("finalize boom")

    with pytest.raises(RuntimeError, match="finalize boom"):
        store.consume_and_retire(ref, finalize=failing_finalize)

    _assert_nothing_left_behind(project)
    assert _committed_sheet_name(project) == "Source"
    assert _committed(
        project, "SELECT state FROM effect_checkpoints WHERE id=?", ("cp-atomic",)
    ) == [("returned",)]
