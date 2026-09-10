from pathlib import Path

from frisket.engine.store.project import Project


def test_explicit_visible_rows_use_rowid_lookup_and_preserve_scope(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "rows.frisket", name="Rows")
    sheet_id = project.add_sheet("source")
    column_id = project.add_column(sheet_id, "value")
    row_ids = project.add_rows(
        sheet_id,
        [{"value": str(index)} for index in range(905)],
        {"value": column_id},
    )
    other_sheet = project.add_sheet("other")
    other_column = project.add_column(other_sheet, "value")
    other_row = project.add_rows(
        other_sheet, [{"value": "outside"}], {"value": other_column}
    )[0]
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row_ids[450],))

    selected = [*reversed(row_ids), row_ids[2], other_row, 999_999]
    assert project.visible_row_ids(sheet_id, selected) == [
        row_id for row_id in row_ids if row_id != row_ids[450]
    ]

    placeholders = ",".join("?" for _ in row_ids[:100])
    explicit_plan = project.db.execute(
        "EXPLAIN QUERY PLAN SELECT id, position FROM rows NOT INDEXED "
        f"WHERE sheet_id=? AND hidden=0 AND id IN ({placeholders})",
        [sheet_id, *row_ids[:100]],
    ).fetchall()
    assert any("USING INTEGER PRIMARY KEY" in row["detail"] for row in explicit_plan)

    all_visible_plan = project.db.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM rows "
        "WHERE sheet_id=? AND hidden=0 ORDER BY position",
        (sheet_id,),
    ).fetchall()
    assert any("idx_rows_sheet" in row["detail"] for row in all_visible_plan)
