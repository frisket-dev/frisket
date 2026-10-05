from __future__ import annotations

import random
from collections.abc import MutableSequence
from pathlib import Path
from typing import Any, TypeVar

from frisket.engine.runner.review_sampling import (
    exact_review_bundle_page,
    sample_review_bundle_page,
)
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.value_codec import encode_stored_value
from helpers import write_claimed_test_results


_T = TypeVar("_T")


class _MissingProbeRandom:
    def __init__(self, misses: list[int]):
        self.misses = misses
        self.probe_sizes: list[int] = []

    def sample(self, population: range, k: int) -> list[int]:
        assert all(value in population for value in self.misses[:k])
        self.probe_sizes.append(k)
        return self.misses[:k]

    def randrange(self, stop: int) -> int:
        return random.Random(stop).randrange(stop)

    def shuffle(self, values: MutableSequence[_T]) -> None:
        values.reverse()


def _finish_run(
    project: Project,
    *,
    sheet_id: int,
    row_ids: list[int],
    columns: list[int],
    outcomes: dict[tuple[int, int], str] | None = None,
    action_kind: str = "map.extract",
) -> int:
    op_id = project.append_op("map", {"fixture": "review sampling"})
    store = RunResultStore(project)
    run_id = store.start_run(
        op_id,
        sheet_id,
        action_kind,
        params={"input_columns": ["source"]},
        row_ids=row_ids,
        total_rows=len(row_ids),
    )
    batch: list[dict[str, Any]] = []
    for row_id in row_ids:
        for column_id in columns:
            outcome = (outcomes or {}).get((row_id, column_id), "ok")
            item: dict[str, Any] = {
                "row_id": row_id,
                "column_id": column_id,
                "outcome": outcome,
            }
            if outcome == "model_error":
                item["error"] = "fixture failure"
            else:
                item["value"] = f"value:{row_id}:{column_id}"
                item["confidence"] = row_id / 10_000
            batch.append(item)
    write_claimed_test_results(project, run_id, batch)
    store.finish_run(run_id, "completed")
    return run_id


