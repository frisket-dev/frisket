from __future__ import annotations

import copy
from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.materialization import (
    AggregateColumnSpec,
    AggregateMaterializedRow,
    AggregateSheetPlan,
    ContributorMaterializedRow,
    ContributorTablePlan,
    MaterializedColumnSpec,
    load_materialized_row_sources_for_op,
    materialized_row_sources_ref_matches,
    write_aggregate_sheet,
    write_contributor_table,
)


def _source_project(tmp_path: Path) -> tuple[Project, int, list[int]]:
    project = Project.create(tmp_path / "aggregate-membership.frisket")
    sheet_id = project.add_sheet("Source")
    source_id = project.add_column(sheet_id, "source", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [{"source": "alpha"}, {"source": "beta"}],
        {"source": source_id},
    )
    return project, sheet_id, row_ids


def _write_aggregate(project: Project, sheet_id: int, row_ids: list[int]):
    project.db.execute("BEGIN IMMEDIATE")
    write = write_aggregate_sheet(
        project.db.cursor(),
        AggregateSheetPlan(
            action_kind="test.aggregate",
            label="aggregate",
            target_sheet_name="Aggregate",
            parent_sheet_id=sheet_id,
            op_spec={"kind": "test.aggregate"},
            columns=[AggregateColumnSpec("group", "text")],
            rows=[
                AggregateMaterializedRow(
                    values={"group": "all"},
                    source_row_ids=row_ids,
                )
            ],
        ),
    )
    project.db.commit()
    return write


def test_aggregate_membership_ref_uses_the_stored_relation(tmp_path: Path) -> None:
    project, sheet_id, row_ids = _source_project(tmp_path)
    try:
        write = _write_aggregate(project, sheet_id, row_ids)
        ref = write.materialized_row_sources_ref
        stored = load_materialized_row_sources_for_op(project.db, op_id=write.op_id)

        assert set(ref) == {"kind", "op_id", "row_count", "sha256", "storage"}
        assert ref["storage"] == "normalized_relation_v1"
        assert ref["row_count"] == len(row_ids)
        assert materialized_row_sources_ref_matches(ref, stored)
    finally:
        project.close()


def test_compact_aggregate_ref_rejects_stored_membership_tamper(
    tmp_path: Path,
) -> None:
    project, sheet_id, row_ids = _source_project(tmp_path)
    try:
        write = _write_aggregate(project, sheet_id, row_ids)
        ref = write.materialized_row_sources_ref
        project.db.execute(
            "UPDATE materialized_row_sources SET role='edge_source' "
            "WHERE op_id=? AND source_row_id=?",
            (write.op_id, row_ids[0]),
        )
        project.db.commit()
        changed = load_materialized_row_sources_for_op(project.db, op_id=write.op_id)

        assert not materialized_row_sources_ref_matches(ref, changed)
    finally:
        project.close()


def test_old_embedded_refs_stay_strict_and_contributor_refs_keep_rows(
    tmp_path: Path,
) -> None:
    project, sheet_id, row_ids = _source_project(tmp_path)
    try:
        aggregate = _write_aggregate(project, sheet_id, row_ids)
        stored = load_materialized_row_sources_for_op(project.db, op_id=aggregate.op_id)
        old_ref = {**aggregate.materialized_row_sources_ref, "rows": stored}
        old_ref.pop("storage")
        assert materialized_row_sources_ref_matches(old_ref, stored)

        missing_rows_legacy_ref = copy.deepcopy(old_ref)
        missing_rows_legacy_ref.pop("rows")
        assert not materialized_row_sources_ref_matches(missing_rows_legacy_ref, stored)

        changed_ref = copy.deepcopy(old_ref)
        changed_ref["rows"][0]["source_row_id"] = row_ids[1]
        assert not materialized_row_sources_ref_matches(changed_ref, stored)

        project.db.execute("BEGIN IMMEDIATE")
        contributor = write_contributor_table(
            project.db.cursor(),
            ContributorTablePlan(
                action_kind="test.contributor",
                label="contributor",
                target_sheet_name="Contributors",
                parent_sheet_id=sheet_id,
                op_spec={"kind": "test.contributor"},
                columns=[MaterializedColumnSpec("name", "text")],
                rows=[
                    ContributorMaterializedRow(
                        values={"name": "both"},
                        sources=tuple(
                            (row_id, "aggregate_source") for row_id in row_ids
                        ),
                    )
                ],
            ),
        )
        project.db.commit()

        assert contributor.materialized_row_sources_ref["rows"]
        assert materialized_row_sources_ref_matches(
            contributor.materialized_row_sources_ref,
            load_materialized_row_sources_for_op(project.db, op_id=contributor.op_id),
        )
    finally:
        project.close()
