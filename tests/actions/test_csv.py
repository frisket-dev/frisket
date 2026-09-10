from __future__ import annotations

import csv
import hashlib
import io
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry
from frisket.actions.imports import CSV, ImportCsvParams, import_csv
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, TableError
from frisket.authoring.column_types import register_column_type, unregister_column_type
from frisket.contracts.action import ActionSpec, Receipt, ReceiptIO
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.action_support import _params_hash
from frisket.engine.executor.action_inventory import BoundLocalFile
from frisket.engine.executor.local_file_read import AdmittedLocalFileReader
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.sdk import (
    ActionParams,
    LocalFileReader,
    TableResult,
    TableRow,
    action,
    create_sheet,
)


class FeedParams(ActionParams):
    folder: str
    feed: str


class FeedRow(BaseModel):
    name: str
    count: int


def streaming_feed(params: FeedParams, files: LocalFileReader) -> TableResult[FeedRow]:
    def rows():
        with files.open_text(str(Path(params.folder) / f"{params.feed}.tsv")) as stream:
            for name, count in csv.reader(stream, delimiter="\t"):
                yield TableRow(output=FeedRow(name=name.upper(), count=int(count)))

    return TableResult(rows=rows())


def test_renamed_source_static_producer_reuses_streaming_reader_and_actual_hash(
    tmp_path,
):
    raw = "Café\t3\r\nAda\t5\r\n".encode()
    source = tmp_path / "people.tsv"
    source.write_bytes(raw)
    definition = action(
        name="feed",
        title="Feed",
        description="Read a headerless feed lazily.",
        category=ActionCategory.CONVERT,
        run=create_sheet(streaming_feed),
    )
    registered = ActionRegistry(
        (ActionNamespace("custom", actions=(definition,)),)
    ).get("custom.feed")
    bound = BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id="custom.feed",
            scope={"kind": "project"},
            sheet_name="Feed",
            params={"folder": str(tmp_path), "feed": "people"},
            idempotency_key="feed-once",
        ),
    )
    project = Project.create(tmp_path / "project")
    try:
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "completed", result.errors
        sheet = result.outputs[0].ref
        assert list(
            project.get_values(sheet["sheet_id"], sheet["columns"]["name"]).values()
        ) == ["CAFÉ", "ADA"]
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert [
            item.ref
            for item in receipt.inputs
            if item.ref.get("kind") == "local_file_read"
        ] == [
            {
                "kind": "local_file_read",
                "path": str(source),
                "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "byte_count": len(raw),
            }
        ]
        source.unlink()
        replay = run_typed_create_sheet_action(project, "p", bound)
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
    finally:
        project.close()


@pytest.mark.parametrize("refusal", [None, "partial", "changed"])
def test_streaming_reader_borrows_source_and_verifies_complete_consumption(refusal):
    raw = b"first\r\nsecond\r\n"
    borrowed = io.BytesIO(raw)
    reader = AdmittedLocalFileReader(
        {
            "/admitted/feed.txt": BoundLocalFile(
                stream=borrowed,
                sha256="sha256:"
                + hashlib.sha256(
                    b"original" if refusal == "changed" else raw
                ).hexdigest(),
            )
        }
    )

    def consume():
        with reader.open_text("/admitted/feed.txt") as source:
            return source.read(1) if refusal == "partial" else list(source)

    try:
        if refusal:
            with pytest.raises(TableError) as error:
                consume()
            assert error.value.code == "invalid_file_source"
            assert reader.facts == []
        else:
            assert consume() == ["first\r\n", "second\r\n"]
            assert reader.facts[0]["byte_count"] == len(raw)
    finally:
        reader.close()
    assert not borrowed.closed
    borrowed.seek(0)
    assert borrowed.read() == raw


class MemoryFiles:
    def __init__(self, sources):
        self.sources = sources
        self.opened = []
        self.closed = []

    def read_bytes(self, _path):
        raise AssertionError("CSV must use streaming reads")

    @contextmanager
    def open_text(self, path, *, encoding="utf-8", newline=""):
        self.opened.append(path)
        try:
            with io.TextIOWrapper(
                io.BytesIO(self.sources[path]), encoding=encoding, newline=newline
            ) as stream:
                yield stream
        finally:
            self.closed.append(path)


