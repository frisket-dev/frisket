from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from frisket.engine.store import Project


def test_add_rows_commits_nested_batch_and_records_one_producer(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet_id = project.add_sheet("Records")
        column_id = project.add_column(sheet_id, "value")

        project.db.execute("BEGIN IMMEDIATE")
        project.add_rows(
            sheet_id,
            [{"value": "one"}, {"value": "two"}],
            {"value": column_id},
        )

        with sqlite3.connect(project.db_path) as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM rows WHERE sheet_id=?", (sheet_id,)
                ).fetchone()[0]
                == 2
            )
            producers = db.execute(
                "SELECT p.op_id,o.kind FROM cells c "
                "JOIN base_cell_producers p ON p.id=c.producer_id "
                "JOIN ops o ON o.id=p.op_id "
                "WHERE c.column_id=?",
                (column_id,),
            ).fetchall()
            assert len(producers) == 2
            assert {row[0] for row in producers} == {producers[0][0]}
            assert {row[1] for row in producers} == {"add_rows"}


def test_add_rows_failure_rolls_back_only_its_nested_savepoint(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        project.db.execute("BEGIN IMMEDIATE")
        sheet_id = project.add_sheet("Outer", commit=False)
        column_id = project.add_column(sheet_id, "value", commit=False)

        with pytest.raises(ValueError, match="unknown base-cell producer"):
            project.add_rows(
                sheet_id,
                [{"value": "discarded"}],
                {"value": column_id},
                producer_id=999,
                commit=False,
            )

        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM rows WHERE sheet_id=?", (sheet_id,)
            ).fetchone()[0]
            == 0
        )
        project.db.commit()

        with sqlite3.connect(project.db_path) as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM columns WHERE id=?", (column_id,)
                ).fetchone()[0]
                == 1
            )
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM rows WHERE sheet_id=?", (sheet_id,)
                ).fetchone()[0]
                == 0
            )
