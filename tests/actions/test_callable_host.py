"""Actual reserved callable execution, including completed primitive failures."""

from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry, action
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    InvocationContext,
    QueryPreviewer,
    SheetCsvExporter,
    SheetJsonlExporter,
    SheetParquetExporter,
    WorkLogExporter,
)
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.action_inventory import ExecutorDeps
from frisket.engine.executor.callable_action import run_typed_callable_action
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


class ReportParams(ActionParams):
    source: int
    folder: str
    fail: bool = False


class Report(BaseModel):
    selected: int
    artifact: str


class AliasedReport(BaseModel):
    internal: str = Field(serialization_alias="public")


def aliased_report(params: ReportParams) -> AliasedReport:
    return AliasedReport(internal="published")


def _query(sheet):
    return {
        "schema_version": "frisket.query.v1",
        "kind": "sheet.filter",
        "scope": {"kind": "sheet", "sheet_id": sheet},
        "filter": {},
    }


def report(
    params: ReportParams,
    csv: SheetCsvExporter,
    context: InvocationContext,
    query: QueryPreviewer,
) -> Report:
    context.check_cancelled()
    selected = query.preview(query=_query(params.source), limit=500, offset=0)
    output = csv.write_sheet_csv(
        sheet_id=selected.sheet_id,
        path=str(Path(params.folder) / "report.csv"),
        query=selected.query,
        formula_policy="escape",
    )
    if params.fail:
        raise RuntimeError("author secret must not escape")
    return Report(selected=selected.row_count, artifact=output.path)


def _bound(handler, params, *, key="once"):
    definition = action(
        name="report",
        title="Report",
        description="Build a report.",
        category=ActionCategory.CONVERT,
        run=handler,
    )
    registry = ActionRegistry((ActionNamespace("custom", actions=(definition,)),))
    request = ActionRequest(
        action_id="custom.report",
        scope={"kind": "project"},
        params=params,
        idempotency_key=key,
    )
    return BoundTypedActionRequest.bind(
        registry.get("custom.report"), request
    ), registry


@pytest.fixture
def source(tmp_path):
    project = Project.create(tmp_path / "project")
    sheet = project.add_sheet("Data")
    column = project.add_column(sheet, "name")
    rows = project.add_rows(
        sheet, [{"name": "=unsafe"}, {"name": "Grace"}], {"name": column}
    )
    yield project, {"source": sheet, "folder": str(tmp_path)}, rows
    project.close()


def test_report_uses_shared_dispatch_declared_value_and_actual_observations(
    source, monkeypatch
):
    from frisket.actions import system

    project, params, rows = source
    bound, registry = _bound(report, params)
    monkeypatch.setattr(system, "ACTION_REGISTRY", registry)
    entry = bound.action.catalog_entry()
    assert entry["output_schema"]["properties"].keys() == {"selected", "artifact"}
    assert set(entry["required_capabilities"]) == {"project:read", "project:write"}
    assert entry["writes_project"] is True
    result = run_action_spec(
        project, bound.request.model_dump(mode="json"), project_id="p"
    )
    assert result.status == "completed", result.errors
    path = Path(params["folder"]) / "report.csv"
    assert result.value == {"selected": 2, "artifact": str(path)}
    assert "'=unsafe" in path.read_text()
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt.value == result.value
    assert bound.params.model_dump() == {**params, "fail": False}
    assert result.outputs[0].kind == "query_preview"
    assert result.outputs[0].row_ids == rows
    assert "query" not in result.outputs[0].ref
    assert len(receipt.exports) == 1
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "query_preview_rowset",
        "export_artifact",
    }
    replay = run_action_spec(
        project, bound.request.model_dump(mode="json"), project_id="p"
    )
    assert replay == result


def test_later_failure_keeps_completed_artifact_and_replays_without_handler(source):
    project, params, _ = source
    bound, _ = _bound(report, {**params, "fail": True})
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "failed"
    assert result.receipt_id and result.value is None
    assert result.errors[0].code == "action_failed"
    assert "secret" not in result.model_dump_json()
    path = Path(params["folder"]) / "report.csv"
    assert path.exists()
    assert len(result.outputs) == 2
    assert run_typed_callable_action(project, "p", bound) == result
    path.write_text("tampered")
    replay = run_typed_callable_action(project, "p", bound)
    assert replay.errors[0].code == "export_artifact_mismatch"