def _params(**overrides):
    return ImportCsvParams.model_validate(
        {
            "sources": [{"path": "input.csv"}],
            "columns": [{"name": "name", "type": "text"}],
            **overrides,
        }
    )


def _values(result):
    return [row.output.root for row in result.rows]


def test_csv_is_registered_once_and_producer_is_lazy_and_closes():
    assert ACTION_REGISTRY.get("import.csv").definition is CSV
    files = MemoryFiles({"input.csv": b"name\nAda\nGrace\n"})
    table = import_csv(_params(), files)
    assert files.opened == []
    rows = iter(table.rows)
    assert next(rows).output.root == {"name": "Ada"}
    assert files.closed == []
    rows.close()
    assert files.closed == ["input.csv"]


def test_csv_source_headers_preserve_group_order_and_labels():
    params = _params(
        sources=[
            {"path": "first.csv", "label": "folder/first.csv"},
            {"path": "second.csv", "headers": ["count", "name"]},
        ],
        columns=[
            {"name": "name", "type": "text", "format": "markdown", "hidden": True},
            {"name": "count", "type": "integer"},
            {"name": "origin", "type": "text"},
        ],
        source_label_column="origin",
    )
    files = MemoryFiles(
        {"first.csv": b"name,count\nAda,2\n", "second.csv": b"count,name\n3,Grace\n"}
    )
    assert _values(import_csv(params, files)) == [
        {"name": "Ada", "count": 2, "origin": "folder/first.csv"},
        {"name": "Grace", "count": 3, "origin": "second.csv"},
    ]
    columns = CSV.run.resolve_columns(params)
    assert columns[0].format == "markdown"
    assert columns[0].hidden is True


@pytest.mark.parametrize("source_count", [1, 2])
def test_csv_receipt_preserves_only_unambiguous_source_label(tmp_path, source_count):
    sources = []
    for index in range(source_count):
        path = tmp_path / f"source-{index}.csv"
        path.write_text("name\nAda\n")
        sources.append({"path": str(path), "label": f"Original source {index}"})
    project = Project.create(tmp_path / "project")
    try:
        result = run_action_spec(
            project,
            ActionRequest(
                action_id="import.csv",
                scope={"kind": "project"},
                sheet_name="Imported",
                params=_params(sources=sources).model_dump(mode="json"),
                idempotency_key="source-label",
            ).model_dump(mode="json"),
            project_id="p",
        )
        assert result.status == "completed", result.errors
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        summary = next(
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "import_source"
        )
        assert summary.get("label") == (
            "Original source 0" if source_count == 1 else None
        )
        spec = json.loads(project.db.execute("SELECT spec FROM ops").fetchone()[0])
        assert [source["label"] for source in spec["params"]["sources"]] == [
            source["label"] for source in sources
        ]
        assert (
            len(
                [
                    item
                    for item in receipt.inputs
                    if item.ref.get("kind") == "local_file_read"
                ]
            )
            == source_count
        )
    finally:
        project.close()


def test_csv_encoding_delimiter_decimal_boolean_json_and_nulls():
    params = _params(
        sources=[
            {
                "path": "input.csv",
                "encoding": "utf-16",
                "delimiter": ";",
                "decimal_separator": ",",
            }
        ],
        columns=[
            {"name": "name", "type": "text"},
            {"name": "amount", "type": "number"},
            {"name": "active", "type": "boolean"},
            {"name": "data", "type": "json"},
            {"name": "empty", "type": "text"},
        ],
    )
    data = 'name;amount;active;data;empty\r\n"Ada\r\nLovelace";1,25;yes;[1,2];\r\n'
    assert _values(
        import_csv(params, MemoryFiles({"input.csv": data.encode("utf-16")}))
    ) == [
        {
            "name": "Ada\r\nLovelace",
            "amount": 1.25,
            "active": True,
            "data": [1, 2],
            "empty": None,
        }
    ]


