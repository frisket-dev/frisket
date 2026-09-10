from __future__ import annotations

import pytest

from frisket.engine.store import Project
from frisket.engine.executor import run_action_spec
from frisket.ops.integrations import natural_pdf


@pytest.mark.parametrize(
    ("action_id", "column_type", "accepted_types"),
    [
        ("media.ocr", "image", ["image", "file"]),
        ("media.transcribe", "audio", ["audio", "video", "file"]),
        ("media.fetch_url", "link", ["link", "text"]),
        ("media.ytdlp_download", "link", ["link", "text"]),
        ("media.ytdlp_download", "text", ["link", "text"]),
        ("web.capture_screenshot", "link", ["text", "link"]),
        ("media.video_frames", "video", ["video", "file"]),
        ("media.extract_faces", "image", ["image", "file"]),
        ("media.to_markdown", "file", ["file", "text"]),
    ],
)
@pytest.mark.parametrize("failure", ("sheet", "column", "type", "rows", "empty"))
def test_typed_media_input_admission_precedes_execution(
    tmp_path, monkeypatch, action_id, column_type, accepted_types, failure
):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid or empty inputs must not dispatch a media download")

    monkeypatch.setattr("frisket.ops.ytdlp.download_media", forbidden)
    project = Project.create(tmp_path / "typed-media-inputs.frisket")
    try:
        sheet = project.add_sheet("Media")
        name = "missing" if failure == "column" else "source"
        columns = {}
        if failure != "column":
            columns[name] = project.add_column(
                sheet, name, type="boolean" if failure == "type" else column_type
            )
        rows = project.add_rows(sheet, [{}], columns) if failure == "rows" else []
        result = run_action_spec(
            project,
            {
                "action_id": action_id,
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": 999_999 if failure == "sheet" else sheet,
                    "row_ids": [*rows, 999_999] if failure == "rows" else None,
                },
                "params": {"source": name},
                "idempotency_key": f"input-{failure}",
            },
            project_id="p",
        )
        if failure == "empty":
            # Typed media follows the common row host: an empty local/free run
            # is a no-op; unknown-cost fetching still requires its normal consent.
            assert result.status == (
                "needs_confirmation"
                if action_id in {"media.fetch_url", "media.ytdlp_download"}
                else "completed"
            ), result.errors
            assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
            assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
            return
        assert result.status == "failed", result.errors
        assert result.errors[0].code == "invalid_input_ref", result.errors
        assert result.errors[0].action_kind == action_id
        assert {
            table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("runs", "blobs", "results")
        } == {"runs": 0, "blobs": 0, "results": 0}
        if failure == "column":
            assert result.errors[0].details == {"columns": [name]}
        elif failure == "type":
            assert result.errors[0].details == {
                "columns": [
                    {
                        "name": name,
                        "actual_type": "boolean",
                        "accepted_column_types": accepted_types,
                    }
                ]
            }
    finally:
        project.close()


@pytest.mark.parametrize("failure", ("sheet", "column", "type", "rows", "empty"))
def test_typed_pdf_input_refusals_precede_reader_dispatch(
    tmp_path, monkeypatch, failure
):
    project = Project.create(tmp_path / "pdf-input-resolution.frisket")

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid inputs must not reach PDF extraction")

    monkeypatch.setattr(natural_pdf, "extract_pdf_tables", forbidden)
    try:
        sheet_id = project.add_sheet("Media")
        input_name = "missing" if failure == "column" else "source"
        columns = {}
        if failure != "column":
            columns[input_name] = project.add_column(
                sheet_id, input_name, type="boolean" if failure == "type" else "file"
            )
        row_ids = project.add_rows(sheet_id, [{}], columns) if failure == "rows" else []
        result = run_action_spec(
            project,
            {
                "action_id": "media.extract_pdf_tables",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": 999_999 if failure == "sheet" else sheet_id,
                    "row_ids": [*row_ids, 999_999] if failure == "rows" else None,
                },
                "params": {"source": input_name},
                "idempotency_key": f"pdf-{failure}",
            },
            project_id="p",
        )
        assert result.status == "failed", result.errors
        assert result.errors[0].code == (
            "pdf_table_extract_failed" if failure == "empty" else "invalid_input_ref"
        ), result.errors
        assert result.errors[0].action_kind == "media.extract_pdf_tables"
        assert all(output.kind != "named_result" for output in result.outputs)
        if failure == "column":
            assert result.errors[0].details == {"columns": [input_name]}
        elif failure == "type":
            assert result.errors[0].details == {
                "columns": [
                    {
                        "name": input_name,
                        "actual_type": "boolean",
                        "accepted_column_types": ["file"],
                    }
                ]
            }
    finally:
        project.close()
