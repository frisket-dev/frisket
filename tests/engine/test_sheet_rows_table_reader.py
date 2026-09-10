"""Native table producers can derive a child sheet from request-scoped rows."""

from __future__ import annotations

from contextlib import closing

import pytest
from pydantic import BaseModel

from frisket.actions.core import ActionCategory, RegisteredAction, action, create_sheet
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    ColumnRef,
    RowSource,
    SheetRows,
    SheetRowsReader,
    TableResult,
    TableRow,
)
from frisket.engine.executor.action_inventory import ExecutorDeps
from frisket.engine.executor.sheet_rows_read import AdmittedSheetRowsReader
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.executor.table_preview import preview_table
from frisket.engine.store import Project
from frisket.engine.store.project import ProjectReadSnapshot
from frisket.engine.store.receipts import ReceiptStore
from frisket.sdk import SheetRowsReader as PublicSheetRowsReader


class SummaryParams(ActionParams):
    constellation: ColumnRef[str]
    rating: ColumnRef[float]
    star: ColumnRef[str]


class SummaryOutput(BaseModel):
    constellation: str
    star_count: int
    rating_total: float
    top_star: str


def summarize_stars(
    params: SummaryParams, rows: SheetRowsReader
) -> TableResult[SummaryOutput]:
    groups = {}
    for item in rows.read():
        constellation = params.constellation.read(item.row)
        groups.setdefault(constellation, []).append(item)
    return TableResult(
        rows=[
            TableRow(
                output=SummaryOutput(
                    constellation=constellation,
                    star_count=len(stars),
                    rating_total=sum(params.rating.read(star.row) for star in stars),
                    top_star=max(
                        stars, key=lambda star: params.rating.read(star.row)
                    ).row.read(params.star.name),
                ),
                sources=tuple(star.source for star in stars),
            )
            for constellation, stars in sorted(groups.items())
        ]
    )


def forge_source(
    params: SummaryParams, rows: SheetRowsReader
) -> TableResult[SummaryOutput]:
    item = next(iter(rows.read()))
    return TableResult(
        rows=[
            TableRow(
                output=SummaryOutput(
                    constellation=params.constellation.read(item.row),
                    star_count=1,
                    rating_total=params.rating.read(item.row),
                    top_star=params.star.read(item.row),
                ),
                sources=(RowSource(item.source.sheet_id, item.source.row_id),),
            )
        ]
    )


def first_star(
    params: SummaryParams, rows: SheetRowsReader
) -> TableResult[SummaryOutput]:
    for item in rows.read():
        return TableResult(
            rows=[
                TableRow(
                    output=SummaryOutput(
                        constellation=params.constellation.read(item.row),
                        star_count=1,
                        rating_total=params.rating.read(item.row),
                        top_star=params.star.read(item.row),
                    ),
                    sources=(item.source,),
                    parent=item.source,
                )
            ]
        )
    return TableResult(rows=[])


retained_iterators = []


def retain_first_star(
    params: SummaryParams, rows: SheetRowsReader
) -> TableResult[SummaryOutput]:
    iterator = iter(rows.read())
    retained_iterators.append(iterator)
    item = next(iterator)
    return TableResult(
        rows=[
            TableRow(
                output=SummaryOutput(
                    constellation=params.constellation.read(item.row),
                    star_count=1,
                    rating_total=params.rating.read(item.row),
                    top_star=params.star.read(item.row),
                ),
                sources=(item.source,),
                parent=item.source,
            )
        ]
    )


def _registered(producer=summarize_stars) -> RegisteredAction:
    return RegisteredAction(
        "example.star_summary",
        action(
            name="star_summary",
            title="Star summary",
            description="Summarize selected source rows.",
            category=ActionCategory.CONVERT,
            run=create_sheet(producer),
        ),
    )


def _bound(
    sheet_id: int, *, row_ids: list[int] | None, key: str, producer=summarize_stars
):
    return BoundTypedActionRequest.bind(
        _registered(producer),
        ActionRequest(
            action_id="example.star_summary",
            scope={
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": row_ids,
            },
            params={
                "constellation": "constellation",
                "rating": "rating",
                "star": "star",
            },
            sheet_name=f"Summary {key}",
            idempotency_key=key,
        ),
    )


