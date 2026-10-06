from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.scalar_storage_migration import (
    SCALAR_CURRENT_VALUES_FROM_DIGEST,
)
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY
from helpers import run_writer_authority_fixture
from tests.engine.test_current_cells_migration import _prior_bundle


_PREDECESSOR_CURRENT_CELLS = """
CREATE TABLE current_cells (
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  origin_kind TEXT NOT NULL CHECK (
    origin_kind IN ('source_cell', 'run_result', 'manual_edit')
  ),
  origin_op_id INTEGER REFERENCES ops(id) ON DELETE CASCADE,
  origin_run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
  base_producer_id INTEGER REFERENCES base_cell_producers(id) ON DELETE RESTRICT,
  validity TEXT NOT NULL CHECK (validity IN ('valid', 'missing', 'invalid')),
  CHECK (
    (origin_kind='source_cell' AND origin_op_id IS NULL AND origin_run_id IS NULL)
    OR
    (origin_kind='run_result' AND origin_op_id IS NOT NULL
      AND origin_run_id IS NOT NULL AND base_producer_id IS NULL)
    OR
    (origin_kind='manual_edit' AND origin_op_id IS NOT NULL
      AND origin_run_id IS NULL AND base_producer_id IS NULL)
  )
)
"""

_PREDECESSOR_CURRENT_CELL_VALUES = """
CREATE VIEW current_cell_values AS
SELECT head.column_id,head.row_id,
       CASE head.origin_kind
         WHEN 'source_cell' THEN (
           SELECT source.value_kind FROM cells AS source
           WHERE source.row_id=head.row_id
             AND source.column_id=head.column_id
             AND source.producer_id IS head.base_producer_id
         )
         WHEN 'run_result' THEN (
           SELECT CASE result.publication_effect
             WHEN 'publish_value' THEN result.value_kind
             WHEN 'publish_null' THEN 'null'
           END
           FROM results AS result
           WHERE result.run_id=head.origin_run_id
             AND result.row_id=head.row_id
             AND result.column_id=head.column_id
         )
         WHEN 'manual_edit' THEN (
           SELECT edit.value_kind FROM edits AS edit
           WHERE edit.op_id=head.origin_op_id
             AND edit.row_id=head.row_id
             AND edit.column_id=head.column_id
         )
       END AS value_kind,
       CASE head.origin_kind
         WHEN 'source_cell' THEN (
           SELECT source.value FROM cells AS source
           WHERE source.row_id=head.row_id
             AND source.column_id=head.column_id
             AND source.producer_id IS head.base_producer_id
         )
         WHEN 'run_result' THEN (
           SELECT CASE WHEN result.publication_effect='publish_value'
             THEN result.value END
           FROM results AS result
           WHERE result.run_id=head.origin_run_id
             AND result.row_id=head.row_id
             AND result.column_id=head.column_id
         )
         WHEN 'manual_edit' THEN (
           SELECT edit.value FROM edits AS edit
           WHERE edit.op_id=head.origin_op_id
             AND edit.row_id=head.row_id
             AND edit.column_id=head.column_id
         )
       END AS value,
       head.origin_kind,head.origin_op_id,head.origin_run_id,
       head.base_producer_id,head.validity
FROM current_cells AS head INDEXED BY idx_current_cells_column_row
"""


