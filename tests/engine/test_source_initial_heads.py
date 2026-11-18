"""Initial source writes populate heads without weakening ordered authority."""

from contextlib import closing

from frisket.engine.store import Project
from frisket.engine.store.cell_projection import HEAD_COLUMNS
from frisket.engine.store.cell_writes import (
    BaseCellWrite,
    create_base_cell_producer,
    initialize_base_cells,
)
from frisket.engine.store.changes import start_change


def test_large_initial_import_has_canonical_heads_before_first_read(tmp_path):
    with closing(Project.create(tmp_path / "heads.frisket")) as project:
        sheet = project.add_sheet("Rows")
        columns = {
            name: project.add_column(sheet, name, type=kind)
            for name, kind in (("text", "text"), ("number", "number"), ("json", "json"))
        }
        rows = project.add_rows(
            sheet,
            [
                {
                    "text": "é" * (256 if i % 2 else 257),
                    "number": "invalid" if i % 3 else i,
                    "json": {"row": i},
                }
                for i in range(700)
            ],
            columns,
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM current_cells").fetchone()[0]
            == 2100
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM current_cell_repair").fetchone()[0]
            == 0
        )
        cached = project.db.execute(
            f"SELECT {HEAD_COLUMNS} FROM current_cells ORDER BY column_id,row_id"
        ).fetchall()
        canonical = project.db.execute(
            f"SELECT {HEAD_COLUMNS} FROM canonical_cell_heads ORDER BY column_id,row_id"
        ).fetchall()
        assert [tuple(row) for row in cached] == [tuple(row) for row in canonical]
        assert project.undo() is not None
        assert project.visible_row_ids(sheet) == []
        assert project.redo() is not None
        assert project.visible_row_ids(sheet) == rows
        assert (
            project.get_values(sheet, columns["text"], rows[:1])[rows[0]] == "é" * 257
        )


def test_initializing_absent_base_cell_still_respects_newer_edit(tmp_path):
    with closing(Project.create(tmp_path / "ordered.frisket")) as project:
        sheet = project.add_sheet("Rows")
        column = project.add_column(sheet, "text")
        rows = project.add_rows(sheet, [{}, {}, {}], {})
        op_id = project.append_change("source", {})
        project.db.execute("BEGIN IMMEDIATE")
        producer = create_base_cell_producer(
            project.db, stage_id="delayed-source", op_id=op_id
        )
        start_change(project.db, op_id, writing=True)
        project.db.commit()
        project.apply_edits(
            [
                {"row_id": row, "column_id": column, "value": "later edit"}
                for row in rows
            ]
        )
        untouched = tuple(
            project.db.execute(
                f"SELECT {HEAD_COLUMNS} FROM current_cells WHERE row_id=? AND column_id=?",
                (rows[1], column),
            ).fetchone()
        )
        project.db.execute("BEGIN IMMEDIATE")
        initialize_base_cells(
            project.db,
            producer_id=producer,
            cells=[
                BaseCellWrite(row, column, "older source") for row in (rows[0], rows[2])
            ],
        )
        project.db.commit()
        assert project.get_values(sheet, column) == {row: "later edit" for row in rows}
        # Exact initialization must not scan/refresh the gap between sparse keys.
        assert (
            tuple(
                project.db.execute(
                    f"SELECT {HEAD_COLUMNS} FROM current_cells WHERE row_id=? AND column_id=?",
                    (rows[1], column),
                ).fetchone()
            )
            == untouched
        )
        assert project.undo() is not None
        assert project.get_values(sheet, column) == {
            rows[0]: "older source",
            rows[1]: None,
            rows[2]: "older source",
        }
