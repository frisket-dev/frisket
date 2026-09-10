from __future__ import annotations

from pathlib import Path
from typing import Any

from frisket.engine.store.materialization import (
    AggregateColumnSpec,
    AggregateMaterializedRow,
    AggregateSheetPlan,
    EdgeTableColumnSpec,
    EdgeTablePlan,
    MaterializedEdgeRecord,
    active_materialized_row_sources,
    materialized_row_source_diagnostics,
    write_aggregate_sheet,
    write_edge_table,
)
from frisket.features.investigations.rowsets import resolve_investigative_rowset
from frisket.engine.store import Project


def _index_names(project: Project) -> set[str]:
    return {
        row["name"]
        for row in project.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }


def _edge_project(tmp_path: Path) -> tuple[Project, Any]:
    project = Project.create(tmp_path / "edge-membership.frisket", name="edge")
    source_sheet = project.add_sheet("Source")
    source_col = project.add_column(source_sheet, "name")
    source_rows = project.add_rows(
        source_sheet,
        [{"name": "A"}, {"name": "B"}],
        {"name": source_col},
    )
    target_sheet = project.add_sheet("Target")
    target_col = project.add_column(target_sheet, "name")
    target_rows = project.add_rows(
        target_sheet,
        [{"name": "AA"}, {"name": "BB"}],
        {"name": target_col},
    )
    cur = project.db.cursor()
    cur.execute("BEGIN IMMEDIATE")
    write = write_edge_table(
        cur,
        EdgeTablePlan(
            action_kind="derive.link_table",
            label="derive.link_table Links",
            target_sheet_name="Links",
            parent_sheet_id=source_sheet,
            op_spec={"kind": "derive.link_table"},
            columns=[
                EdgeTableColumnSpec("source_row_id", "integer"),
                EdgeTableColumnSpec("target_row_id", "integer"),
            ],
            edges=[
                MaterializedEdgeRecord(
                    source_row_id=source_rows[0],
                    target_row_id=target_rows[0],
                    values={
                        "source_row_id": source_rows[0],
                        "target_row_id": target_rows[0],
                    },
                ),
                MaterializedEdgeRecord(
                    source_row_id=source_rows[1],
                    target_row_id=target_rows[1],
                    values={
                        "source_row_id": source_rows[1],
                        "target_row_id": target_rows[1],
                    },
                ),
            ],
        ),
    )
    project.db.commit()
    return project, write


def test_active_membership_reader_filters_hidden_rows_and_diagnostics(tmp_path: Path):
    project, write = _edge_project(tmp_path)
    try:
        active = active_materialized_row_sources(
            project.db, materialized_row_ids=write.row_ids
        )
        assert len(active) == 4

        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (write.row_ids[0],))
        project.db.commit()
        active_after_hide = active_materialized_row_sources(
            project.db, materialized_row_ids=write.row_ids
        )
        historical = active_materialized_row_sources(
            project.db,
            materialized_row_ids=write.row_ids,
            include_historical=True,
        )
        assert {row["materialized_row_id"] for row in active_after_hide} == {
            write.row_ids[1]
        }
        assert {row["materialized_row_id"] for row in historical} == set(write.row_ids)

        project.db.execute(
            "UPDATE materialized_row_sources SET source_sheet_id=? "
            "WHERE materialized_row_id=? AND role='edge_source'",
            (write.sheet_id, write.row_ids[1]),
        )
        project.db.execute(
            "UPDATE rows SET parent_row_id=NULL WHERE id=?",
            (write.row_ids[1],),
        )
        project.db.execute("PRAGMA ignore_check_constraints=ON")
        project.db.execute(
            "UPDATE materialized_row_sources SET role='bad_role' "
            "WHERE materialized_row_id=? AND role='edge_target'",
            (write.row_ids[1],),
        )
        project.db.execute("PRAGMA ignore_check_constraints=OFF")
        project.db.commit()
        issues = materialized_row_source_diagnostics(
            project.db,
            expected_roles_by_materialized_row={
                write.row_ids[0]: ("edge_source", "edge_target"),
                write.row_ids[1]: ("edge_source", "edge_target"),
            },
        )
        issue_kinds = {issue["kind"] for issue in issues}
        assert "source_sheet_id_mismatch" in issue_kinds
        assert "invalid_role" in issue_kinds
        assert "missing_active_membership" in issue_kinds
        assert "edge_parent_row_id_mismatch" in issue_kinds
    finally:
        project.close()