def test_reservation_precedes_handler_and_interrupted_work_is_not_repeated(source):
    project, params, _ = source
    calls = []

    def interrupted(params: ReportParams, query: QueryPreviewer) -> None:
        calls.append(True)
        assert (
            project.db.execute("SELECT status FROM receipts").fetchone()[0] == "running"
        )
        raise KeyboardInterrupt()

    bound, _ = _bound(interrupted, params)
    with pytest.raises(KeyboardInterrupt):
        run_typed_callable_action(project, "p", bound)
    project.db.execute("UPDATE receipts SET created_at='2000-01-01T00:00:00+00:00'")
    project.db.commit()
    result = run_typed_callable_action(project, "p", bound)
    assert result.errors[0].code == "idempotency_in_progress"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "returned", [{"selected": 2, "artifact": "untrusted"}, float("nan"), object()]
)
def test_invalid_return_fails_but_does_not_undo_completed_csv(source, returned):
    project, params, _ = source

    def invalid(params: ReportParams, csv: SheetCsvExporter) -> float:
        csv.write_sheet_csv(
            sheet_id=params.source,
            path=str(Path(params.folder) / "saved.csv"),
            query=None,
            formula_policy="raw",
        )
        return returned

    bound, _ = _bound(invalid, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "failed"
    assert result.errors[0].code == "invalid_action_result"
    assert (Path(params["folder"]) / "saved.csv").exists()
    assert len(result.outputs) == 1


def test_none_context_and_cancellation_after_completed_csv(source):
    project, params, _ = source
    cancelled = False

    def finish(
        params: ReportParams, csv: SheetCsvExporter, ctx: InvocationContext
    ) -> None:
        nonlocal cancelled
        csv.write_sheet_csv(
            sheet_id=params.source,
            path=str(Path(params.folder) / "saved.csv"),
            query=None,
            formula_policy="raw",
        )
        cancelled = True
        ctx.check_cancelled()

    bound, _ = _bound(finish, params)
    result = run_typed_callable_action(
        project, "p", bound, deps=ExecutorDeps(cancelled=lambda: cancelled)
    )
    assert result.status == "cancelled"
    assert result.value is None and len(result.outputs) == 1
    assert ReceiptStore(project).parsed_by_id(result.receipt_id).status == "cancelled"


def test_repeated_destination_keeps_latest_output_and_historical_facts(source):
    project, params, _ = source

    def repeated(params: ReportParams, csv: SheetCsvExporter) -> None:
        for policy in ("escape", "raw"):
            csv.write_sheet_csv(
                sheet_id=params.source,
                path=str(Path(params.folder) / "twice.csv"),
                query=None,
                formula_policy=policy,
            )

    bound, _ = _bound(repeated, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "completed", result.errors
    assert result.value is None
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert len(receipt.exports) == len(result.outputs) == 1
    artifacts = [
        item.ref for item in receipt.evidence if item.ref["kind"] == "export_artifact"
    ]
    assert len(artifacts) == 2 and artifacts[0]["sha256"] != artifacts[1]["sha256"]
    assert receipt.exports[0]["sha256"] == artifacts[-1]["sha256"]
    assert run_typed_callable_action(project, "p", bound) == result


def test_sibling_exporters_use_actual_calls_and_compose_with_replay(source):
    project, params, _ = source

    def sibling_report(
        params: ReportParams,
        work_log: WorkLogExporter,
        jsonl: SheetJsonlExporter,
        parquet: SheetParquetExporter,
    ) -> list[str]:
        work = work_log.write_work_log(
            path=str(Path(params.folder) / "custom.md"), include_receipts=False
        )
        lines = jsonl.write_sheet_jsonl(
            sheet_id=params.source,
            path=str(Path(params.folder) / "custom.jsonl"),
            query=None,
        )
        table = parquet.write_sheet_parquet(
            sheet_id=params.source,
            path=str(Path(params.folder) / "custom.parquet"),
            query=None,
        )
        return [work.format, lines.format, table.format]

    bound, _ = _bound(sibling_report, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "completed", result.errors
    assert result.value == ["markdown", "jsonl", "parquet"]
    assert {Path(item.ref["path"]).suffix for item in result.outputs} == {
        ".md",
        ".jsonl",
        ".parquet",
    }
    assert len(ReceiptStore(project).parsed_by_id(result.receipt_id).exports) == 3
    assert run_typed_callable_action(project, "p", bound) == result


def test_repeated_jsonl_replaces_current_artifact_but_keeps_history(source):
    project, params, _ = source

    def repeated(params: ReportParams, exporter: SheetJsonlExporter) -> None:
        for query in (None, _query(params.source)):
            exporter.write_sheet_jsonl(
                sheet_id=params.source,
                path=str(Path(params.folder) / "twice.jsonl"),
                query=query,
            )

    bound, _ = _bound(repeated, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "completed", result.errors
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert len(result.outputs) == len(receipt.exports) == 1
    assert (
        len(
            [
                item
                for item in receipt.evidence
                if item.ref.get("kind") == "export_artifact"
            ]
        )
        == 2
    )


def test_later_sibling_failure_preserves_prior_export_and_replays(source):
    project, params, _ = source

    def partial(
        params: ReportParams,
        work_log: WorkLogExporter,
        jsonl: SheetJsonlExporter,
    ) -> None:
        work_log.write_work_log(
            path=str(Path(params.folder) / "kept.md"), include_receipts=False
        )
        jsonl.write_sheet_jsonl(
            sheet_id=999_999,
            path=str(Path(params.folder) / "never.jsonl"),
            query=None,
        )

    bound, _ = _bound(partial, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "failed"
    assert result.errors[0].code == "invalid_sheet_ref"
    assert len(result.outputs) == 1
    assert (Path(params["folder"]) / "kept.md").exists()
    assert not (Path(params["folder"]) / "never.jsonl").exists()
    assert run_typed_callable_action(project, "p", bound) == result


def test_current_failed_delivery_restores_previous_completed_destination(
    source, monkeypatch
):
    from frisket.engine.executor.action_families import exports

    project, params, _ = source
    original = exports._deliver_export_artifact
    calls = 0

    def deliver(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("delivery failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(exports, "_deliver_export_artifact", deliver)

    def repeated(params: ReportParams, csv: SheetCsvExporter) -> None:
        for policy in ("escape", "raw"):
            csv.write_sheet_csv(
                sheet_id=params.source,
                path=str(Path(params.folder) / "twice.csv"),
                query=None,
                formula_policy=policy,
            )

    bound, _ = _bound(repeated, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "failed" and len(result.outputs) == 1
    assert "'=unsafe" in (Path(params["folder"]) / "twice.csv").read_text()
    assert run_typed_callable_action(project, "p", bound) == result
    assert not list(Path(params["folder"]).glob(".*.tmp"))


def test_handler_mutation_cannot_rewrite_captured_query_facts(source):
    project, params, rows = source

    def mutate(params: ReportParams, query: QueryPreviewer) -> None:
        value = query.preview(query=_query(params.source), limit=500, offset=0)
        value.query["scope"]["sheet_id"] = 999
        value.row_ids.clear()

    bound, _ = _bound(mutate, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "completed"
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert (
        next(item.ref for item in receipt.inputs if item.name == "query")["query"][
            "scope"
        ]["sheet_id"]
        == params["source"]
    )
    assert result.outputs[0].row_ids == rows


def test_cancellation_at_csv_commit_rolls_back_only_current_primitive(
    source, monkeypatch
):
    from frisket.engine.executor.action_families import exports

    project, params, _ = source
    cancelled = False
    original = exports._export_sheet_csv_receipt

    def cancel_before_commit(*args, **kwargs):
        nonlocal cancelled
        observed = original(*args, **kwargs)
        cancelled = True
        return observed

    monkeypatch.setattr(exports, "_export_sheet_csv_receipt", cancel_before_commit)
    bound, _ = _bound(report, params)
    result = run_typed_callable_action(
        project, "p", bound, deps=ExecutorDeps(cancelled=lambda: cancelled)
    )
    assert result.status == "cancelled", result.errors
    assert [output.kind for output in result.outputs] == ["query_preview"]
    assert not (Path(params["folder"]) / "report.csv").exists()
    assert not project.db.in_transaction
    assert ReceiptStore(project).parsed_by_id(result.receipt_id).status == "cancelled"


def test_query_repeats_use_actual_arguments_and_existing_500_row_bound(source):
    project, params, rows = source

    def query_twice(params: ReportParams, query: QueryPreviewer) -> list[int]:
        first = query.preview(query=_query(params.source), limit=1, offset=0)
        last = query.preview(query=_query(params.source), limit=999, offset=1)
        return [first.row_count, last.row_count]

    bound, _ = _bound(query_twice, params)
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "completed", result.errors
    assert result.value == [1, 1]
    assert result.outputs[0].row_ids == rows[:1]
    assert result.outputs[1].row_ids == rows[1:]
    assert result.outputs[1].ref["limit"] == 500


def test_params_only_and_context_only_share_the_reserved_contract(source):
    project, params, _ = source

    def pure(params: ReportParams) -> str:
        return "done"

    def context_only(params: ReportParams, ctx: InvocationContext) -> None:
        ctx.check_cancelled()

    for i, handler in enumerate((pure, context_only)):
        bound, _ = _bound(handler, params, key=f"pure-{i}")
        entry = bound.action.catalog_entry()
        assert entry["side_effects"] == ["write_receipt"]
        assert entry["required_capabilities"] == ["project:read"]
        result = run_typed_callable_action(project, "p", bound)
        assert result.status == "completed", result.errors
        assert result.receipt_id and result.outputs == []
        assert result.value == ("done" if i == 0 else None)


def test_declared_serialization_schema_matches_durable_value(source):
    project, params, _ = source
    bound, _ = _bound(aliased_report, params)
    schema = bound.action.catalog_entry()["output_schema"]
    assert set(schema["properties"]) == {"public"}
    result = run_typed_callable_action(project, "p", bound)
    assert result.status == "completed"
    assert result.value == {"public": "published"}
    assert ReceiptStore(project).parsed_by_id(result.receipt_id).value == result.value


def test_unsupported_specialized_combinations_refuse_before_handler(source):
    from frisket.actions.types import SourceCreator

    project, params, _ = source
    calls = []

    # Local annotations are supplied explicitly so registration tests the
    # executable contract, not forward-reference lookup.
    def unsupported(params, query, source):
        calls.append(True)

    unsupported.__annotations__ = {
        "params": ReportParams,
        "query": QueryPreviewer,
        "source": SourceCreator,
        "return": type(None),
    }
    with pytest.raises(TypeError, match="composed callable execution"):
        _bound(unsupported, params)
    assert calls == []
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
