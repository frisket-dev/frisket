from __future__ import annotations

from contextlib import closing
import json

import pytest
from pydantic import BaseModel

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor import ExecutorDeps, ImportWorkloadLimits, run_action_spec
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.streaming_import import StreamingSheetWriter
from frisket.sdk import ActionParams, TableResult, TableRow, action, create_sheet


class NumberParams(ActionParams):
    count: int = 1001


class NumberRow(BaseModel):
    number: int


def _bound(handler, *, count=1001):
    definition = action(
        name="numbers",
        title="Numbers",
        description="Generate numbers lazily.",
        category=ActionCategory.CONVERT,
        run=create_sheet(handler),
    )
    registered = ActionRegistry(
        (ActionNamespace("custom", actions=(definition,)),)
    ).get("custom.numbers")
    return BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id="custom.numbers",
            scope={"kind": "project"},
            sheet_name="Numbers",
            output_names={"number": "Value"},
            params={"count": count},
            idempotency_key="numbers",
        ),
    )


def test_table_result_is_lazy_and_host_consumes_original_iterator_once(
    tmp_path, monkeypatch
):
    events = []

    class Once:
        def __init__(self, count):
            self.count, self.position = count, 0

        def __iter__(self):
            return self

        def __next__(self):
            if self.position == self.count:
                raise StopIteration
            self.position += 1
            return TableRow(output=NumberRow(number=self.position))

        def close(self):
            events.append("closed")

    def numbers(params: NumberParams) -> TableResult[NumberRow]:
        class Source:
            iterated = False

            def __iter__(self):
                assert not self.iterated
                self.iterated = True
                return Once(params.count)

        source = Source()
        result = TableResult(rows=source)
        assert not source.iterated
        return result

    original = StreamingSheetWriter.append_rows
    sizes = []

    def observe(self, rows):
        sizes.append(len(rows))
        return original(self, rows)

    monkeypatch.setattr(StreamingSheetWriter, "append_rows", observe)
    with closing(Project.create(tmp_path / "project")) as project:
        statements = []
        project.db.set_trace_callback(statements.append)
        # The import deployment ceiling is not inferred from source-free lineage.
        result = run_typed_create_sheet_action(
            project,
            "p",
            _bound(numbers),
            deps=ExecutorDeps(import_workload_limits=ImportWorkloadLimits(max_rows=2)),
        )
        assert result.status == "completed", result.errors
        project.db.set_trace_callback(None)
        assert statements.count("BEGIN IMMEDIATE") == 5
        assert statements.count("COMMIT") == 5
        assert events == ["closed"]
        assert sizes == [500, 500, 1]
        ref = result.outputs[0].ref
        assert ref["row_count"] == 1001
        assert set(ref["columns"]) == {"Value"}
        replay = run_typed_create_sheet_action(project, "p", _bound(numbers))
        assert replay.receipt_id == result.receipt_id
        assert events == ["closed"]


def _materialized_result(project, tmp_path, kind):
    def numbers(params: NumberParams) -> TableResult[NumberRow]:
        rows = [TableRow(output=NumberRow(number=n)) for n in range(params.count)]
        return TableResult(rows=tuple(rows) if kind == "custom.tuple" else rows)

    if kind.startswith("custom."):
        return run_typed_create_sheet_action(project, "p", _bound(numbers))
    params = {"columns": [{"name": "number", "type": "integer"}]}
    if kind == "import.rows":
        params["rows"] = [{"number": n} for n in range(1001)]
    else:
        source = tmp_path / "numbers.ndjson"
        source.write_text("".join(f'{{"number":{n}}}\n' for n in range(1001)))
        params["source"] = {"kind": "file", "path": str(source)}
    return run_action_spec(
        project,
        {
            "action_id": kind,
            "scope": {"kind": "project"},
            "sheet_name": "Numbers",
            "params": params,
            "idempotency_key": "numbers",
        },
        project_id="p",
    )


@pytest.mark.parametrize(
    "kind", ["import.rows", "import.ndjson", "custom.list", "custom.tuple"]
)
def test_materialized_rows_publish_in_one_transaction(tmp_path, kind):
    with closing(Project.create(tmp_path / "project")) as project:
        statements = []
        project.db.set_trace_callback(statements.append)
        result = _materialized_result(project, tmp_path, kind)
        project.db.set_trace_callback(None)
        assert result.status == "completed", result.errors
        assert statements.count("BEGIN IMMEDIATE") == 1
        assert statements.count("COMMIT") == 1
        assert not project.db.in_transaction
        assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 1001
        assert project.db.execute("SELECT hidden FROM sheets").fetchone()[0] == 0
        assert not any(".__streaming_import_" in statement for statement in statements)


