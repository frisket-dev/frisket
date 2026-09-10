from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionRequest, SheetRows
from frisket.actions.system import BoundTypedActionRequest
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.map_rows_action import (
    build_typed_map_rows_plan,
    run_typed_map_rows_action,
)
from frisket.engine.runner import MapRunner
from frisket.engine.runner.review import review_queue
from frisket.engine.store import Project
from frisket.execution.attempt_authority import UnroutedOnlyAuthority


CLEAN_COLUMN = ACTION_REGISTRY.get("map.clean_column")


@pytest.fixture
def project(tmp_path: Path):
    store = Project.create(tmp_path / "typed-clean-column.frisket")
    sheet_id = store.add_sheet("contacts")
    source_id = store.add_column(sheet_id, "agency", type="text")
    row_ids = store.add_rows(
        sheet_id,
        [
            {"agency": "  NEW YORK dept. of health "},
            {"agency": "New York Department of Health"},
            {"agency": "N/A"},
            {"agency": "REPORTER@EXAMPLE.COM"},
        ],
        {"agency": source_id},
    )
    yield store, sheet_id, source_id, row_ids
    store.close()


def _runner(project: Project, router=None) -> MapRunner:
    return MapRunner(
        project,
        router or ModelRouter(cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(project),
    )


def test_clean_column_batch_persists_canonical_values_without_touching_source(
    project,
) -> None:
    store, sheet_id, source_id, row_ids = project
    request = ActionRequest(
        action_id="map.clean_column",
        scope=SheetRows(sheet_id=sheet_id, row_ids=tuple(row_ids)),
        params={"source": "agency"},
        output_names={"cleaned": "agency_clean"},
        idempotency_key="clean-column@1",
    )

    result = run_typed_map_rows_action(
        store,
        "project-1",
        BoundTypedActionRequest.bind(CLEAN_COLUMN, request),
        None,
        _runner,
    )

    assert result.status == "completed", result.errors
    output_id = result.outputs[0].column_id
    assert output_id is not None
    assert store.get_values(sheet_id, output_id, row_ids=row_ids) == {
        row_ids[0]: "New York Department of Health",
        row_ids[1]: "New York Department of Health",
        row_ids[2]: None,
        row_ids[3]: "reporter@example.com",
    }
    assert store.get_values(sheet_id, source_id, row_ids=row_ids)[row_ids[0]] == (
        "  NEW YORK dept. of health "
    )
    assert review_queue(store, sheet_id=sheet_id) == []


def test_clean_column_numeric_batch_records_row_local_failure(tmp_path: Path) -> None:
    store = Project.create(tmp_path / "typed-clean-number.frisket")
    try:
        sheet_id = store.add_sheet("amounts")
        source_id = store.add_column(sheet_id, "amount", type="text")
        row_ids = store.add_rows(
            sheet_id,
            [{"amount": "$1,299.00"}, {"amount": "ambiguous"}, {"amount": "N/A"}],
            {"amount": source_id},
        )
        request = ActionRequest(
            action_id="map.clean_column",
            scope=SheetRows(sheet_id=sheet_id, row_ids=tuple(row_ids)),
            params={
                "source": "amount",
                "make_numeric": True,
                "remove_thousands_separators": True,
            },
            idempotency_key="clean-number@1",
        )

        result = run_typed_map_rows_action(
            store,
            "project-1",
            BoundTypedActionRequest.bind(CLEAN_COLUMN, request),
            None,
            _runner,
        )

        assert result.status == "partial", result.errors
        output = next(
            column for column in store.columns(sheet_id) if column["name"] == "cleaned"
        )
        assert output["type"] == "number"
        assert store.get_values(sheet_id, output["id"], row_ids=row_ids) == {
            row_ids[0]: 1299.0,
            row_ids[1]: None,
            row_ids[2]: None,
        }
        failure = store.db.execute(
            "SELECT error_code, error FROM results "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            (result.run_id, row_ids[1], output["id"]),
        ).fetchone()
        assert failure["error_code"] == "invalid_numeric_value"
        assert failure["error"] == "The source value could not be parsed as a number."
    finally:
        store.close()


def test_clean_column_batch_preview_is_global_and_write_free(project) -> None:
    store, sheet_id, _source_id, row_ids = project
    request = ActionRequest(
        action_id="map.clean_column",
        scope=SheetRows(sheet_id=sheet_id, row_ids=tuple(row_ids)),
        params={"source": "agency"},
        idempotency_key="clean-column@preview",
    )
    plan = build_typed_map_rows_plan(
        store, BoundTypedActionRequest.bind(CLEAN_COLUMN, request)
    )
    before = [column["name"] for column in store.columns(sheet_id)]

    preview = asyncio.run(
        _runner(store).preview(plan.spec_dict(), program=plan.program)
    )

    assert preview.row_ids == row_ids
    assert preview.values[row_ids[0]]["cleaned"]["value"] == (
        "New York Department of Health"
    )
    assert [column["name"] for column in store.columns(sheet_id)] == before