def test_active_membership_reader_chunks_large_materialized_row_sets(tmp_path: Path):
    project = Project.create(tmp_path / "large-membership.frisket", name="large")
    try:
        source_sheet = project.add_sheet("Source")
        source_col = project.add_column(source_sheet, "name")
        source_rows = project.add_rows(
            source_sheet,
            [{"name": f"row {idx}"} for idx in range(1100)],
            {"name": source_col},
        )
        cur = project.db.cursor()
        cur.execute("BEGIN IMMEDIATE")
        write = write_aggregate_sheet(
            cur,
            AggregateSheetPlan(
                action_kind="reduce.group_summary",
                label="reduce.group_summary Large",
                target_sheet_name="Large",
                parent_sheet_id=source_sheet,
                op_spec={"kind": "reduce.group_summary"},
                columns=[AggregateColumnSpec("group", "text")],
                rows=[
                    AggregateMaterializedRow(
                        values={"group": f"group {idx}"},
                        source_row_ids=[source_row_id],
                    )
                    for idx, source_row_id in enumerate(source_rows)
                ],
            ),
        )
        project.db.commit()

        rows = active_materialized_row_sources(
            project.db,
            materialized_row_ids=write.row_ids,
            roles=("aggregate_source",),
        )
        assert len(rows) == len(write.row_ids)
        assert [row["materialized_row_id"] for row in rows] == write.row_ids
        assert [row["source_row_id"] for row in rows] == source_rows
    finally:
        project.close()


def test_archive_round_trip_preserves_membership_table_and_indexes(tmp_path: Path):
    project, write = _edge_project(tmp_path)
    zip_path = tmp_path / "edge.frisket.zip"
    try:
        project.export(zip_path, include_media=False)
    finally:
        project.close()

    restored = Project.import_bundle(zip_path, tmp_path / "restored.frisket")
    try:
        assert restored.db.execute(
            "SELECT COUNT(*) FROM materialized_row_sources"
        ).fetchone()[0] == len(write.membership_rows)
        assert {
            "idx_materialized_row_sources_source",
            "idx_materialized_row_sources_materialized",
            "idx_materialized_row_sources_op",
        }.issubset(_index_names(restored))
    finally:
        restored.close()


def test_rowset_records_expose_active_aggregate_membership_without_fake_parent(
    tmp_path: Path,
):
    project = Project.create(tmp_path / "rowset.frisket", name="rowset")
    try:
        source_sheet = project.add_sheet("Source")
        source_col = project.add_column(source_sheet, "name")
        source_rows = project.add_rows(
            source_sheet,
            [{"name": "Alice"}, {"name": "Alicia"}],
            {"name": source_col},
        )
        cur = project.db.cursor()
        cur.execute("BEGIN IMMEDIATE")
        write = write_aggregate_sheet(
            cur,
            AggregateSheetPlan(
                action_kind="resolve.entities",
                label="resolve.entities Entities",
                target_sheet_name="Entities",
                parent_sheet_id=source_sheet,
                op_spec={"kind": "resolve.entities"},
                columns=[AggregateColumnSpec("entity", "text")],
                rows=[
                    AggregateMaterializedRow(
                        values={"entity": "Alice"},
                        source_row_ids=source_rows,
                    )
                ],
            ),
        )
        project.db.commit()

        rowset = resolve_investigative_rowset(
            project,
            {"kind": "materialized_sheet", "sheet_id": write.sheet_id},
        )
        record = rowset["records"][0]
        assert record["lineage"]["parent_row_ref"] is None
        assert record["materialized_membership"] == [
            {
                "materialized_row_id": write.row_ids[0],
                "source_row_id": source_rows[0],
                "source_sheet_id": source_sheet,
                "op_id": write.op_id,
                "role": "aggregate_source",
            },
            {
                "materialized_row_id": write.row_ids[0],
                "source_row_id": source_rows[1],
                "source_sheet_id": source_sheet,
                "op_id": write.op_id,
                "role": "aggregate_source",
            },
        ]
    finally:
        project.close()
