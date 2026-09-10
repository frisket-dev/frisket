from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest

from frisket.engine.executor import ExecutorDeps, ImportWorkloadLimits, run_action_spec
from frisket.engine.store import Project
from tests.engine.test_import_ndjson_executor import _import_ndjson_action
from tests.engine.test_temporal_persistence_ingress import _range_value, _seed_project


def _counts(project: Project) -> dict[str, int]:
    return {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("sheets", "columns", "rows", "cells", "ops", "receipts")
    }


def _action(path: Path, *, type_name: str = "text") -> dict:
    action = _import_ndjson_action(path)
    action["params"]["columns"] = [{"name": "value", "type": type_name}]
    return action


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_ndjson_uses_physical_lines_and_declared_encoding(
    tmp_path: Path, encoding: str
) -> None:
    path = tmp_path / "lines.ndjson"
    values = ["Café\u2028Town\u0085Hall", "Library"]
    path.write_bytes(
        (
            "\r\n  \r\n"
            + json.dumps({"value": values[0]}, ensure_ascii=False)
            + "\r"
            + json.dumps({"value": values[1]})
            + "\n\n"
        ).encode(encoding)
    )
    with closing(Project.create(tmp_path / "lines.frisket", name="Lines")) as project:
        action = _action(path)
        action["params"]["encoding"] = encoding
        result = run_action_spec(project, action, project_id="p")
        assert result.status == "completed", result.errors
        sheet_id = result.outputs[0].sheet_id
        (column,) = project.columns(sheet_id)
        assert list(project.get_values(sheet_id, column["id"]).values()) == values


@pytest.mark.parametrize(
    ("line", "type_name", "code", "column"),
    [
        ("{bad}", "text", "ndjson_parse_failed", None),
        ("[]", "text", "ndjson_parse_failed", None),
        ('{"extra": 1}', "text", "row_shape_mismatch", None),
        ('{"value": "ok", "extra": 1}', "text", "row_shape_mismatch", None),
        ('{"value": "high"}', "number", "invalid_ndjson_value", "value"),
        ('{"value": NaN}', "number", "invalid_ndjson_value", "value"),
        ('{"value": {"nested": Infinity}}', "json", "invalid_ndjson_value", "value"),
    ],
)
def test_ndjson_refusal_keeps_physical_line_and_column_without_partial_writes(
    tmp_path: Path, line: str, type_name: str, code: str, column: str | None
) -> None:
    path = tmp_path / "invalid.ndjson"
    path.write_text('\n{"value": null}\n  \n' + line + "\n")
    with closing(
        Project.create(tmp_path / "invalid.frisket", name="Invalid")
    ) as project:
        before = _counts(project)
        result = run_action_spec(
            project, _action(path, type_name=type_name), project_id="p"
        )
        assert result.status == "failed"
        error = result.errors[0]
        assert error.code == code
        assert error.details["line"] == 4
        if column is not None:
            assert error.details["column"] == column
        if code == "row_shape_mismatch":
            assert error.details["extra"] == ["extra"]
        assert _counts(project) == before


def test_ndjson_empty_file_publishes_declared_empty_schema(tmp_path: Path) -> None:
    path = tmp_path / "empty.ndjson"
    path.write_bytes(b"\n  \r\n")
    with closing(Project.create(tmp_path / "empty.frisket", name="Empty")) as project:
        result = run_action_spec(project, _action(path), project_id="p")
        assert result.status == "completed", result.errors
        assert project.row_count(result.outputs[0].sheet_id) == 0
        assert len(project.columns(result.outputs[0].sheet_id)) == 1


@pytest.mark.parametrize("encoding", ["utf-8", "not-a-real-codec"])
def test_ndjson_decode_failure_never_publishes(tmp_path: Path, encoding: str) -> None:
    path = tmp_path / "bad-encoding.ndjson"
    path.write_bytes(b'{"value": "\xff"}\n')
    with closing(Project.create(tmp_path / "decode.frisket", name="Decode")) as project:
        action = _action(path)
        action["params"]["encoding"] = encoding
        before = _counts(project)
        result = run_action_spec(project, action, project_id="p")
        assert result.status == "failed"
        assert result.errors[0].code == "ndjson_parse_failed"
        assert _counts(project) == before