@pytest.fixture
def stars(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet_id = project.add_sheet("Stars")
        constellation = project.add_column(sheet_id, "constellation", "text")
        rating = project.add_column(sheet_id, "rating", "number")
        star = project.add_column(sheet_id, "star", "text")
        row_ids = project.add_rows(
            sheet_id,
            [
                {"constellation": "Lyra", "rating": 4.5, "star": "Vega"},
                {"constellation": "Orion", "rating": 3.5, "star": "Rigel"},
                {"constellation": "Lyra", "rating": 4.8, "star": "Sheliak"},
                {"constellation": "Orion", "rating": 2.5, "star": "Saiph"},
            ],
            {"constellation": constellation, "rating": rating, "star": star},
        )
        yield project, sheet_id, row_ids


def _preview(project, bound):
    return preview_table(
        project,
        "project",
        bound,
        deps=ExecutorDeps(),
        progress=lambda _completed, _total: None,
        cancelled=lambda: False,
    )


def test_sheet_rows_reader_is_available_to_plugins():
    assert PublicSheetRowsReader is SheetRowsReader


def test_sheet_rows_reader_previews_and_materializes_selected_aggregate_lineage(stars):
    project, sheet_id, row_ids = stars
    selected = _bound(
        sheet_id, row_ids=[row_ids[0], row_ids[1], row_ids[2]], key="selected"
    )

    preview = _preview(project, selected)
    assert [row["constellation"]["value"] for row in preview.rows] == ["Lyra", "Orion"]
    assert [row["star_count"]["value"] for row in preview.rows] == [2, 1]

    result = run_typed_create_sheet_action(project, "project", selected)
    assert result.status == "completed", result.errors
    output = result.outputs[0].ref
    assert output["row_count"] == 2
    assert (
        project.db.execute(
            "SELECT parent_sheet_id FROM sheets WHERE id=?", (output["sheet_id"],)
        ).fetchone()[0]
        == sheet_id
    )
    values = {
        name: project.get_values(output["sheet_id"], column_id)
        for name, column_id in output["columns"].items()
    }
    assert list(values["constellation"].values()) == ["Lyra", "Orion"]
    assert list(values["star_count"].values()) == [2, 1]
    assert list(values["top_star"].values()) == ["Sheliak", "Rigel"]

    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    [read] = receipt.inputs
    assert read.ref == {
        "kind": "sheet_rows_read",
        "sheet_id": sheet_id,
        "row_ids": row_ids[:3],
        "columns": [
            {"name": "constellation", "column_id": 1, "type": "text"},
            {"name": "rating", "column_id": 2, "type": "number"},
            {"name": "star", "column_id": 3, "type": "text"},
        ],
        "source_values_hash": read.ref["source_values_hash"],
    }
    assert read.ref["source_values_hash"].startswith("sha256:")
    assert "values" not in read.ref
    membership = next(
        item.ref
        for item in receipt.evidence
        if item.ref["kind"] == "materialized_row_sources"
    )
    assert {row["source_row_id"] for row in membership["rows"]} == set(row_ids[:3])


def test_sheet_rows_reader_all_scope_uses_all_visible_rows(stars):
    project, sheet_id, row_ids = stars
    bound = _bound(sheet_id, row_ids=None, key="all")

    preview = _preview(project, bound)
    assert [row["star_count"]["value"] for row in preview.rows] == [2, 2]
    result = run_typed_create_sheet_action(project, "project", bound)
    assert result.status == "completed", result.errors
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt.inputs[0].ref["row_ids"] == row_ids


def test_sheet_rows_reader_records_an_early_consumed_source(stars):
    project, sheet_id, row_ids = stars
    result = run_typed_create_sheet_action(
        project,
        "project",
        _bound(sheet_id, row_ids=row_ids, key="first", producer=first_star),
    )
    assert result.status == "completed", result.errors
    [read] = ReceiptStore(project).parsed_by_id(result.receipt_id).inputs
    assert read.ref["row_ids"] == row_ids
    assert read.ref["read_row_ids"] == [row_ids[0]]
    assert read.ref["source_values_hash"].startswith("sha256:")


def test_sheet_rows_reader_closes_a_retained_partial_iterator(stars):
    project, sheet_id, row_ids = stars
    retained_iterators.clear()
    result = run_typed_create_sheet_action(
        project,
        "project",
        _bound(sheet_id, row_ids=row_ids, key="retained", producer=retain_first_star),
    )
    assert result.status == "completed", result.errors
    [iterator] = retained_iterators
    with pytest.raises(StopIteration):
        next(iterator)
    [read] = ReceiptStore(project).parsed_by_id(result.receipt_id).inputs
    assert read.ref["read_row_ids"] == [row_ids[0]]
    assert read.ref["source_values_hash"].startswith("sha256:")


def test_sheet_rows_reader_fetches_values_in_bounded_column_pages(stars, monkeypatch):
    project, sheet_id, row_ids = stars
    columns = {column["name"]: column["id"] for column in project.columns(sheet_id)}
    row_ids.extend(
        project.add_rows(
            sheet_id,
            [
                {"constellation": "Lyra", "rating": 4.0, "star": f"Star {index}"}
                for index in range(501)
            ],
            columns,
        )
    )
    calls = []
    original = ProjectReadSnapshot.get_values

    def read_values(self, sheet_id, column_id, row_ids=None, **kwargs):
        calls.append(len(row_ids or ()))
        return original(self, sheet_id, column_id, row_ids=row_ids, **kwargs)

    monkeypatch.setattr(ProjectReadSnapshot, "get_values", read_values)
    reader = AdmittedSheetRowsReader(
        project,
        scope=SheetRows(sheet_id=sheet_id),
        params=SummaryParams(
            constellation="constellation", rating="rating", star="star"
        ),
    )
    assert len(list(reader.read())) == len(row_ids)
    assert len(calls) == 6
    assert max(calls) == 500


def test_sheet_rows_reader_requires_request_owned_sheet_selection(stars):
    _project, sheet_id, _row_ids = stars
    with pytest.raises(ValueError, match="sheet_rows"):
        BoundTypedActionRequest.bind(
            _registered(),
            ActionRequest(
                action_id="example.star_summary",
                scope={"kind": "project"},
                params={
                    "constellation": "constellation",
                    "rating": "rating",
                    "star": "star",
                },
                sheet_name="No scope",
                idempotency_key="no-scope",
            ),
        )


def test_sheet_rows_reader_rejects_forged_lineage(stars):
    project, sheet_id, row_ids = stars
    result = run_typed_create_sheet_action(
        project,
        "project",
        _bound(sheet_id, row_ids=[row_ids[0]], key="forged", producer=forge_source),
    )
    assert result.status == "failed"
    assert result.errors[0].code == "invalid_params"
    assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 1
