from __future__ import annotations

import sqlite3
from pathlib import Path

from frisket.engine.runner.review import review_bundles
from frisket.engine.store import Project
from frisket.engine.store.review_confidence_migration import (
    REVIEW_CONFIDENCE_FROM_DIGEST,
    REVIEW_CONFIDENCE_TO_DIGEST,
)
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY
from frisket.engine.store.scalar_storage_migration import (
    SCALAR_CURRENT_VALUES_FROM_DIGEST,
)
from helpers import write_claimed_test_results


def _review_run(tmp_path: Path) -> tuple[Project, int, list[int], int, int]:
    project = Project.create(tmp_path / "confidence.frisket")
    sheet_id = project.add_sheet("Rows")
    first = project.add_column(sheet_id, "first", ai_generated=True)
    second = project.add_column(sheet_id, "second", ai_generated=True)
    row_ids = project.add_rows(sheet_id, [{} for _ in range(8)], {})
    op_id = project.append_op("map", {"fixture": "confidence ordering"})
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.extract",
        params={"fields": [{"name": "first"}, {"name": "second"}]},
        row_ids=row_ids,
        total_rows=len(row_ids),
    )
    confidence = {
        (row_ids[0], first): 0.8,
        (row_ids[0], second): 0.2,
        (row_ids[1], first): 0.2,
        (row_ids[1], second): 0.8,
        (row_ids[2], first): 0.1,
        (row_ids[2], second): None,
    }
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": column_id,
                "value": f"{row_id}:{column_id}",
                "confidence": confidence.get((row_id, column_id)),
            }
            for row_id in row_ids
            for column_id in (first, second)
        ],
    )
    RunResultStore(project).finish_run(run_id, "completed")
    return project, run_id, row_ids, first, second


def test_confidence_pages_merge_field_streams_by_exact_row_minimum(
    tmp_path: Path,
) -> None:
    project, run_id, row_ids, _first, second = _review_run(tmp_path)
    try:
        first_page = review_bundles(project, run_id=run_id, limit=2, offset=0)
        second_page = review_bundles(project, run_id=run_id, limit=2, offset=2)
        assert [item["row_id"] for item in first_page + second_page] == [
            row_ids[2],
            row_ids[0],
            row_ids[1],
            row_ids[3],
        ]
        assert [item["confidence"] for item in first_page + second_page] == [
            0.1,
            0.2,
            0.2,
            None,
        ]

        one_field = review_bundles(
            project,
            run_id=run_id,
            field_id=second,
            limit=4,
            offset=0,
        )
        assert [item["row_id"] for item in one_field] == [
            row_ids[0],
            row_ids[1],
            row_ids[2],
            row_ids[3],
        ]
        assert [item["confidence"] for item in one_field] == [0.2, 0.8, None, None]
    finally:
        project.close()


def test_confidence_candidates_apply_pending_filter_before_ranking(
    tmp_path: Path,
) -> None:
    project, run_id, row_ids, first, second = _review_run(tmp_path)
    try:
        store = RunResultStore(project)
        for column_id in (first, second):
            store.set_result_review_state(run_id, row_ids[2], column_id, "verified")
            store.set_result_review_metadata(
                run_id, row_ids[2], column_id, "accept", None
            )

        pending = review_bundles(project, run_id=run_id, limit=3)
        assert [item["row_id"] for item in pending] == [
            row_ids[0],
            row_ids[1],
            row_ids[3],
        ]
        including_reviewed = review_bundles(
            project, run_id=run_id, limit=3, include_reviewed=True
        )
        assert [item["row_id"] for item in including_reviewed] == [
            row_ids[2],
            row_ids[0],
            row_ids[1],
        ]
    finally:
        project.close()


def test_prior_review_schema_adds_confidence_ordering_index_on_open(
    tmp_path: Path,
) -> None:
    path = tmp_path / "prior.frisket"
    Project.create(path).close()
    with sqlite3.connect(path / "project.db") as db:
        index_rows = db.execute("PRAGMA index_list(results)").fetchall()
        confidence_index = next(
            str(row[1])
            for row in index_rows
            if tuple(
                column[2]
                for column in db.execute(f"PRAGMA index_info({row[1]})").fetchall()
            )
            == ("run_id", "column_id", "confidence", "row_id")
        )
        db.execute(f"DROP INDEX {confidence_index}")
        db.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (REVIEW_CONFIDENCE_FROM_DIGEST, SCHEMA_DIGEST_META_KEY),
        )

    migrated = Project(path)
    try:
        assert REVIEW_CONFIDENCE_TO_DIGEST == SCALAR_CURRENT_VALUES_FROM_DIGEST
        assert migrated.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
        index_columns = {
            tuple(
                column[2]
                for column in migrated.db.execute(
                    f"PRAGMA index_info({row['name']})"
                ).fetchall()
            )
            for row in migrated.db.execute("PRAGMA index_list(results)")
        }
        assert ("run_id", "column_id", "confidence", "row_id") in index_columns
    finally:
        migrated.close()