def test_ndjson_preserves_file_values_descriptor_attributes_and_output_names(
    tmp_path: Path,
) -> None:
    path = tmp_path / "file.ndjson"
    path.write_text(json.dumps({"value": "https://example.com/report.pdf"}) + "\n")
    with closing(Project.create(tmp_path / "file.frisket", name="Files")) as project:
        action = _action(path, type_name="file")
        action["params"]["columns"][0].update(format="plain_text", hidden=True)
        action["output_names"] = {"value": "Document"}
        result = run_action_spec(project, action, project_id="p")
        assert result.status == "completed", result.errors
        (column,) = project.columns(result.outputs[0].sheet_id, include_hidden=True)
        assert column["name"] == "Document"
        assert column["type"] == "file"
        assert column["format"] == "plain_text"
        assert column["hidden"] == 1
        assert list(
            project.get_values(result.outputs[0].sheet_id, column["id"]).values()
        ) == ["https://example.com/report.pdf"]


@pytest.mark.parametrize(("max_rows", "row_count"), [(None, 10_001), (2, 2), (1, 2)])
def test_ndjson_row_budget_is_host_policy_and_never_partially_publishes(
    tmp_path: Path, max_rows: int | None, row_count: int
) -> None:
    path = tmp_path / "budget.ndjson"
    path.write_text("".join(json.dumps({"value": n}) + "\n" for n in range(row_count)))
    with closing(Project.create(tmp_path / "budget.frisket", name="Budget")) as project:
        before = _counts(project)
        result = run_action_spec(
            project,
            _action(path, type_name="integer"),
            project_id="p",
            deps=ExecutorDeps(
                import_workload_limits=ImportWorkloadLimits(max_rows=max_rows)
            ),
        )
        if max_rows is not None and row_count > max_rows:
            assert result.status == "failed"
            assert result.errors[0].code == "import_workload_limit_exceeded"
            assert _counts(project) == before
        else:
            assert result.status == "completed", result.errors
            assert project.row_count(result.outputs[0].sheet_id) == row_count


def test_ndjson_receipt_failure_rolls_back_sheet_rows_and_operation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollback.ndjson"
    path.write_text('{"value": "one"}\n{"value": "two"}\n')
    with closing(
        Project.create(tmp_path / "rollback.frisket", name="Rollback")
    ) as project:
        project.db.execute(
            "CREATE TEMP TRIGGER reject_receipt BEFORE INSERT ON receipts "
            "BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END"
        )
        project.db.commit()
        before = _counts(project)
        result = run_action_spec(project, _action(path), project_id="p")
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert _counts(project) == before


@pytest.mark.parametrize("missing_anchor", [False, True])
def test_ndjson_temporal_values_require_project_context(
    tmp_path: Path, missing_anchor: bool
) -> None:
    seeded = _seed_project(tmp_path, "ndjson-temporal")
    project = seeded["project"]
    try:
        anchor = seeded["lease"].anchor.wire_value()
        if missing_anchor:
            anchor["artifact_stable_id"] = (
                "source_artifact:00000000-0000-4000-8000-000000000011"
            )
        value = _range_value(anchor)
        path = tmp_path / "temporal.ndjson"
        path.write_text(json.dumps({"value": value}) + "\n")
        before = _counts(project)
        result = run_action_spec(
            project, _action(path, type_name="timeline_range"), project_id="p"
        )
        if missing_anchor:
            assert result.status == "failed"
            assert result.errors[0].code == "timeline_not_found"
            assert _counts(project) == before
        else:
            assert result.status == "completed", result.errors
            (column,) = project.columns(result.outputs[0].sheet_id)
            assert list(
                project.get_values(result.outputs[0].sheet_id, column["id"]).values()
            ) == [
                {
                    **value,
                    "item": {**value["item"], "label": None, "metadata": {}},
                }
            ]
    finally:
        project.close()
