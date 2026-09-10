"""Public run projections report the stored canonical action kind."""

from __future__ import annotations

from pathlib import Path

from helpers import write_claimed_test_results
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore


def _seed_sheet(project: Project) -> int:
    sheet = project.add_sheet("data")
    cols = {"text": project.add_column(sheet, "text")}
    project.add_rows(
        sheet,
        [{"text": "call 212-555-0123"}, {"text": "no phone"}],
        cols,
    )
    return sheet


def _run_row(project: Project, run_id: int):
    return project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()


def _seed_divergent_run(project: Project) -> int:
    """Seed a row whose public kind differs from its legacy implementation id."""
    sheet = _seed_sheet(project)
    op_id = project.append_op("map", {"action_kind": "map.extract"}, label="seed")
    return RunResultStore(project).start_run(op_id, sheet, "map.translate")


def test_run_status_reports_the_stored_action_kind_column(tmp_path: Path) -> None:
    from frisket.authoring.actions import run_status

    project = Project.create(tmp_path / "p.frisket")
    try:
        run_id = _seed_divergent_run(project)
        payload = run_status("p", project, run_id)
        assert payload["run"]["action_kind"] == "map.translate"
    finally:
        project.close()


def test_run_status_preserves_a_canonical_plugin_kind_without_a_catalog_title(
    tmp_path: Path,
) -> None:
    from frisket.authoring.actions import run_status

    project = Project.create(tmp_path / "plugin.frisket")
    try:
        sheet_id = _seed_sheet(project)
        action_kind = "demo.plugin.op.clean"
        op_id = project.append_op(action_kind, {"action_kind": action_kind})
        run_id = RunResultStore(project).start_run(op_id, sheet_id, action_kind)

        payload = run_status("plugin", project, run_id)["run"]
        assert payload["action_kind"] == action_kind
        assert payload["action_kind"] == action_kind
        assert payload["action_name"] == "Unknown action"
    finally:
        project.close()


def test_provenance_manifest_reports_the_stored_action_kind_column(
    tmp_path: Path,
) -> None:
    from frisket.server.provenance_payloads import provenance_manifest_payload

    project = Project.create(tmp_path / "p.frisket")
    try:
        _seed_divergent_run(project)
        payload = provenance_manifest_payload(project, "p")
        assert [summary["action_kind"] for summary in payload["action_kinds"]] == [
            "map.translate"
        ]
        assert [run["action_kind"] for run in payload["runs"]] == ["map.translate"]
    finally:
        project.close()


def test_lineage_dag_ai_column_reports_the_stored_action_kind_column(
    tmp_path: Path,
) -> None:
    from frisket.engine.store.lineage import build_lineage_dag

    project = Project.create(tmp_path / "p.frisket")
    try:
        sheet_id = _seed_sheet(project)
        row_ids = [
            int(row["id"])
            for row in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? ORDER BY id", (sheet_id,)
            ).fetchall()
        ]
        column_id = project.add_column(sheet_id, "translated", ai_generated=True)
        store = RunResultStore(project)
        first_op = project.append_op(
            "map", {"action_kind": "map.extract"}, label="first"
        )
        first_run = store.start_run(
            first_op,
            sheet_id,
            "map.translate",
            total_rows=len(row_ids),
            row_ids=row_ids,
        )
        write_claimed_test_results(
            project,
            first_run,
            [
                {"row_id": row_id, "column_id": column_id, "value": "first"}
                for row_id in row_ids
            ],
        )
        store.finish_run(first_run)
        store.point_column_at_run(first_op, column_id, first_run)

        second_op = project.append_op(
            "map", {"action_kind": "map.summarize"}, label="second"
        )
        second_run = store.start_run(
            second_op,
            sheet_id,
            "map.summarize",
            total_rows=len(row_ids),
            row_ids=row_ids,
        )
        write_claimed_test_results(
            project,
            second_run,
            [
                {"row_id": row_id, "column_id": column_id, "value": "second"}
                for row_id in row_ids
            ],
        )
        store.finish_run(second_run)

        dag = build_lineage_dag(project)
        node = next(node for node in dag["nodes"] if node["kind"] == "ai_column")
        assert node["current_run_id"] == second_run
        assert node["action_kind"] == "map.summarize"
        assert project.get_column(column_id)["current_run_id"] == first_run
    finally:
        project.close()
