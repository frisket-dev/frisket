from __future__ import annotations

import asyncio
from pathlib import Path

from frisket.contracts.action import ActionError
from frisket.engine.executor.actions import resolve_map_preview, run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.server.services.sheet_grid import _sheet_data_payload
from test_result_generation_store import (
    _declare,
    _release,
    _seal,
    _start_claimed_run,
)


def _run_clean_dates(
    project: Project, sheet_id: int, *, source: str, output: str, key: str
):
    return run_action_spec(
        project,
        {
            "action_id": "map.clean_dates",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"source": source, "format": "%m/%d/%y"},
            "output_names": {"cleaned": output},
            "idempotency_key": key,
        },
        project_id="clean-dates-invalid-source",
    )


def _result_rows(project: Project, run_id: int, column_id: int):
    return [
        dict(row)
        for row in project.db.execute(
            "SELECT row_id,value_kind,value,confidence,justification,"
            "publication_effect FROM results WHERE run_id=? AND column_id=? "
            "ORDER BY row_id",
            (run_id, column_id),
        )
    ]


def test_clean_dates_repairs_recoverable_invalid_generated_date_values(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "clean-generated-dates.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        text_id = project.add_column(sheet_id, "text_control", type="text")
        date_id = project.add_column(
            sheet_id,
            "extracted_date",
            type="date",
            ai_generated=True,
        )
        row_ids = project.add_rows(
            sheet_id,
            [
                {"text_control": "09/08/26"},
                {"text_control": None},
                {"text_control": "not a date"},
            ],
            {"text_control": text_id},
        )

        producer = _start_claimed_run(
            project,
            sheet_id=sheet_id,
            output_column_id=date_id,
            row_ids=row_ids,
            label="generated date fixture",
        )
        generations = ResultGenerationStore(project)
        _declare(
            generations,
            producer,
            date_id,
            write_mode="create",
            compatibility_key="sha256:generated-date-fixture-v1",
        )
        RunResultStore(project).write_results(
            producer.run_id,
            [
                {
                    "row_id": row_ids[0],
                    "column_id": date_id,
                    "value": "09/08/26",
                    "publication_effect": "publish_value",
                },
                {
                    "row_id": row_ids[1],
                    "column_id": date_id,
                    "value": None,
                    "publication_effect": "publish_null",
                },
                {
                    "row_id": row_ids[2],
                    "column_id": date_id,
                    "value": "not a date",
                    "publication_effect": "publish_value",
                },
            ],
            project_heads=False,
            **producer.authority.kwargs(),
        )
        _seal(generations, producer, date_id)
        _release(project, producer)

        assert project.get_values(sheet_id, date_id) == dict.fromkeys(row_ids)
        assert project.get_values(sheet_id, date_id, preserve_invalid=True) == {
            row_ids[0]: "09/08/26",
            row_ids[1]: None,
            row_ids[2]: "not a date",
        }
        payload = _sheet_data_payload(
            project,
            sheet_id,
            project.columns(sheet_id),
            row_ids,
            total=len(row_ids),
            value_mode="bounded",
        )
        for index, raw in ((0, "09/08/26"), (2, "not a date")):
            assert payload["rows"][index]["cells"][str(date_id)] == raw
            assert payload["rows"][index]["meta"][str(date_id)]["invalid"] is True
        assert "invalid" not in payload["rows"][1]["meta"][str(date_id)]

        preview_plan = resolve_map_preview(
            project,
            {
                "action_id": "map.clean_dates",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": row_ids,
                },
                "params": {"source": "extracted_date", "format": "%m/%d/%y"},
                "output_names": {"cleaned": "cleaned_date_preview"},
                "idempotency_key": "preview-clean-generated-date",
            },
        )
        assert not isinstance(preview_plan, ActionError), preview_plan
        preview = asyncio.run(
            preview_plan.make_runner().preview(
                preview_plan.runner_spec, program=preview_plan.program
            )
        )
        assert preview.values[row_ids[0]]["cleaned_date_preview"]["value"] == (
            "2026-09-08"
        )
        assert preview.values[row_ids[1]]["cleaned_date_preview"]["value"] is None
        assert preview.values[row_ids[2]]["cleaned_date_preview"]["value"] is None

        date_result = _run_clean_dates(
            project,
            sheet_id,
            source="extracted_date",
            output="cleaned_date",
            key="clean-generated-date",
        )
        text_result = _run_clean_dates(
            project,
            sheet_id,
            source="text_control",
            output="cleaned_text_control",
            key="clean-text-control",
        )
        assert date_result.status == text_result.status == "completed"

        date_output_id = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='cleaned_date'",
                (sheet_id,),
            ).fetchone()["id"]
        )
        text_output_id = int(
            project.db.execute(
                "SELECT id FROM columns "
                "WHERE sheet_id=? AND name='cleaned_text_control'",
                (sheet_id,),
            ).fetchone()["id"]
        )
        expected_values = {
            row_ids[0]: "2026-09-08",
            row_ids[1]: None,
            row_ids[2]: None,
        }
        assert project.get_values(sheet_id, date_output_id) == expected_values
        assert project.get_values(sheet_id, text_output_id) == expected_values

        date_rows = _result_rows(project, int(date_result.run_id), date_output_id)
        text_rows = _result_rows(project, int(text_result.run_id), text_output_id)
        assert date_rows == text_rows
        assert date_rows[0] == {
            "row_id": row_ids[0],
            "value_kind": "text",
            "value": "2026-09-08",
            "confidence": 1.0,
            "justification": None,
            "publication_effect": "publish_value",
        }
        assert date_rows[1] == {
            "row_id": row_ids[1],
            "value_kind": "null",
            "value": None,
            "confidence": None,
            "justification": None,
            "publication_effect": "publish_null",
        }
        assert date_rows[2] == {
            "row_id": row_ids[2],
            "value_kind": "null",
            "value": None,
            "confidence": 0.0,
            "justification": (
                "could not parse 'not a date' did not match the format '%m/%d/%y' "
                "(original kept in the source column)"
            ),
            "publication_effect": "publish_null",
        }
    finally:
        project.close()
