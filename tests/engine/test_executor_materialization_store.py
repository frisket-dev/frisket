from __future__ import annotations

from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.materialization import (
    EdgeTableColumnSpec,
    EdgeTablePlan,
    MaterializedEdgeRecord,
    active_materialized_row_sources,
    materialized_row_source_diagnostics,
    materialized_row_sources_ref_matches,
    write_edge_table,
)


def test_materialization_store_writes_and_reads_active_membership(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "materialization.frisket", name="Mat")
    source_sheet_id = project.add_sheet("People")
    name_col = project.add_column(source_sheet_id, "name", type="text")
    row_ids = project.add_rows(
        source_sheet_id,
        [{"name": "Alice"}, {"name": "Bob"}],
        {"name": name_col},
    )

    cur = project.db.cursor()
    cur.execute("BEGIN IMMEDIATE")
    write = write_edge_table(
        cur,
        EdgeTablePlan(
            action_kind="test.edge_table",
            label="edge table",
            target_sheet_name="Edges",
            parent_sheet_id=source_sheet_id,
            op_spec={"kind": "test.edge_table"},
            columns=[EdgeTableColumnSpec("label", "text")],
            edges=[
                MaterializedEdgeRecord(
                    source_row_id=row_ids[0],
                    target_row_id=row_ids[1],
                    values={"label": "knows"},
                )
            ],
        ),
    )
    project.db.commit()

    active = active_materialized_row_sources(project.db.cursor())
    assert active == write.membership_rows
    assert materialized_row_sources_ref_matches(
        write.materialized_row_sources_ref,
        active,
    )
    assert materialized_row_source_diagnostics(project.db.cursor()) == []

    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (write.row_ids[0],))
    project.db.commit()
    assert active_materialized_row_sources(project.db.cursor()) == []
    historical = active_materialized_row_sources(
        project.db.cursor(),
        include_historical=True,
    )
    assert historical == write.membership_rows
