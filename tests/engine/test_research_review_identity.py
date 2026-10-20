from __future__ import annotations

from pathlib import Path

from frisket.actions.row_research import ANSWER
from frisket.contracts.http.history_review import ReviewBundleItem
from frisket.engine.runner.review import review_bundle_page, review_runs_page
from frisket.engine.store import Project
from frisket.engine.store.review_stats import rebuild_run_review_stats
from tests.engine.test_run_review_stats import _field, _managed_run


def test_research_review_roles_use_authored_outputs_and_selected_run(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "research-review.frisket")
    try:
        sheet_id = project.add_sheet("Companies")
        # The answer deliberately has a conventional support name, while the
        # sources have a name that used to look like an independent result.
        answer = project.add_column(sheet_id, "source", ai_generated=True)
        sources = project.add_column(sheet_id, "Web pages", ai_generated=True)
        row_ids = project.add_rows(sheet_id, [{}], {})
        params = {"output_names": {"answer": "source", "sources": "Web pages"}}
        run_id = _managed_run(
            project,
            sheet_id=sheet_id,
            columns=[answer, sources],
            row_ids=row_ids,
            action_kind="research.answer",
            params=params,
        )
        assert _field(project, run_id, answer)["is_primary"] == 1
        assert _field(project, run_id, sources)["is_primary"] == 0
        assert ANSWER.title == "Agent Research"

        # Replacing both current cells must not replace the selected old run's
        # supporting value or logical output mapping.
        _managed_run(
            project,
            sheet_id=sheet_id,
            columns=[answer, sources],
            row_ids=row_ids,
            action_kind="research.answer",
            params=params,
        )
        bundle = review_bundle_page(project, run_id=run_id)["bundles"][0]
        assert [(item["output_key"], item["role"]) for item in bundle["items"]] == [
            ("answer", "field"),
            ("sources", "evidence"),
        ]
        assert bundle["evidence"][0]["value"] == f"{run_id}:{row_ids[0]}:{sources}"
        assert bundle["evidence"][0]["chore"] is False
        assert bundle["evidence"][0]["can_edit"] is False
        assert ReviewBundleItem.model_validate(bundle["evidence"][0]).output_key == (
            "sources"
        )

        # Historical runs whose renamed sources were frozen as primary must
        # retain their decision controls and counts after a normal rebuild.
        project.db.execute(
            "UPDATE run_review_fields SET is_primary=1 WHERE run_id=?",
            (run_id,),
        )
        rebuild_run_review_stats(project.db, run_id)
        historical = review_bundle_page(project, run_id=run_id)["bundles"][0]
        assert len(historical["fields"]) == 2
        assert historical["evidence"] == []
        source_item = next(
            item for item in historical["items"] if item["output_key"] == "sources"
        )
        assert source_item["chore"] is True
        assert (
            review_runs_page(project, run_id=run_id)["runs"][0]["total"][
                "eligible_count"
            ]
            == 2
        )
    finally:
        project.close()


def test_unmanaged_review_items_have_no_logical_output_key(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "unmanaged-review.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        column = project.add_column(sheet_id, "result", ai_generated=True)
        row_ids = project.add_rows(sheet_id, [{}], {})
        run_id = _managed_run(
            project, sheet_id=sheet_id, columns=[column], row_ids=row_ids
        )
        item = review_bundle_page(project, run_id=run_id)["bundles"][0]["items"][0]
        assert item["output_key"] is None
        without_key = {key: value for key, value in item.items() if key != "output_key"}
        assert ReviewBundleItem.model_validate(without_key).output_key is None
    finally:
        project.close()
