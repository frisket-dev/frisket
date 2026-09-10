from __future__ import annotations

import hashlib
import io
from contextlib import contextmanager
from datetime import datetime
from zipfile import ZipFile

import pytest
from pydantic import ValidationError

from frisket.actions.import_xlsx import XLSX, ImportXlsxParams, import_xlsx
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionRequest, TableError
from frisket.authoring.column_types import register_column_type, unregister_column_type
from frisket.engine.executor import BoundLocalFile, ExecutorDeps, run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.streaming_import import StreamingSheetWriter

openpyxl = pytest.importorskip("openpyxl")


def _workbook(rows, *, other_sheet=None):
    workbook = openpyxl.Workbook()
    try:
        for row in rows:
            workbook.active.append(row)
        if other_sheet is not None:
            sheet = workbook.create_sheet("Selected")
            for row in other_sheet:
                sheet.append(row)
        stream = io.BytesIO()
        workbook.save(stream)
        return stream.getvalue()
    finally:
        workbook.close()


class MemoryFiles:
    def __init__(self, data):
        self.data = data
        self.opened = []
        self.closed = []

    def read_bytes(self, _path):
        raise AssertionError("XLSX must use the seekable admitted handle")

    @contextmanager
    def open_binary(self, path):
        self.opened.append(path)
        try:
            with io.BytesIO(self.data) as stream:
                yield stream
        finally:
            self.closed.append(path)


def _params(**overrides):
    return ImportXlsxParams.model_validate(
        {
            "source": {"kind": "file", "path": "input.xlsx"},
            "columns": [{"name": "name", "type": "text"}],
            **overrides,
        }
    )


def _values(table):
    return [row.output.root for row in table.rows]


@pytest.mark.parametrize("finish", [True, False])
def test_xlsx_is_lazy_and_closes_workbook_and_source(monkeypatch, finish):
    files = MemoryFiles(_workbook([["name"], ["Ada"], ["Grace"]]))
    load = openpyxl.load_workbook
    workbooks = []
    closed = []

    def track(source, **kwargs):
        assert kwargs == {"read_only": True, "data_only": True}
        assert hasattr(source, "seek")
        workbook = load(source, **kwargs)
        close = workbook.close

        def close_workbook():
            closed.append(workbook)
            close()

        workbook.close = close_workbook
        workbooks.append(workbook)
        return workbook

    monkeypatch.setattr(openpyxl, "load_workbook", track)
    result = import_xlsx(_params(), files)
    assert files.opened == []
    assert workbooks == []
    rows = iter(result.rows)
    assert next(rows).output.root == {"name": "Ada"}
    assert closed == []
    if finish:
        assert [row.output.root for row in rows] == [{"name": "Grace"}]
    else:
        rows.close()
    assert closed == workbooks
    assert len(closed) == 1
    assert files.closed == ["input.xlsx"]


def test_xlsx_native_values_null_headers_empty_rows_and_named_worksheet():
    params = _params(
        worksheet="Selected",
        columns=[
            {"name": "col0", "type": "text", "format": "markdown", "hidden": True},
            {"name": "count", "type": "integer"},
            {"name": "amount", "type": "number"},
            {"name": "active", "type": "boolean"},
            {"name": "date", "type": "text"},
            {"name": "json", "type": "json"},
            {"name": "missing", "type": "text"},
        ],
    )
    files = MemoryFiles(
        _workbook(
            [["wrong"]],
            other_sheet=[
                [None, "count", "amount", "active", "date", "json", "missing"],
                [None] * 7,
                ["**Ada**", 3, 1.25, True, datetime(2024, 1, 2, 3, 4), '[1,"x"]'],
            ],
        )
    )
    assert _values(import_xlsx(params, files)) == [
        {
            "col0": "**Ada**",
            "count": 3,
            "amount": 1.25,
            "active": True,
            "date": "2024-01-02 03:04:00",
            "json": [1, "x"],
            "missing": None,
        }
    ]
    columns = XLSX.run.resolve_columns(params)
    assert columns[0].format == "markdown"
    assert columns[0].hidden is True