def _seed_mixed_origins(path: Path) -> tuple[int, int, int, list[int]]:
    project = Project.create(path, name="Scalar migration")
    sheet_id = project.add_sheet("Rows")
    source_column_id = project.add_column(sheet_id, "source", type="integer")
    output_column_id = project.add_column(
        sheet_id, "generated", type="text", ai_generated=True
    )
    row_ids = project.add_rows(
        sheet_id,
        [{"source": 10}, {"source": 20}, {"source": 30}],
        {"source": source_column_id},
    )

    op_id = project.append_op(
        "map.regex_extract", {"pattern": "(.*)"}, label="generate values"
    )
    results = RunResultStore(project)
    run_id = results.start_run(
        op_id,
        sheet_id,
        "map.regex_extract",
        row_ids=row_ids,
        total_rows=len(row_ids),
    )
    authority = run_writer_authority_fixture(
        project, run_id, output_column_ids={output_column_id}
    )
    assert authority.claim_token is not None
    generations = ResultGenerationStore(project)
    generations.declare(
        run_id,
        output_column_id,
        output_role="match",
        compatibility_key="sha256:scalar-migration-fixture",
        write_mode="create",
        claim_token=authority.claim_token,
    )
    results.write_results(
        run_id,
        [
            {
                "row_id": row_ids[0],
                "column_id": output_column_id,
                "value": "generated",
                "publication_effect": "publish_value",
            },
            {
                "row_id": row_ids[1],
                "column_id": output_column_id,
                "value": None,
                "publication_effect": "publish_null",
            },
            {
                "row_id": row_ids[2],
                "column_id": output_column_id,
                "value": "before edit",
                "publication_effect": "publish_value",
            },
        ],
        **authority.kwargs(),
    )
    generations.seal(
        run_id,
        [output_column_id],
        claim_token=authority.claim_token,
        terminal_disposition="completed",
    )
    results.point_column_at_run(op_id, output_column_id, run_id)
    OutputColumnClaimStore(project).release(claim_token=authority.claim_token)
    project.apply_edits(
        [
            {
                "row_id": row_ids[2],
                "column_id": output_column_id,
                "value": "manual",
            }
        ]
    )
    project.close()
    return sheet_id, source_column_id, output_column_id, row_ids


def _downgrade_to_scalar_predecessor(path: Path) -> None:
    with sqlite3.connect(path / "project.db") as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DROP VIEW current_cell_values")
        db.execute("ALTER TABLE current_cells RENAME TO projected_current_cells")
        db.execute(_PREDECESSOR_CURRENT_CELLS)
        db.execute(
            "INSERT INTO current_cells "
            "(column_id,row_id,origin_kind,origin_op_id,origin_run_id,"
            "base_producer_id,validity) "
            "SELECT column_id,row_id,origin_kind,origin_op_id,origin_run_id,"
            "base_producer_id,validity FROM projected_current_cells"
        )
        db.execute("DROP TABLE projected_current_cells")
        db.execute(
            "CREATE INDEX idx_current_cells_row ON current_cells(row_id,column_id)"
        )
        db.execute(
            "CREATE UNIQUE INDEX idx_current_cells_column_row "
            "ON current_cells(column_id,row_id)"
        )
        db.execute(_PREDECESSOR_CURRENT_CELL_VALUES)
        _drop_post_scalar_objects(db)
        db.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (SCALAR_CURRENT_VALUES_FROM_DIGEST, SCHEMA_DIGEST_META_KEY),
        )


def _drop_post_scalar_objects(db: sqlite3.Connection) -> None:
    """Make a current fixture represent a bundle from before later migrations."""

    db.execute("DROP VIEW IF EXISTS prepared_content_ref_values")
    for table in (
        "prepared_content_refs",
        "prepared_content_set_pages",
        "prepared_content_sets",
        "prepared_page_versions",
        "extraction_layout_selection",
        "extraction_layout_documents",
        "extraction_layouts",
    ):
        db.execute(f"DROP TABLE IF EXISTS {table}")


def _current_values(
    project: Project, column_id: int
) -> list[tuple[int, object, str, str]]:
    return [
        (int(row[0]), row[1], str(row[2]), str(row[3]))
        for row in project.db.execute(
            "SELECT row_id,value,value_kind,origin_kind FROM current_cell_values "
            "WHERE column_id=? ORDER BY row_id",
            (column_id,),
        )
    ]