def test_csv_scanned_headers_accept_whitespace_but_direct_headers_are_exact():
    body = b" name \nAda\n"
    params = _params(sources=[{"path": "input.csv", "headers": ["name"]}])
    assert _values(import_csv(params, MemoryFiles({"input.csv": body}))) == [
        {"name": "Ada"}
    ]
    with pytest.raises(TableError, match="CSV header"):
        _values(import_csv(_params(), MemoryFiles({"input.csv": body})))


@pytest.mark.parametrize(
    ("body", "columns", "code", "details"),
    [
        (
            b"other\nAda\n",
            [{"name": "name", "type": "text"}],
            "csv_header_mismatch",
            {"expected": ["name"], "actual": ["other"]},
        ),
        (
            b"name\nAda,2\n",
            [{"name": "name", "type": "text"}],
            "csv_parse_failed",
            {"line": 2, "expected_columns": 1, "actual_columns": 2},
        ),
        (
            b"name\ninvalid\n",
            [{"name": "name", "type": "integer"}],
            "invalid_csv_value",
            {"line": 2, "column": "name", "type": "integer", "value": "invalid"},
        ),
        (
            b'name\n"unclosed\n',
            [{"name": "name", "type": "text"}],
            "csv_parse_failed",
            {"error": "unexpected end of data"},
        ),
        (
            b"name\nnan\n",
            [{"name": "name", "type": "number"}],
            "invalid_csv_value",
            {"line": 2, "column": "name", "type": "number", "value": "nan"},
        ),
    ],
)
def test_csv_refusals_have_diagnostics_and_close(body, columns, code, details):
    files = MemoryFiles({"input.csv": body})
    with pytest.raises(TableError) as error:
        _values(import_csv(_params(columns=columns), files))
    assert error.value.code == code
    assert (error.value.details or {}) == details
    assert files.closed == ["input.csv"]


@pytest.mark.parametrize(
    "changes",
    [
        {"sources": []},
        {"sources": [{"path": "x", "delimiter": "||"}]},
        {"sources": [{"path": "x", "headers": ["name", "name"]}]},
        {"sources": [{"path": "x", "headers": ["other"]}]},
        {"source_label_column": "missing"},
        {"source_label_column": "name"},
    ],
)
def test_csv_rejects_invalid_semantic_configuration(changes):
    with pytest.raises(ValidationError):
        _params(**changes)


def test_csv_normalizes_registered_values_once():
    calls = []

    def parse(value):
        calls.append(value)
        if not isinstance(value, str) or value.endswith("!"):
            raise ValueError("already parsed")
        return value + "!"

    register_column_type(
        "csv_once",
        parse=parse,
        validate=lambda value: isinstance(value, str) and value.endswith("!"),
    )
    try:
        params = _params(columns=[{"name": "name", "type": "csv_once"}])
        assert _values(
            import_csv(params, MemoryFiles({"input.csv": b"name\nAda\n"}))
        ) == [{"name": "Ada!"}]
        assert calls == ["Ada"]
    finally:
        unregister_column_type("csv_once")


def test_retired_csv_wire_cannot_replay_a_stored_receipt(tmp_path):
    retired = {
        "schema_version": "frisket.action.v2",
        "kind": "import.csv",
        "capabilities": ["project:write"],
        "params": {
            "sheet_name": "Imported",
            "mode": "create_sheet",
            "source": {"kind": "file", "path": str(tmp_path / "missing.csv")},
            "columns": [{"name": "name", "type": "text"}],
            "header": "present",
        },
        "idempotency_key": "retired-import",
    }
    request_hash = _params_hash(ActionSpec.model_validate(retired))
    project = Project.create(tmp_path / "project")
    try:
        ReceiptStore(project).insert_completed(
            Receipt(
                receipt_id="retired-receipt",
                project_id="p",
                action_id="retired-action",
                action_kind="import.csv",
                idempotency_key="retired-import",
                params_hash=request_hash,
                status="completed",
                inputs=[
                    ReceiptIO(
                        name="csv_rows",
                        ref={"kind": "csv_rows", "params_hash": request_hash},
                    )
                ],
            )
        )
        result = run_action_spec(project, retired, project_id="p")
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
        assert result.receipt_id is None
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
    finally:
        project.close()