def test_xlsx_uses_cached_formula_values():
    original = _workbook([["name"], ["=1+1"], ["=2+2"]])
    cached = io.BytesIO()
    with ZipFile(io.BytesIO(original)) as source, ZipFile(cached, "w") as target:
        for member in source.infolist():
            data = source.read(member.filename)
            if member.filename == "xl/worksheets/sheet1.xml":
                data = data.replace(b"<f>1+1</f><v></v>", b"<f>1+1</f><v>2</v>")
            target.writestr(member, data)
    params = _params(columns=[{"name": "name", "type": "integer"}])
    # A formula without a cached value reads as null and its empty row is skipped.
    assert _values(import_xlsx(params, MemoryFiles(cached.getvalue()))) == [{"name": 2}]


@pytest.mark.parametrize(
    ("rows", "params", "code", "details"),
    [
        (
            [[" name "], ["Ada"]],
            {},
            "xlsx_header_mismatch",
            {"expected": ["name"], "actual": [" name "]},
        ),
        (
            [["count", "name"], [1, "Ada"]],
            {
                "columns": [
                    {"name": "name", "type": "text"},
                    {"name": "count", "type": "integer"},
                ]
            },
            "xlsx_header_mismatch",
            {"expected": ["name", "count"], "actual": ["count", "name"]},
        ),
        (
            [["name"], [None], ["invalid"]],
            {"columns": [{"name": "name", "type": "integer"}]},
            "invalid_xlsx_value",
            {"line": 3, "column": "name", "type": "integer", "value": "invalid"},
        ),
    ],
)
def test_xlsx_refusals_preserve_diagnostics_and_close(rows, params, code, details):
    files = MemoryFiles(_workbook(rows))
    with pytest.raises(TableError) as error:
        _values(import_xlsx(_params(**params), files))
    assert error.value.code == code
    assert error.value.details == details
    assert files.closed == ["input.xlsx"]


@pytest.mark.parametrize("broken", ["archive", "worksheet", "close"])
def test_xlsx_parser_and_late_close_failures_are_actionable(monkeypatch, broken):
    data = b"invalid zip" if broken == "archive" else _workbook([["name"], ["Ada"]])
    params = _params(worksheet="Missing") if broken == "worksheet" else _params()
    if broken == "close":
        load = openpyxl.load_workbook

        def failing_close(*args, **kwargs):
            workbook = load(*args, **kwargs)
            close = workbook.close

            def fail():
                close()
                raise OSError("close failed")

            workbook.close = fail
            return workbook

        monkeypatch.setattr(openpyxl, "load_workbook", failing_close)
    files = MemoryFiles(data)
    with pytest.raises(TableError) as error:
        _values(import_xlsx(params, files))
    assert error.value.code == "xlsx_parse_failed"
    assert error.value.details["error"]
    assert files.closed == ["input.xlsx"]


@pytest.mark.parametrize(
    "changes",
    [
        {"source": {"path": "input.xlsx"}},
        {"source": {"kind": "file", "path": "input.xlsx", "fingerprint": "untrusted"}},
        {"columns": []},
        {"header": "absent"},
        {"sheet_name": "Params cannot name the destination"},
    ],
)
def test_xlsx_refuses_invalid_or_retired_params(changes):
    with pytest.raises(ValidationError):
        _params(**changes)


def test_xlsx_public_execution_normalizes_once_renames_and_replays(tmp_path):
    assert ACTION_REGISTRY.get("import.xlsx").definition is XLSX
    calls = []

    def parse(value):
        calls.append(value)
        if not isinstance(value, str) or value.endswith("!"):
            raise ValueError("already parsed")
        return value + "!"

    register_column_type(
        "xlsx_once",
        parse=parse,
        validate=lambda value: isinstance(value, str) and value.endswith("!"),
    )
    project = Project.create(tmp_path / "project")
    try:
        raw = _workbook([["name"], ["Ada"], ["Grace"]])
        source = tmp_path / "names.xlsx"
        source.write_bytes(raw)
        request = ActionRequest(
            action_id="import.xlsx",
            scope={"kind": "project"},
            sheet_name="Names",
            output_names={"name": "Display name"},
            params={
                "source": {
                    "kind": "file",
                    "path": str(source),
                    "label": "Original.xlsx",
                },
                "columns": [{"name": "name", "type": "xlsx_once", "hidden": True}],
            },
            idempotency_key="xlsx-once",
        )
        result = run_action_spec(
            project, request.model_dump(mode="json"), project_id="p"
        )
        assert result.status == "completed", result.errors
        assert calls == ["Ada", "Grace"]
        sheet = result.outputs[0].ref
        assert list(
            project.get_values(
                sheet["sheet_id"], sheet["columns"]["Display name"]
            ).values()
        ) == ["Ada!", "Grace!"]
        column = project.db.execute(
            "SELECT name, hidden FROM columns WHERE id=?",
            (sheet["columns"]["Display name"],),
        ).fetchone()
        assert tuple(column) == ("Display name", 1)
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert next(
            item.ref
            for item in receipt.inputs
            if item.ref.get("kind") == "local_file_read"
        ) == {
            "kind": "local_file_read",
            "path": str(source),
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "byte_count": len(raw),
        }
        source.unlink()
        replay = run_action_spec(
            project, request.model_dump(mode="json"), project_id="p"
        )
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
        assert calls == ["Ada", "Grace"]
        changed = request.model_copy(update={"sheet_name": "Changed"})
        conflict = run_action_spec(
            project, changed.model_dump(mode="json"), project_id="p"
        )
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"
    finally:
        project.close()
        unregister_column_type("xlsx_once")