def test_sparse_strict_probes_fall_back_once_without_duplicate_bundles(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "sparse-sample.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        source = project.add_column(sheet_id, "source")
        first = project.add_column(sheet_id, "answer", ai_generated=True)
        second = project.add_column(sheet_id, "category", ai_generated=True)
        all_rows = project.add_rows(
            sheet_id,
            [{"source": f"row {index}"} for index in range(90)],
            {"source": source},
        )
        run_rows = [all_rows[0], all_rows[20], all_rows[45], all_rows[70], all_rows[89]]
        outcomes = {
            (run_rows[0], first): "model_error",
            (run_rows[0], second): "model_error",
            (run_rows[-1], first): "model_error",
            (run_rows[-1], second): "model_error",
        }
        run_id = _finish_run(
            project,
            sheet_id=sheet_id,
            row_ids=run_rows,
            columns=[first, second],
            outcomes=outcomes,
        )
        eligible = set(run_rows[1:-1])
        gaps = [row_id for row_id in all_rows[1:-1] if row_id not in run_rows]
        rng = _MissingProbeRandom(gaps)

        first_page = sample_review_bundle_page(
            project,
            run_id=run_id,
            limit=2,
            include_reviewed=True,
            exclude_row_ids=[999_999],
            rng=rng,
        )
        first_ids = [int(bundle["row_id"]) for bundle in first_page["bundles"]]
        assert first_page["total"] == 3
        assert first_page["has_more"] is True
        assert len(first_ids) == len(set(first_ids)) == 2
        assert set(first_ids) < eligible

        last_page = sample_review_bundle_page(
            project,
            run_id=run_id,
            limit=2,
            include_reviewed=True,
            exclude_row_ids=[*first_ids, 999_999],
            rng=rng,
        )
        last_ids = [int(bundle["row_id"]) for bundle in last_page["bundles"]]
        assert last_page["total"] == 3
        assert last_page["has_more"] is False
        assert len(last_ids) == 1
        assert set(first_ids + last_ids) == eligible
        assert rng.probe_sizes == [48, 48]
    finally:
        project.close()


def test_exact_rows_preserve_order_filter_fields_and_read_fresh_decisions(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "exact-sample.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        source = project.add_column(sheet_id, "source")
        answer = project.add_column(sheet_id, "answer", ai_generated=True)
        category = project.add_column(sheet_id, "category", ai_generated=True)
        support = project.add_column(sheet_id, "answer_confidence", ai_generated=True)
        rows = project.add_rows(
            sheet_id,
            [{"source": f"row {index}"} for index in range(3)],
            {"source": source},
        )
        run_id = _finish_run(
            project,
            sheet_id=sheet_id,
            row_ids=rows,
            columns=[answer, category, support],
            outcomes={(rows[0], category): "model_error"},
        )

        store = RunResultStore(project)
        store.set_result_review_state(run_id, rows[0], answer, "verified", commit=False)
        store.set_result_review_metadata(
            run_id, rows[0], answer, "accept", "checked", commit=False
        )
        project.db.commit()

        pending = exact_review_bundle_page(
            project,
            run_id=run_id,
            row_ids=[rows[2], rows[0], rows[2]],
            field_id=answer,
            include_reviewed=False,
        )
        assert [bundle["row_id"] for bundle in pending["bundles"]] == [rows[2]]

        stable = exact_review_bundle_page(
            project,
            run_id=run_id,
            row_ids=[rows[2], rows[0], rows[2]],
            field_id=answer,
            include_reviewed=True,
        )
        assert [bundle["row_id"] for bundle in stable["bundles"]] == [
            rows[2],
            rows[0],
        ]
        reviewed = stable["bundles"][1]["fields"][0]
        assert reviewed["review_decision"] == "accept"
        assert reviewed["review_note"] == "checked"

        category_page = exact_review_bundle_page(
            project,
            run_id=run_id,
            row_ids=rows,
            field_id=category,
            include_reviewed=True,
        )
        assert category_page["total"] == 2
        assert [bundle["row_id"] for bundle in category_page["bundles"]] == rows[1:]
        assert all(
            [field["column_id"] for field in bundle["fields"]] == [answer, category]
            for bundle in category_page["bundles"]
        )

        support_page = exact_review_bundle_page(
            project,
            run_id=run_id,
            row_ids=rows,
            field_id=support,
            include_reviewed=True,
        )
        assert support_page["total"] == 0
        assert support_page["bundles"] == []
    finally:
        project.close()


def test_map_find_sampling_uses_derived_result_rows(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "find-sample.frisket")
    try:
        source_sheet = project.add_sheet("Sources")
        body = project.add_column(source_sheet, "body")
        source_row = project.add_rows(
            source_sheet, [{"body": "source document"}], {"body": body}
        )[0]

        findings_sheet = project.add_sheet("Findings")
        match = project.add_column(findings_sheet, "Match", ai_generated=True)
        findings = project.add_rows(findings_sheet, [{}, {}], {})
        op_id = project.append_op("map", {"fixture": "derived review output"})
        store = RunResultStore(project)
        run_id = store.start_run(
            op_id,
            source_sheet,
            "map.find",
            params={"input_columns": ["body"]},
            row_ids=[source_row],
            total_rows=1,
        )
        project.db.executemany(
            "INSERT INTO results "
            "(run_id,row_id,column_id,value_kind,value,outcome) "
            "VALUES (?,?,?,?,?,'ok')",
            [
                (run_id, row_id, match, *encode_stored_value(f"finding {index}"))
                for index, row_id in enumerate(findings)
            ],
        )
        project.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id=?", (run_id, match)
        )
        store.finish_run(run_id, "completed")

        page = sample_review_bundle_page(
            project,
            run_id=run_id,
            include_reviewed=True,
            limit=2,
            rng=random.Random(17),
        )
        assert {bundle["row_id"] for bundle in page["bundles"]} == set(findings)
        assert source_row not in {bundle["row_id"] for bundle in page["bundles"]}
        assert {
            int(row["row_id"])
            for row in project.db.execute(
                "SELECT row_id FROM run_rows WHERE run_id=?", (run_id,)
            )
        } == {source_row}
        assert {bundle["sheet_id"] for bundle in page["bundles"]} == {findings_sheet}
    finally:
        project.close()