def test_scalar_migration_preserves_authority_values_history_and_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mixed.frisket"
    sheet_id, source_column_id, output_column_id, row_ids = _seed_mixed_origins(path)
    _downgrade_to_scalar_predecessor(path)

    project = Project(path)

    assert project.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
    assert project.get_values(sheet_id=sheet_id, column_id=source_column_id) == {
        row_ids[0]: 10,
        row_ids[1]: 20,
        row_ids[2]: 30,
    }
    assert project.get_values(sheet_id=sheet_id, column_id=output_column_id) == {
        row_ids[0]: "generated",
        row_ids[1]: None,
        row_ids[2]: "manual",
    }
    assert _current_values(project, output_column_id) == [
        (row_ids[0], "generated", "text", "run_result"),
        (row_ids[1], None, "null", "run_result"),
        (row_ids[2], "manual", "text", "manual_edit"),
    ]
    projected = project.db.execute(
        "SELECT row_id,inline_value_kind,inline_value FROM current_cells "
        "WHERE column_id=? ORDER BY row_id",
        (output_column_id,),
    ).fetchall()
    assert [tuple(row) for row in projected] == [
        (row_ids[0], None, None),
        (row_ids[1], "null", None),
        (row_ids[2], None, None),
    ]
    source_projection = project.db.execute(
        "SELECT inline_value_kind,inline_value FROM current_cells "
        "WHERE column_id=? ORDER BY row_id",
        (source_column_id,),
    ).fetchall()
    assert [tuple(row) for row in source_projection] == [
        ("integer", 10),
        ("integer", 20),
        ("integer", 30),
    ]
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 3
    assert project.db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 1
    assert [str(row["kind"]) for row in project.history()] == [
        "add_rows",
        "map.regex_extract",
        "edit",
    ]
    assert project.db.execute("PRAGMA foreign_key_check").fetchall() == []
    project.close()

    with sqlite3.connect(path / "project.db") as db:
        db.execute(
            "CREATE TRIGGER reject_repeat_scalar_backfill "
            "BEFORE UPDATE OF inline_value_kind,inline_value ON current_cells "
            "BEGIN SELECT RAISE(ABORT,'scalar backfill repeated'); END"
        )

    reopened = Project(path)
    assert reopened.get_values(sheet_id, output_column_id)[row_ids[2]] == "manual"
    reopened.close()


def test_scalar_migration_rolls_back_and_retries_after_interrupted_stamp(
    tmp_path: Path,
) -> None:
    path = tmp_path / "interrupted.frisket"
    sheet_id, _, output_column_id, row_ids = _seed_mixed_origins(path)
    _downgrade_to_scalar_predecessor(path)
    with sqlite3.connect(path / "project.db") as db:
        db.execute(
            "CREATE TRIGGER interrupt_scalar_stamp BEFORE UPDATE ON meta "
            "WHEN NEW.key='schema_digest' AND NEW.value<>OLD.value BEGIN "
            "SELECT RAISE(ABORT,'migration interrupted'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="migration interrupted"):
        Project(path)

    with sqlite3.connect(path / "project.db") as db:
        assert (
            db.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
            ).fetchone()[0]
            == SCALAR_CURRENT_VALUES_FROM_DIGEST
        )
        assert {
            str(row[1]) for row in db.execute("PRAGMA table_info(current_cells)")
        }.isdisjoint({"inline_value_kind", "inline_value"})
        assert (
            db.execute(
                "SELECT value FROM current_cell_values WHERE row_id=? AND column_id=?",
                (row_ids[0], output_column_id),
            ).fetchone()[0]
            == "generated"
        )
        db.execute("DROP TRIGGER interrupt_scalar_stamp")

    migrated = Project(path)
    assert migrated.get_values(sheet_id, output_column_id) == {
        row_ids[0]: "generated",
        row_ids[1]: None,
        row_ids[2]: "manual",
    }
    migrated.close()


def test_supported_older_bundle_chains_through_scalar_migration(tmp_path: Path) -> None:
    path = _prior_bundle(tmp_path)
    with sqlite3.connect(path / "project.db") as db:
        _drop_post_scalar_objects(db)

    project = Project(path)

    assert project.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
    projected = project.db.execute(
        "SELECT row_id,inline_value_kind,inline_value FROM current_cells "
        "WHERE column_id=10 ORDER BY row_id"
    ).fetchall()
    assert [tuple(row) for row in projected] == [
        (100, None, None),
        (101, "null", None),
    ]
    assert _current_values(project, 10) == [
        (100, "edited", "text", "manual_edit"),
        (101, None, "null", "run_result"),
    ]
    assert project.db.execute("PRAGMA foreign_key_check").fetchall() == []
    project.close()