def test_xlsx_late_invalid_cell_never_publishes_partial_sheet(tmp_path):
    source = tmp_path / "late.xlsx"
    source.write_bytes(
        _workbook([["name"], *[[index] for index in range(1200)], ["bad"]])
    )
    project = Project.create(tmp_path / "project")
    try:
        request = ActionRequest(
            action_id="import.xlsx",
            scope={"kind": "project"},
            sheet_name="Late failure",
            params={
                "source": {"kind": "file", "path": str(source)},
                "columns": [{"name": "name", "type": "integer"}],
            },
            idempotency_key="xlsx-late",
        )
        result = run_action_spec(
            project, request.model_dump(mode="json"), project_id="p"
        )
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_xlsx_value"
        assert result.errors[0].details["line"] == 1202
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    finally:
        project.close()


@pytest.mark.parametrize(
    ("failure", "code"),
    [("close", "xlsx_parse_failed"), ("hash", "invalid_file_source")],
)
def test_xlsx_final_source_failure_removes_already_staged_rows(
    tmp_path, monkeypatch, failure, code
):
    raw = _workbook([["name"], *[[index] for index in range(601)]])
    borrowed = io.BytesIO(raw)
    path = str(tmp_path / "admitted.xlsx")
    expected = hashlib.sha256(b"different" if failure == "hash" else raw).hexdigest()
    deps = ExecutorDeps(
        local_file_sources={
            path: BoundLocalFile(stream=borrowed, sha256="sha256:" + expected)
        }
    )
    project = Project.create(tmp_path / "project")
    appended = []
    closed = []
    append_rows = StreamingSheetWriter.append_rows
    load_workbook = openpyxl.load_workbook

    def observe_append(writer, rows):
        result = append_rows(writer, rows)
        appended.append(len(rows))
        assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] >= 500
        assert project.db.execute("SELECT hidden FROM sheets").fetchone()[0] == 1
        return result

    def observe_workbook(*args, **kwargs):
        workbook = load_workbook(*args, **kwargs)
        close = workbook.close

        def final_close():
            close()
            closed.append(True)
            assert sum(appended) >= 500
            if failure == "close":
                raise OSError("late workbook close failure")

        workbook.close = final_close
        return workbook

    monkeypatch.setattr(StreamingSheetWriter, "append_rows", observe_append)
    monkeypatch.setattr(openpyxl, "load_workbook", observe_workbook)
    try:
        result = run_action_spec(
            project,
            {
                "action_id": "import.xlsx",
                "scope": {"kind": "project"},
                "sheet_name": "Late source failure",
                "params": {
                    "source": {"kind": "file", "path": path},
                    "columns": [{"name": "name", "type": "integer"}],
                },
                "idempotency_key": "xlsx-source-failure",
            },
            project_id="p",
            deps=deps,
        )
        assert result.status == "failed"
        assert result.errors[0].code == code
        assert closed == [True]
        assert sum(appended) >= 500
        assert result.receipt_id is None
        for table in ("sheets", "columns", "rows", "cells", "ops", "receipts"):
            assert (
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )
        assert not borrowed.closed
        borrowed.seek(0)
        assert borrowed.read() == raw
    finally:
        project.close()
        borrowed.close()
