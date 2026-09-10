from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path

import pytest
from pydantic import BaseModel

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.authoring.column_types import register_column_type, unregister_column_type
from frisket.contracts.action import ActionSpec, Receipt, ReceiptIO
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.action_support import _params_hash
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.sdk import (
    ActionParams,
    DynamicOutput,
    LocalFileReader,
    TableColumn,
    TableResult,
    TableRow,
    action,
    create_sheet,
)


class TabSeparatedParams(ActionParams):
    directory: str
    stem: str
    skip_read: bool = False


class TabSeparatedRow(BaseModel):
    name: str
    count: int


def tab_separated(
    params: TabSeparatedParams, files: LocalFileReader
) -> TableResult[TabSeparatedRow]:
    rows = []
    if not params.skip_read:
        data = files.read_bytes(str(Path(params.directory) / f"{params.stem}.tsv"))
        with io.TextIOWrapper(io.BytesIO(data), encoding="utf-8") as stream:
            for name, count in csv.reader(stream, delimiter="\t"):
                rows.append(
                    TableRow(
                        output=TabSeparatedRow(name=name.upper(), count=int(count))
                    )
                )
    return TableResult(
        rows=rows,
        source={
            "kind": "file",
            "label": "A custom feed",
            "path": "/invented",
            "fingerprint": "sha256:invented",
            "request_hash": "invented",
            "line_count": 1000,
        },
    )


def _bound(handler, params, *, columns_from=None):
    definition = action(
        name="tabular",
        title="Tabular",
        description="Read a custom file format.",
        category=ActionCategory.CONVERT,
        run=create_sheet(handler, columns_from=columns_from),
    )
    registered = ActionRegistry(
        (ActionNamespace("custom", actions=(definition,)),)
    ).get("custom.tabular")
    return BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id="custom.tabular",
            scope={"kind": "project"},
            sheet_name="result",
            params=params,
            idempotency_key="custom-table",
        ),
    )


def test_second_format_derived_path_and_observed_facts(tmp_path, monkeypatch):
    from frisket.engine.executor import table_action

    def unexpected_embedding_reader(*_args):
        raise AssertionError("undeclared embedding reader was instantiated")

    monkeypatch.setattr(
        table_action, "AdmittedEmbeddingIndexReader", unexpected_embedding_reader
    )
    data = b"Ada\t3\nGrace\t5\n"
    source = tmp_path / "people.tsv"
    source.write_bytes(data)
    project = Project.create(tmp_path / "project")
    try:
        bound = _bound(tab_separated, {"directory": str(tmp_path), "stem": "people"})
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "completed", result.errors
        sheet = result.outputs[0].ref
        assert list(
            project.get_values(sheet["sheet_id"], sheet["columns"]["name"]).values()
        ) == ["ADA", "GRACE"]
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        facts = [
            item.ref
            for item in receipt.inputs
            if item.ref.get("kind") == "local_file_read"
        ]
        assert facts == [
            {
                "kind": "local_file_read",
                "path": str(source),
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                "byte_count": len(data),
            }
        ]
        summary = next(
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "import_source"
        )
        assert summary["path"] == str(source)
        assert summary["fingerprint"] == facts[0]["sha256"]
        assert summary["line_count"] == 2
        assert summary["label"] == "A custom feed"
        assert summary["request_hash"] != "invented"
        source.unlink()
        assert (
            run_typed_create_sheet_action(project, "p", bound).receipt_id
            == result.receipt_id
        )
    finally:
        project.close()


def test_unused_file_capability_does_not_claim_a_read(tmp_path):
    project = Project.create(tmp_path / "project")
    try:
        bound = _bound(
            tab_separated,
            {"directory": str(tmp_path), "stem": "missing", "skip_read": True},
        )
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "completed", result.errors
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert not any(
            item.ref.get("kind") in {"local_file_read", "file_rows"}
            for item in receipt.inputs
        )
        assert not any(
            item.ref.get("kind") == "import_source" for item in receipt.evidence
        )
    finally:
        project.close()


def test_file_reader_refuses_symlink_loop_without_writes(tmp_path):
    source = tmp_path / "loop.tsv"
    source.symlink_to(source.name)
    project = Project.create(tmp_path / "project")
    try:
        bound = _bound(tab_separated, {"directory": str(tmp_path), "stem": "loop"})
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_file_source"
        assert result.errors[0].field == "params"
        assert result.receipt_id is None
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
    finally:
        project.close()


def test_retired_ndjson_wire_cannot_replay_a_stored_receipt(tmp_path):
    retired = {
        "schema_version": "frisket.action.v2",
        "kind": "import.ndjson",
        "capabilities": ["project:write"],
        "params": {
            "sheet_name": "Imported",
            "mode": "create_sheet",
            "source": {"kind": "file", "path": str(tmp_path / "missing.ndjson")},
            "columns": [{"name": "name", "type": "text"}],
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
                action_kind="import.ndjson",
                idempotency_key="retired-import",
                params_hash=request_hash,
                status="completed",
                inputs=[
                    ReceiptIO(
                        name="ndjson_rows",
                        ref={"kind": "ndjson_rows", "request_hash": request_hash},
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


def test_ndjson_retry_replays_original_read_and_new_key_imports_changed_bytes(
    tmp_path, monkeypatch
):
    from frisket.engine.executor.local_file_read import AdmittedLocalFileReader

    source = tmp_path / "data.ndjson"
    original = b'{"name":"Original"}\n'
    changed = b'{"name":"Changed"}\n'
    source.write_bytes(original)
    request = {
        "action_id": "import.ndjson",
        "scope": {"kind": "project"},
        "sheet_name": "Original",
        "params": {
            "source": {"kind": "file", "path": str(source)},
            "columns": [{"name": "name", "type": "text"}],
        },
        "idempotency_key": "original-read",
    }
    project = Project.create(tmp_path / "project")
    try:
        first = run_action_spec(project, request, project_id="p")
        assert first.status == "completed", first.errors
        source.write_bytes(changed)

        def unexpected_read(*_args):
            raise AssertionError("a retry must not reread source bytes")

        with monkeypatch.context() as retry_patch:
            retry_patch.setattr(AdmittedLocalFileReader, "read_bytes", unexpected_read)
            replay = run_action_spec(project, request, project_id="p")
            assert replay.status == "completed", replay.errors
            assert replay.receipt_id == first.receipt_id
            conflict = run_action_spec(
                project, {**request, "sheet_name": "Different"}, project_id="p"
            )
            assert conflict.status == "failed"
            assert conflict.errors[0].code == "idempotency_conflict"

        second = run_action_spec(
            project,
            {**request, "sheet_name": "Updated", "idempotency_key": "updated-read"},
            project_id="p",
        )
        assert second.status == "completed", second.errors
        assert second.receipt_id != first.receipt_id
        for result, data, value in (
            (replay, original, "Original"),
            (second, changed, "Changed"),
        ):
            sheet = result.outputs[0].ref
            assert list(
                project.get_values(sheet["sheet_id"], sheet["columns"]["name"]).values()
            ) == [value]
            receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
            read = next(
                item.ref
                for item in receipt.inputs
                if item.ref.get("kind") == "local_file_read"
            )
            assert read["sha256"] == "sha256:" + hashlib.sha256(data).hexdigest()
            assert read["byte_count"] == len(data)
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 2
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 2
    finally:
        project.close()


@pytest.mark.parametrize("kind", ["import.rows", "import.ndjson"])
def test_registered_nonidempotent_parser_runs_once(tmp_path, kind):
    calls = []

    def parse(value):
        calls.append(value)
        return {"parsed": value}

    register_column_type(
        "one_parse",
        parse=parse,
        validate=lambda value: (
            isinstance(value, dict) and isinstance(value.get("parsed"), str)
        ),
    )
    project = Project.create(tmp_path / "project")
    try:
        params = {"columns": [{"name": "value", "type": "one_parse"}]}
        if kind == "import.rows":
            params["rows"] = [{"value": "raw"}]
        else:
            source = tmp_path / "data.ndjson"
            source.write_text('{"value":"raw"}\n')
            params["source"] = {"kind": "file", "path": str(source)}
        result = run_action_spec(
            project,
            {
                "action_id": kind,
                "scope": {"kind": "project"},
                "sheet_name": "result",
                "params": params,
                "idempotency_key": "parse-once",
            },
            project_id="p",
        )
        assert result.status == "completed", result.errors
        assert calls == ["raw"]
        sheet = result.outputs[0].ref
        assert list(
            project.get_values(sheet["sheet_id"], sheet["columns"]["value"]).values()
        ) == [{"parsed": "raw"}]
    finally:
        project.close()
        unregister_column_type("one_parse")


class RawParams(ActionParams):
    pass


def unnormalized(_params: RawParams) -> TableResult[DynamicOutput]:
    return TableResult(rows=[TableRow(output=DynamicOutput({"value": "raw"}))])


def test_host_rejects_unnormalized_values_instead_of_parsing_them(tmp_path):
    calls = []
    register_column_type(
        "one_parse",
        parse=lambda value: calls.append(value) or {"parsed": value},
        validate=lambda value: isinstance(value, dict),
    )
    project = Project.create(tmp_path / "project")
    try:
        bound = _bound(
            unnormalized,
            {},
            columns_from=lambda _params: (TableColumn("value", "one_parse"),),
        )
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_params"
        assert calls == []
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
    finally:
        project.close()
        unregister_column_type("one_parse")
