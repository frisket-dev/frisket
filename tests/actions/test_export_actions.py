from __future__ import annotations

from dataclasses import fields
from typing import get_type_hints

import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import root_action_catalog, validate_root_action
from frisket.actions.types import (
    SheetCsvExporter,
    SheetJsonlExporter,
    SheetParquetExporter,
    WorkLogExporter,
)


_EXPORTS = {
    "export.work_log": (
        WorkLogExporter,
        "work_log_export",
        None,
        {"format", "path", "byte_count", "sha256"},
    ),
    "export.sheet_csv": (
        SheetCsvExporter,
        "sheet_csv_export",
        "csv",
        {
            "sheet_id",
            "format",
            "path",
            "byte_count",
            "sha256",
            "row_count",
            "row_ids",
            "query_hash",
        },
    ),
    "export.sheet_jsonl": (
        SheetJsonlExporter,
        "sheet_jsonl_export",
        "jsonl",
        {
            "sheet_id",
            "format",
            "path",
            "byte_count",
            "sha256",
            "row_count",
            "row_ids",
            "query_hash",
        },
    ),
    "export.sheet_parquet": (
        SheetParquetExporter,
        "sheet_parquet_export",
        "parquet",
        {
            "sheet_id",
            "format",
            "path",
            "byte_count",
            "sha256",
            "row_count",
            "row_ids",
            "query_hash",
        },
    ),
}


def test_local_exports_have_four_narrow_typed_capabilities() -> None:
    capabilities = []
    for action_id, (capability, _form, _target, result_fields) in _EXPORTS.items():
        run = ACTION_REGISTRY.get(action_id).definition.run
        assert run.capabilities == (capability,)
        assert {
            field.name for field in fields(get_type_hints(run.handler)["return"])
        } == result_fields
        capabilities.append(run.single_capability())

    assert len(set(capabilities)) == 4
    assert [
        name
        for capability in capabilities
        for name in capability.__dict__
        if name.startswith("write_")
    ] == [
        "write_work_log",
        "write_sheet_csv",
        "write_sheet_jsonl",
        "write_sheet_parquet",
    ]


def test_local_export_catalog_preserves_forms_targets_and_contracts() -> None:
    catalog = {entry.kind: entry for entry in root_action_catalog().actions}
    for action_id, (_capability, form, target, result_fields) in _EXPORTS.items():
        entry = catalog[action_id]
        assert entry.required_capabilities == ["project:read", "project:write"]
        assert entry.ui_hints["form"] == form
        assert (
            entry.input_schema
            == ACTION_REGISTRY.get(
                action_id
            ).definition.run.params_model.model_json_schema()
        )
        assert "LocalFileDestination" in entry.input_schema["$defs"]
        if target is None:
            assert "export_target" not in entry.ui_hints
        else:
            export_target = entry.ui_hints["export_target"]
            assert export_target == {
                "surface": "project.export",
                "label": target.upper() if target != "parquet" else "Parquet",
                "destination_kind": target,
                "form": form,
                "source_modes": ["current_sheet", "current_view"],
                "destination_modes": ["download"],
            }
        assert set(entry.output_schema["properties"]) == result_fields
        assert entry.output_schema["additionalProperties"] is False
        assert (
            entry.output_schema["properties"]["format"]["const"]
            == {
                "export.work_log": "markdown",
                "export.sheet_csv": "csv",
                "export.sheet_jsonl": "jsonl",
                "export.sheet_parquet": "parquet",
            }[action_id]
        )


def test_local_export_catalog_preserves_per_format_errors() -> None:
    catalog = {entry.kind: entry for entry in root_action_catalog().actions}
    errors = {kind: {item.code for item in catalog[kind].errors} for kind in _EXPORTS}

    csv_schema = catalog["export.sheet_csv"].output_schema
    assert csv_schema["properties"]["format"] == {
        "const": "csv",
        "title": "Format",
        "type": "string",
    }
    assert csv_schema["additionalProperties"] is False
    assert "receipt_id" not in csv_schema["properties"]

    assert "export_column_collision" not in errors["export.sheet_csv"]
    assert "export_column_collision" in errors["export.sheet_jsonl"]
    assert {"parquet_writer_unavailable", "parquet_render_failed"} <= errors[
        "export.sheet_parquet"
    ]
    callable_errors = {
        "action_cancelled",
        "action_failed",
        "idempotency_in_progress",
        "invalid_action_result",
    }
    assert errors["export.work_log"] == callable_errors | {
        "invalid_action_request",
        "invalid_params",
        "invalid_export_destination",
        "idempotency_conflict",
        "export_artifact_missing",
        "export_artifact_mismatch",
        "project_write_failed",
    }
    assert errors["export.sheet_csv"] == errors["export.work_log"] | {
        "invalid_sheet_ref",
        "invalid_query_spec",
        "invalid_query_filter",
        "export_rowset_too_large",
    }
    assert errors["export.sheet_jsonl"] == errors["export.sheet_csv"] | {
        "export_column_collision"
    }
    assert errors["export.sheet_parquet"] == errors["export.sheet_jsonl"] | {
        "parquet_writer_unavailable",
        "parquet_render_failed",
    }


def test_export_format_is_derived_from_the_action_identity() -> None:
    request = {
        "action_id": "export.sheet_jsonl",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": 1,
            "format": "csv",
            "destination": {"kind": "local_file", "path": "rows.jsonl"},
        },
        "idempotency_key": "catalog-format-authority@sha256:v1",
    }

    validation = validate_root_action(request)

    assert validation.ok is False
    assert validation.error is not None
    assert validation.error.code == "invalid_action_request"
    catalog = {entry.kind: entry for entry in root_action_catalog().actions}
    advertised = {error.code for error in catalog["export.sheet_jsonl"].errors}
    assert validation.error.code in advertised
    assert "unsupported_export_format" not in advertised


@pytest.mark.parametrize(
    ("action_id", "missing_sheet"),
    [
        ("export.work_log", False),
        ("export.sheet_csv", False),
        ("export.sheet_csv", True),
    ],
)
def test_export_runtime_refusal_only_records_its_failed_invocation(
    tmp_path, action_id, missing_sheet
) -> None:
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "project")
    try:
        sheet_id = project.add_sheet("Data")
        tables = (
            "sheets",
            "columns",
            "rows",
            "cells",
            "ops",
            "runs",
            "blobs",
            "receipts",
        )
        before = {
            table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        destination = tmp_path / "missing-sheet.csv" if missing_sheet else tmp_path
        params = {"destination": {"kind": "local_file", "path": str(destination)}}
        if action_id == "export.sheet_csv":
            params["sheet_id"] = 999 if missing_sheet else sheet_id
        result = run_action_spec(
            project,
            {
                "action_id": action_id,
                "scope": {"kind": "project"},
                "params": params,
                "idempotency_key": "failed-export",
            },
            project_id="p",
        )
        assert result.status == "failed"
        assert result.errors[0].code == (
            "invalid_sheet_ref" if missing_sheet else "invalid_export_destination"
        )
        assert result.receipt_id is not None
        assert (
            project.db.execute(
                "SELECT status FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()[0]
            == "failed"
        )
        assert {
            table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        } == {**before, "receipts": before["receipts"] + 1}
        if missing_sheet:
            assert not destination.exists()
    finally:
        project.close()
