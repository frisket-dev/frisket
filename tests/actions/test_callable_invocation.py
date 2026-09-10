"""Host invocation callbacks share native callable reservation and finalization."""

from pathlib import Path

import pytest

from test_callable_host import ReportParams, _bound, _query, source as source
from test_entity_package import (
    Params,
    _bound as _entity_bound,
    _selection,
    entities as entities,
)
from frisket.actions.entity_package_types import (
    FollowTheMoneyExporter,
    FollowTheMoneyImporter,
)
from frisket.actions.types import SheetCsvExporter
from frisket.contracts.action import ReceiptIO
from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
from frisket.engine.executor.callable_action import (
    run_callable_invocation,
    run_typed_callable_action,
)
from frisket.engine.executor.query_action import QueryPreviewCapability
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.engine.store.receipts import ReceiptStore


def _action():
    return _TypedProjectEnvelope("custom.report", "once", {})


def test_host_callbacks_reserve_record_validate_and_replay(source):
    project, params, _rows = source
    calls = []

    def invoke(invocation):
        calls.append("invoke")
        assert (
            ReceiptStore(project).parsed_by_id(invocation.receipt.receipt_id).status
            == "running"
        )
        return QueryPreviewCapability(invocation).preview(
            query=_query(params["source"]), limit=500, offset=0
        )

    def validate(value):
        calls.append("validate")
        return {"selected": value.row_count}

    def run(params_hash="admitted-hash"):
        return run_callable_invocation(
            project,
            "p",
            _action(),
            params_hash=params_hash,
            invoke=invoke,
            validate_return=validate,
        )

    result = run()
    assert result.status == "completed"
    assert result.value == {"selected": 2}
    assert len(result.outputs) == 1
    assert run() == result
    assert run("different-package-hash").errors[0].code == "idempotency_conflict"
    assert calls == ["invoke", "validate"]


@pytest.mark.parametrize("phase", ["invoke", "validate"])
def test_host_callback_teardown_failure_escapes_and_blocks_reexecution(source, phase):
    project, _params, _rows = source
    fatal = SandboxTeardownError("owned child was not reaped")
    calls = []

    def invoke(_invocation):
        calls.append("invoke")
        if phase == "invoke":
            raise fatal
        return {}

    def validate(value):
        raise fatal

    def run():
        return run_callable_invocation(
            project,
            "p",
            _action(),
            params_hash="admitted-hash",
            invoke=invoke,
            validate_return=validate,
        )

    with pytest.raises(SandboxTeardownError) as caught:
        run()
    assert caught.value is fatal
    assert run().errors[0].code == "idempotency_in_progress"
    assert calls == ["invoke"]


def test_native_teardown_failure_preserves_completed_primitive(source):
    project, params, _rows = source
    fatal = SandboxTeardownError("owned child was not reaped")

    def report(params: ReportParams, csv: SheetCsvExporter) -> None:
        csv.write_sheet_csv(
            sheet_id=params.source,
            path=str(Path(params.folder) / "saved.csv"),
            query=None,
            formula_policy="escape",
        )
        raise fatal

    bound, _registry = _bound(report, params)
    with pytest.raises(SandboxTeardownError) as caught:
        run_typed_callable_action(project, "p", bound)
    assert caught.value is fatal
    result = run_typed_callable_action(project, "p", bound)
    assert result.errors[0].code == "idempotency_in_progress"
    receipt = ReceiptStore(project).parsed_by_id(result.errors[0].details["receipt_id"])
    assert receipt.status == "running"
    assert len(receipt.exports) == 1
    assert (Path(params["folder"]) / "saved.csv").exists()


def test_shared_publication_refuses_nonfinite_callback_value(source):
    project, _params, _rows = source
    result = run_callable_invocation(
        project,
        "p",
        _action(),
        params_hash="admitted-hash",
        invoke=lambda _invocation: None,
        validate_return=lambda _value: {"number": float("nan")},
    )
    assert result.status == "failed"
    assert result.errors[0].code == "invalid_action_result"
    assert result.value is None


def test_same_package_bytes_keep_each_export_filename(source, entities):
    project, _params, _rows = source
    path, _values = entities
    calls = []

    def report(
        params: Params,
        importer: FollowTheMoneyImporter,
        exporter: FollowTheMoneyExporter,
    ) -> list[str]:
        calls.append("invoke")
        dataset = importer.import_entities(source_path=params.document)
        rowsets, mappings = _selection(dataset)
        first = exporter.export_entities(
            rowsets=rowsets, mappings=mappings, filename="a.zip"
        )
        second = exporter.export_entities(
            rowsets=rowsets, mappings=mappings, filename="b.zip"
        )
        assert first.blob_hash == second.blob_hash
        return [first.filename, second.filename]

    bound, _registry = _entity_bound(report, {"document": str(path)})
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "completed", result.errors
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert [item["filename"] for item in receipt.exports] == ["a.zip", "b.zip"]
    package_outputs = [
        item.ref
        for item in receipt.outputs
        if item.ref.get("kind") == "export_project_file"
    ]
    assert [item["filename"] for item in package_outputs] == ["a.zip", "b.zip"]
    assert run_typed_callable_action(project, "p", bound) == result
    assert calls == ["invoke"]


def test_project_file_receipt_records_keep_each_filename(source):
    project, _params, _rows = source

    def invoke(invocation):
        for filename in ("a.zip", "b.zip"):
            ref = {
                "kind": "export_project_file",
                "blob_hash": "a" * 64,
                "filename": filename,
            }
            invocation.record(
                outputs=[ReceiptIO(name=filename, ref=ref)], exports=[ref]
            )
        return None

    result = run_callable_invocation(
        project,
        "p",
        _action(),
        params_hash="admitted-hash",
        invoke=invoke,
        validate_return=lambda value: value,
    )
    assert result.status == "completed", result.errors
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert [item["filename"] for item in receipt.exports] == ["a.zip", "b.zip"]
    assert [item.ref["filename"] for item in receipt.outputs] == ["a.zip", "b.zip"]