@pytest.mark.parametrize("kind", ["import.rows", "import.ndjson", "custom.list"])
def test_materialized_publication_failure_rolls_back_all_writes(
    tmp_path, monkeypatch, kind
):
    with closing(Project.create(tmp_path / "project")) as project:
        attempted = []

        def fail_receipt(*_args, **_kwargs):
            assert project.db.in_transaction
            assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 1001
            assert (
                project.db.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == 1001
            )
            assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 1
            attempted.append(True)
            raise RuntimeError("receipt insertion failed after table writes")

        monkeypatch.setattr(ReceiptStore, "insert_completed", fail_receipt)
        statements = []
        project.db.set_trace_callback(statements.append)
        result = _materialized_result(project, tmp_path, kind)
        project.db.set_trace_callback(None)
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert attempted == [True]
        assert statements.count("BEGIN IMMEDIATE") == 1
        assert statements.count("ROLLBACK") == 1
        assert "COMMIT" not in statements
        assert not project.db.in_transaction
        for table in ("sheets", "columns", "rows", "cells", "ops", "receipts"):
            assert (
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )


@pytest.mark.parametrize("kind", ["import.rows", "import.ndjson", "import.csv"])
def test_whole_sheet_undo_preserves_hidden_columns_and_refuses_claims(tmp_path, kind):
    params = {
        "columns": [
            {"name": "number", "type": "integer"},
            {"name": "note", "type": "text", "hidden": True},
        ]
    }
    row = {"number": 1, "note": "original"}
    if kind == "import.rows":
        params["rows"] = [row]
    elif kind == "import.ndjson":
        source = tmp_path / "numbers.ndjson"
        source.write_text(json.dumps(row) + "\n")
        params["source"] = {"kind": "file", "path": str(source)}
    else:
        source = tmp_path / "numbers.csv"
        source.write_text("number,note\n1,original\n")
        params["sources"] = [{"path": str(source)}]
    with closing(Project.create(tmp_path / "project")) as project:
        result = run_action_spec(
            project,
            {
                "action_id": kind,
                "scope": {"kind": "project"},
                "sheet_name": "Numbers",
                "params": params,
                "idempotency_key": "sheet-transition",
            },
            project_id="p",
        )
        assert result.status == "completed", result.errors
        sheet_id = result.outputs[0].ref["sheet_id"]
        assert json.loads(
            project.db.execute("SELECT undo_info FROM ops").fetchone()[0]
        ) == {"created_sheets": [sheet_id]}
        original_columns = [
            tuple(column) for column in project.db.execute("SELECT * FROM columns")
        ]
        claims = OutputColumnClaimStore(project)
        for transition, expected_hidden in ((project.undo, 1), (project.redo, 0)):
            acquired, conflict = claims.acquire(
                sheet_id=sheet_id,
                output_names=["note"],
                action_kind="map.template",
                claim_token="claim:sheet-transition",
            )
            assert len(acquired) == 1 and conflict is None
            with pytest.raises(ValueError, match="output_column_busy"):
                transition()
            assert project.db.execute("SELECT hidden FROM sheets").fetchone()[0] == (
                1 - expected_hidden
            )
            claims.release(claim_token="claim:sheet-transition")
            assert transition() == result.op_ids[0]
            assert (
                project.db.execute("SELECT hidden FROM sheets").fetchone()[0]
                == expected_hidden
            )
            assert [
                tuple(column) for column in project.db.execute("SELECT * FROM columns")
            ] == original_columns


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_late_producer_failure_removes_committed_hidden_batches(
    tmp_path, monkeypatch, failure
):
    closed = []

    def numbers(params: NumberParams) -> TableResult[NumberRow]:
        def rows():
            try:
                for number in range(params.count):
                    yield TableRow(output=NumberRow(number=number))
                raise failure("late producer failure")
            finally:
                closed.append(True)

        return TableResult(rows=rows())

    appended = []
    original = StreamingSheetWriter.append_rows

    def observe(self, rows):
        result = original(self, rows)
        assert not self.project.db.in_transaction
        assert (
            self.project.db.execute(
                "SELECT hidden FROM sheets WHERE id=?", (self.sheet_id,)
            ).fetchone()[0]
            == 1
        )
        appended.append(len(result))
        return result

    monkeypatch.setattr(StreamingSheetWriter, "append_rows", observe)
    with closing(Project.create(tmp_path / "project")) as project:
        if failure is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt):
                run_typed_create_sheet_action(project, "p", _bound(numbers))
        else:
            result = run_typed_create_sheet_action(project, "p", _bound(numbers))
            assert result.status == "failed"
            assert result.errors[0].code == "project_write_failed"
        assert closed == [True]
        assert appended == [500, 500]
        for table in ("sheets", "rows", "cells", "ops", "receipts"):
            assert (
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )
