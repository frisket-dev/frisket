from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

from pathlib import Path
from typing import Any

import pytest

from frisket.engine.store.media_blobs import media_cell
from frisket.engine.executor.plugin_write_apply import (
    WritePlanValidationError,
    apply_write_plan,
)
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import resolve_timeline
from frisket.authoring.workbench.plugin_subprocess_importers import (
    _parse_importer_row,
)


def _seed_project(tmp_path: Path) -> tuple[Project, int, int, dict[str, Any]]:
    project = Project.create(tmp_path / "temporal-plugin-ingress.frisket")
    sheet_id = project.add_sheet("Media")
    media_column_id = project.add_column(sheet_id, "media", "video")
    blob_hash = project.add_blob(
        b"temporal plugin fixture",
        "fixture.mp4",
        "video/mp4",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 10.0, "kind": "video"}
        ),
    )
    row_id = project.add_rows(
        sheet_id,
        [
            {
                "media": media_cell(
                    blob_hash,
                    mime="video/mp4",
                    filename="fixture.mp4",
                )
            }
        ],
        {"media": media_column_id},
    )[0]
    lease = resolve_timeline(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=media_column_id,
    )
    return project, sheet_id, row_id, lease.anchor.wire_value()


def _temporal_value(type_name: str, anchor: dict[str, Any]) -> dict[str, Any]:
    if type_name == "timeline_point":
        return {
            "schema_version": "frisket.timeline_point.v1",
            "timeline": anchor,
            "item": {"id": "point-1", "at_ms": 1_000},
        }
    if type_name == "timeline_points":
        return {
            "schema_version": "frisket.timeline_points.v1",
            "timeline": anchor,
            "items": [{"id": "point-1", "at_ms": 1_000}],
        }
    if type_name == "timeline_range":
        return {
            "schema_version": "frisket.timeline_range.v1",
            "timeline": anchor,
            "item": {"id": "range-1", "start_ms": 1_000, "end_ms": 2_000},
        }
    assert type_name == "timeline_ranges"
    return {
        "schema_version": "frisket.timeline_ranges.v1",
        "timeline": anchor,
        "items": [{"id": "range-1", "start_ms": 1_000, "end_ms": 2_000}],
    }


def _missing_anchor(anchor: dict[str, Any]) -> dict[str, Any]:
    return {
        **anchor,
        "artifact_stable_id": ("source_artifact:00000000-0000-4000-8000-000000000099"),
    }


def _stale_anchor(anchor: dict[str, Any]) -> dict[str, Any]:
    return {**anchor, "fingerprint": "sha256:" + "0" * 64}


def test_importer_accepts_valid_anchor_and_rejects_stale_anchor_without_writes(
    tmp_path: Path,
) -> None:
    project, _sheet_id, _row_id, anchor = _seed_project(tmp_path)
    try:
        valid_value = _temporal_value("timeline_ranges", anchor)
        valid = _parse_importer_row(
            {"selections": valid_value},
            project=project,
            column_types_by_name={"selections": "timeline_ranges"},
            row_number=1,
        )
        assert valid["selections"]["timeline"] == anchor
        assert valid["selections"]["items"][0]["id"] == "range-1"

        before_rows = project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
        invalid = _parse_importer_row(
            {"selections": _temporal_value("timeline_ranges", _stale_anchor(anchor))},
            project=project,
            column_types_by_name={"selections": "timeline_ranges"},
            row_number=2,
        )
        assert invalid["_error"]["status"] == "failed"
        error = invalid["_error"]["errors"][0]
        assert error["code"] == "plugin_importer_row_invalid"
        assert (
            error["details"]["diagnostics"][0]["attach"]["reason"] == "timeline_stale"
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == before_rows
        )
    finally:
        project.close()


def _temporal_write_plan(value: dict[str, Any], *, sheet_name: str) -> dict[str, Any]:
    return {
        "schema_version": "frisket.plugin_write_plan.v1",
        "ops": [
            {"op": "create_sheet", "sheet_ref": "clips", "name": sheet_name},
            {
                "op": "create_column",
                "sheet_ref": "clips",
                "column_ref": "selection",
                "name": "selection",
                "type": "timeline_range",
            },
            {
                "op": "append_rows",
                "sheet_ref": "clips",
                "rows": [{"selection": value}],
            },
        ],
    }


def test_plugin_write_plan_accepts_project_anchored_temporal_value(
    tmp_path: Path,
) -> None:
    project, _sheet_id, _row_id, anchor = _seed_project(tmp_path)
    try:
        receipt = apply_write_plan(
            project,
            _temporal_write_plan(
                _temporal_value("timeline_range", anchor),
                sheet_name="Valid temporal output",
            ),
            plugin_id="temporal-test",
            manifest_sha="sha256:" + "1" * 64,
            declared_capabilities=["plugin:project_writes"],
        )
        assert receipt.counts == {"sheets": 1, "columns": 1, "rows": 1}
        stored = project.db.execute(
            "SELECT cells.value FROM cells "
            "JOIN columns ON columns.id=cells.column_id "
            "JOIN sheets ON sheets.id=columns.sheet_id "
            "WHERE sheets.name=? AND columns.name=?",
            ("Valid temporal output", "selection"),
        ).fetchone()
        assert stored is not None
    finally:
        project.close()


@pytest.mark.parametrize(
    ("bad_anchor", "reason"),
    [(_missing_anchor, "timeline_not_found"), (_stale_anchor, "timeline_stale")],
)
def test_plugin_write_plan_rejects_invalid_temporal_anchor_with_zero_state(
    tmp_path: Path,
    bad_anchor: Any,
    reason: str,
) -> None:
    project, _sheet_id, _row_id, anchor = _seed_project(tmp_path)
    try:
        before = {
            table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sheets", "columns", "rows", "cells", "ops")
        }
        with pytest.raises(WritePlanValidationError, match=reason):
            apply_write_plan(
                project,
                _temporal_write_plan(
                    _temporal_value("timeline_range", bad_anchor(anchor)),
                    sheet_name="Rejected temporal output",
                ),
                plugin_id="temporal-test",
                manifest_sha="sha256:" + "1" * 64,
                declared_capabilities=["plugin:project_writes"],
            )
        after = {
            table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sheets", "columns", "rows", "cells", "ops")
        }
        assert after == before
    finally:
        project.close()
