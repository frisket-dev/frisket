"""Known bundle upgrades preserve data; unknown schemas still refuse."""

from __future__ import annotations

import json
import sqlite3

import pytest

from frisket.engine.store import Project
from frisket.engine.store.schema import (
    SCHEMA,
    SCHEMA_DIGEST,
    SCHEMA_DIGEST_META_KEY,
    BundleSchemaMismatch,
    schema_digest,
)
from frisket.server.route_errors import RouteError
from frisket.server.workspace import Workspace
from frisket.engine.store.value_codec import decode_stored_value


# Independently pinned from v0.1.1a64 (6602458d), before consent_principal.
_A64_DIGEST = "frisket.schema.v1:0345f3cf7f8e37534124df43e4e97f05"

_REVIEW_METADATA_DDL = (
    "  review_decision TEXT CHECK (\n"
    "    review_decision IN ('accept', 'reject', 'reject_clear', 'edit')\n"
    "  ),\n"
    "  review_note TEXT,\n"
)


def _without_review_stats(schema: str) -> str:
    """Restore the exact typed schema before compact review summaries."""

    if "-- EXTRACTION_LAYOUTS_BEGIN" in schema:
        before, marked = schema.split("-- EXTRACTION_LAYOUTS_BEGIN", 1)
        _removed, after = marked.split("-- EXTRACTION_LAYOUTS_END", 1)
        schema = before + after
    for begin, end in (
        (
            "-- RUN_REVIEW_CURRENT_COLUMN_INDEX_BEGIN",
            "-- RUN_REVIEW_CURRENT_COLUMN_INDEX_END",
        ),
        ("-- RUN_REVIEW_RUN_COLUMNS_BEGIN", "-- RUN_REVIEW_RUN_COLUMNS_END"),
        ("-- RUN_REVIEW_FIELDS_BEGIN", "-- RUN_REVIEW_FIELDS_END"),
        ("-- RUN_REVIEW_HEAD_INDEX_BEGIN", "-- RUN_REVIEW_HEAD_INDEX_END"),
    ):
        before, marked = schema.split(begin, 1)
        _removed, after = marked.split(end, 1)
        schema = before + after
    return schema


def _replace_create_table(schema: str, table: str, replacement: str) -> str:
    prefix = f"CREATE TABLE IF NOT EXISTS {table}"
    start = schema.index(prefix)
    statement = ""
    for line in schema[start:].splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            return (
                schema[:start]
                + replacement.rstrip()
                + "\n"
                + schema[start + len(statement) :]
            )
    raise AssertionError(f"incomplete {table} DDL")


_LEGACY_CELLS = """CREATE TABLE IF NOT EXISTS cells (
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  value TEXT,
  -- CELL_PRODUCER_ID_BEGIN
  producer_id INTEGER REFERENCES base_cell_producers(id) ON DELETE RESTRICT,
  -- CELL_PRODUCER_ID_END
  UNIQUE (row_id, column_id)
);"""

_LEGACY_RESULTS = """CREATE TABLE IF NOT EXISTS results (
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  value TEXT,
  tokens_in INTEGER,
  tokens_out INTEGER,
  confidence REAL,
  justification TEXT,
  error TEXT,
  error_code TEXT,
  review_state TEXT NOT NULL DEFAULT 'unreviewed',
  review_decision TEXT CHECK (
    review_decision IN ('accept', 'reject', 'reject_clear', 'edit')
  ),
  review_note TEXT,
  outcome TEXT NOT NULL DEFAULT 'ok',
  publication_effect TEXT,
  PRIMARY KEY (run_id, row_id, column_id),
  CHECK (
    publication_effect IS NULL
    OR (publication_effect='publish_value' AND value IS NOT NULL AND error IS NULL)
    OR (publication_effect='publish_null' AND value IS NULL AND error IS NULL)
    OR (publication_effect='publish_error' AND value IS NULL AND error IS NOT NULL)
  )
) WITHOUT ROWID;"""

_LEGACY_EDITS = """CREATE TABLE IF NOT EXISTS edits (
  op_id INTEGER NOT NULL REFERENCES ops(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  value TEXT,
  PRIMARY KEY (op_id, row_id, column_id)
) WITHOUT ROWID;"""

_LEGACY_CURRENT_CELLS = """CREATE TABLE IF NOT EXISTS current_cells (
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  value TEXT,
  origin_kind TEXT NOT NULL CHECK (
    origin_kind IN ('source_cell', 'run_result', 'manual_edit')
  ),
  origin_op_id INTEGER REFERENCES ops(id) ON DELETE CASCADE,
  origin_run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
  base_producer_id INTEGER REFERENCES base_cell_producers(id) ON DELETE RESTRICT,
  validity TEXT NOT NULL CHECK (validity IN ('valid', 'missing', 'invalid')),
  CHECK (
    (origin_kind='source_cell' AND origin_op_id IS NULL AND origin_run_id IS NULL)
    OR (origin_kind='run_result' AND origin_op_id IS NOT NULL
        AND origin_run_id IS NOT NULL AND base_producer_id IS NULL)
    OR (origin_kind='manual_edit' AND origin_op_id IS NOT NULL
        AND origin_run_id IS NULL AND base_producer_id IS NULL)
  )
);"""


def _without_typed_values(schema: str) -> str:
    """Restore the exact logical JSON authority layout before typed storage."""

    schema = _without_review_stats(schema)
    citation = schema.find("CREATE TABLE IF NOT EXISTS citation_texts")
    if citation >= 0:
        schema = schema[:citation]
    for table, ddl in (
        ("cells", _LEGACY_CELLS),
        ("results", _LEGACY_RESULTS),
        ("edits", _LEGACY_EDITS),
        ("current_cells", _LEGACY_CURRENT_CELLS),
    ):
        schema = _replace_create_table(schema, table, ddl)
    typed_view = schema.find("CREATE VIEW IF NOT EXISTS current_cell_values")
    if typed_view >= 0:
        search_work = schema.index(
            "CREATE TABLE IF NOT EXISTS search_dirty_scopes", typed_view
        )
        schema = schema[:typed_view] + schema[search_work:]
    return (
        schema.replace("BEFORE UPDATE OF value_kind, value,", "BEFORE UPDATE OF value,")
        .replace("    OR NEW.value_kind IS NOT OLD.value_kind\n", "")
        .replace(
            "        NEW.value_kind IS NOT OLD.value_kind\n        OR NEW.value IS NOT OLD.value",
            "        NEW.value IS NOT OLD.value",
        )
    )


def _without_rowid_cell_layout(schema: str) -> str:
    """Restore the pre-repack physical DDL used by pinned fixtures."""

    schema = _without_typed_values(schema)

    cells = "  UNIQUE (row_id, column_id)\n);"
    assert schema.count(cells) == 1
    schema = schema.replace(
        cells,
        "  PRIMARY KEY (row_id, column_id)\n) WITHOUT ROWID;",
    )
    validity = "  validity TEXT NOT NULL CHECK (validity IN ('valid', 'missing', 'invalid')),\n"
    assert schema.count(validity) == 1
    schema = schema.replace(
        validity,
        validity + "  PRIMARY KEY (column_id, row_id),\n",
    )
    current_cells = (
        "  )\n);\nCREATE INDEX IF NOT EXISTS idx_current_cells_row\n"
        "  ON current_cells(row_id,column_id);\n-- CURRENT_CELLS_END"
    )
    assert schema.count(current_cells) == 1
    schema = schema.replace(
        current_cells,
        "  )\n) WITHOUT ROWID;\n"
        "CREATE INDEX IF NOT EXISTS idx_current_cells_row ON current_cells(row_id);\n"
        "-- CURRENT_CELLS_END",
    )
    return schema.replace(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_current_cells_column_row",
        "CREATE INDEX IF NOT EXISTS idx_current_cells_column_row",
    )


def _with_pre_hygiene_indexes(schema: str) -> str:
    """Restore indexes that existed in every schema fixture below."""

    assert "idx_execution_attempts_run" not in schema
    assert "idx_source_items_source_dedupe" not in schema
    assert "idx_source_runs_source ON" not in schema
    schema = schema.replace(
        "CREATE INDEX IF NOT EXISTS idx_source_runs_source_started",
        "CREATE INDEX IF NOT EXISTS idx_source_runs_source "
        "ON source_runs(source_id);\n"
        "CREATE INDEX IF NOT EXISTS idx_source_runs_source_started",
    )
    schema = schema.replace(
        "CREATE INDEX IF NOT EXISTS idx_source_items_run",
        "CREATE INDEX IF NOT EXISTS idx_source_items_source_dedupe\n"
        "  ON source_items(source_id, dedupe_key);\n"
        "CREATE INDEX IF NOT EXISTS idx_source_items_run",
    )
    return schema.replace(
        "-- ONE dispatch per run, ENFORCED.",
        "CREATE INDEX IF NOT EXISTS idx_execution_attempts_run\n"
        "  ON execution_attempts(run_id, seq DESC);\n"
        "-- ONE dispatch per run, ENFORCED.",
    )


def _restore_pre_hygiene_indexes(db: sqlite3.Connection) -> None:
    db.executescript(
        "CREATE INDEX IF NOT EXISTS idx_execution_attempts_run "
        "ON execution_attempts(run_id,seq DESC);"
        "CREATE INDEX IF NOT EXISTS idx_source_items_source_dedupe "
        "ON source_items(source_id,dedupe_key);"
        "CREATE INDEX IF NOT EXISTS idx_source_runs_source "
        "ON source_runs(source_id);"
    )


def _without_current_cells_foundation(schema: str) -> str:
    prior = schema
    for marker in (
        "BASE_CELL_PRODUCERS",
        "CELL_PRODUCER_ID",
        "CELL_COLUMN_INDEX",
        "CURRENT_CELLS",
    ):
        before, marked = prior.split(f"-- {marker}_BEGIN", 1)
        _removed, after = marked.split(f"-- {marker}_END", 1)
        prior = before + after
    return prior


def _without_import_sessions(schema: str) -> str:
    schema = schema.replace(
        "  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1) AND (active=1 OR hidden=1)),\n",
        "",
    ).replace(
        "  created_at TEXT NOT NULL DEFAULT (datetime('now'))\n);\n"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_columns_active_name\n"
        "  ON columns(sheet_id,name) WHERE active=1;",
        "  created_at TEXT NOT NULL DEFAULT (datetime('now')),\n"
        "  UNIQUE(sheet_id, name)\n);",
    )
    schema = _with_pre_hygiene_indexes(schema)
    before, marked = schema.split("-- SEARCH_INDEX_WORK_BEGIN", 1)
    _removed, after = marked.split("-- SEARCH_INDEX_WORK_END", 1)
    schema = before + after
    before, marked = schema.split("-- IMPORT_SESSIONS_BEGIN", 1)
    _removed, after = marked.split("-- IMPORT_SESSIONS_END", 1)
    return before + after


def _without_project_qa(schema: str) -> str:
    """Reconstruct the predecessor DDL for the pinned a62/a64 fixtures."""

    prior = _without_project_qa_research(schema)
    before, marked = prior.split("-- PROJECT_QA_BEGIN", 1)
    _removed, after = marked.split("-- PROJECT_QA_END", 1)
    return (before + after).replace("  review_completed_at TEXT,\n", "")


def _without_project_qa_research(schema: str) -> str:
    """Reconstruct the schema after Ask history but before research state."""

    before, marked = schema.split("-- PROJECT_QA_RESEARCH_BEGIN", 1)
    _removed, after = marked.split("-- PROJECT_QA_RESEARCH_END", 1)
    return (before + after).replace("  research_json TEXT,\n", "")


def _physically_remove_project_qa_research(db: sqlite3.Connection) -> None:
    """Restore the pre-research Ask tables after seeding with today's schema."""

    db.execute("DROP TABLE project_qa_research_operations")
    db.execute("DROP TABLE project_qa_research_runs")
    db.execute("ALTER TABLE project_qa_turns DROP COLUMN research_json")
    db.execute("ALTER TABLE project_qa_threads DROP COLUMN research_json")


def _legacy_json(value_kind: str | None, value: object) -> str | None:
    if value_kind is None:
        return None
    if value_kind == "legacy_invalid":
        return str(value)
    return json.dumps(decode_stored_value(value_kind, value), allow_nan=False)


def _restore_legacy_authorities(db: sqlite3.Connection) -> None:
    """Downgrade fresh typed fixtures before exercising historical upgrades."""

    db.execute("DROP TABLE run_review_fields")
    db.execute("DROP INDEX idx_cell_result_heads_run")
    db.execute("DROP INDEX idx_columns_current_run")
    db.execute("ALTER TABLE runs DROP COLUMN review_resolved_bundle_count")
    db.execute("ALTER TABLE runs DROP COLUMN review_bundle_count")
    db.execute("ALTER TABLE runs DROP COLUMN review_stats_ready")

    current_rows = [
        (
            row[0],
            row[1],
            _legacy_json(row[2], row[3]),
            *row[4:],
        )
        for row in db.execute(
            "SELECT column_id,row_id,value_kind,value,origin_kind,origin_op_id,"
            "origin_run_id,base_producer_id,validity FROM current_cell_values"
        )
    ]
    authority_rows: dict[str, tuple[list[str], list[tuple[object, ...]]]] = {}
    for table in ("cells", "results", "edits"):
        columns = [str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")]
        rows = []
        for stored in db.execute(f"SELECT {','.join(columns)} FROM {table}"):
            values = dict(zip(columns, stored))
            values["value"] = _legacy_json(values.pop("value_kind"), values["value"])
            legacy_columns = [column for column in columns if column != "value_kind"]
            rows.append(tuple(values[column] for column in legacy_columns))
        authority_rows[table] = (
            [column for column in columns if column != "value_kind"],
            rows,
        )

    db.execute("DROP VIEW current_cell_values")
    db.execute("DROP TABLE citation_texts")
    db.execute("DROP TABLE current_cells")
    for table in ("cells", "results", "edits"):
        db.execute(f"DROP TABLE {table}")
    db.executescript(_without_typed_values(SCHEMA))
    for table, (columns, rows) in authority_rows.items():
        if rows:
            db.executemany(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES "
                f"({','.join('?' for _ in columns)})",
                rows,
            )
    db.executemany(
        "INSERT INTO current_cells "
        "(column_id,row_id,value,origin_kind,origin_op_id,origin_run_id,"
        "base_producer_id,validity) VALUES (?,?,?,?,?,?,?,?)",
        current_rows,
    )


def _physically_remove_current_cells_foundation(db: sqlite3.Connection) -> None:
    """Restore the exact predecessor tables after seeding with today's facade."""

    db.execute("PRAGMA foreign_keys=OFF")
    _restore_legacy_authorities(db)
    db.execute("DROP TABLE import_sessions")
    _physically_remove_project_qa_research(db)
    db.execute("DROP TABLE project_qa_usage_calls")
    db.execute("DROP TABLE project_qa_citations")
    db.execute("DROP TABLE project_qa_events")
    db.execute("DROP TABLE project_qa_turns")
    db.execute("DROP TABLE project_qa_threads")
    db.execute("DROP TABLE current_cells")
    db.execute("DROP INDEX idx_cells_column")
    db.execute("ALTER TABLE cells DROP COLUMN producer_id")
    db.execute("DROP TABLE base_cell_producers")
    db.execute("ALTER TABLE runs DROP COLUMN review_completed_at")
    _restore_pre_hygiene_indexes(db)


def _a64_bundle(tmp_path):
    from frisket.engine.store.runs import RunResultStore

    path = tmp_path / "a64.frisket"
    project = Project.create(path, name="Reporting project")
    sheet = project.add_sheet("Sources")
    column = project.add_column(sheet, "source", type="text")
    rows = project.add_rows(
        sheet, [{"source": "First"}, {"source": "Second"}], {"source": column}
    )
    op = project.append_op("map.classify", label="Existing work")
    run = RunResultStore(project).start_run(op, sheet, "map.classify", total_rows=2)
    project.db.execute(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value,review_state,outcome) "
        "VALUES (?,?,?,?, 'verified', 'ok')",
        (run, rows[0], column, '"existing result"'),
    )
    project.db.commit()
    project.close()
    with sqlite3.connect(path / "project.db") as db:
        _physically_remove_current_cells_foundation(db)
        db.execute("ALTER TABLE runs DROP COLUMN consent_principal")
        db.execute("ALTER TABLE results DROP COLUMN review_note")
        db.execute("ALTER TABLE results DROP COLUMN review_decision")
        db.execute(
            "UPDATE meta SET value=? WHERE key=?", (_A64_DIGEST, SCHEMA_DIGEST_META_KEY)
        )
    return path, sheet, column, rows, run


def test_a64_bundle_migrates_without_losing_rows_cells_or_runs(tmp_path):
    path, sheet, column, rows, run = _a64_bundle(tmp_path)
    project = Project(path)
    assert project.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
    assert project.get_meta("name") == "Reporting project"
    assert project.get_values(sheet, column) == dict(zip(rows, ["First", "Second"]))
    stored = project.db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
    assert stored["total_rows"] == 2
    assert stored["consent_principal"] is None
    result_columns = {
        row[1] for row in project.db.execute("PRAGMA table_info(results)")
    }
    assert {"review_decision", "review_note"} <= result_columns
    review = project.db.execute(
        "SELECT review_state, review_decision, review_note FROM results "
        "WHERE run_id=? AND row_id=? AND column_id=?",
        (run, rows[0], column),
    ).fetchone()
    assert tuple(review) == ("verified", None, None)
    assert project.db.execute("PRAGMA foreign_key_check").fetchall() == []
    project.close()
    Project(path).close()


def test_run_review_status_migration_preserves_existing_runs(tmp_path):
    prior_digest = "frisket.schema.v1:b540a83f8325e5cbcd52fc3fac64eeb5"
    path = tmp_path / "prior-review-status.frisket"
    project = Project.create(path, name="Existing review")
    sheet = project.add_sheet("Sheet")
    op = project.append_op("map.classify")
    from frisket.engine.store.runs import RunResultStore

    run_id = RunResultStore(project).start_run(op, sheet, "map.classify")
    project.close()
    with sqlite3.connect(path / "project.db") as db:
        db.execute("PRAGMA foreign_keys=OFF")
        _restore_legacy_authorities(db)
        db.execute("DROP TABLE import_sessions")
        _physically_remove_project_qa_research(db)
        db.execute("ALTER TABLE runs DROP COLUMN review_completed_at")
        _restore_pre_hygiene_indexes(db)
        db.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (prior_digest, SCHEMA_DIGEST_META_KEY),
        )

    migrated = Project(path)
    row = migrated.db.execute(
        "SELECT review_completed_at FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert row[0] is None
    assert migrated.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
    migrated.close()


def test_unknown_prior_bundle_digest_is_not_migrated(tmp_path):
    path, *_ = _a64_bundle(tmp_path)
    _stamp(path, "frisket.schema.v1:unknown")
    with pytest.raises(BundleSchemaMismatch):
        Project(path)
    with sqlite3.connect(path / "project.db") as db:
        assert "consent_principal" not in {
            row[1] for row in db.execute("PRAGMA table_info(runs)")
        }
        assert (
            db.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
            ).fetchone()[0]
            == "frisket.schema.v1:unknown"
        )


@pytest.mark.parametrize("prior", ["a64", "a62"])
def test_interrupted_schema_stamp_rolls_back_column_addition(tmp_path, prior):
    path, sheet, column, rows, _run = (
        _a62_bundle(tmp_path) if prior == "a62" else _a64_bundle(tmp_path)
    )
    with sqlite3.connect(path / "project.db") as db:
        db.execute(
            "CREATE TRIGGER interrupt_schema_stamp BEFORE UPDATE ON meta "
            f"WHEN NEW.key='schema_digest' AND NEW.value != '{_A64_DIGEST}' BEGIN "
            "SELECT RAISE(ABORT, 'migration interrupted'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="migration interrupted"):
        Project(path)
    with sqlite3.connect(path / "project.db") as db:
        # The receipt-owner step remains committed if the second step fails.
        assert "receipt_id" in {
            row[1] for row in db.execute("PRAGMA table_info(execution_attempts)")
        }
        assert "consent_principal" not in {
            row[1] for row in db.execute("PRAGMA table_info(runs)")
        }
        assert (
            db.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
            ).fetchone()[0]
            == _A64_DIGEST
        )
        db.execute("DROP TRIGGER interrupt_schema_stamp")
    project = Project(path)
    assert project.get_values(sheet, column) == dict(zip(rows, ["First", "Second"]))
    project.close()


# Independently pinned from v0.1.1a62 (907a324c), before receipt-owned attempts.
_A62_DIGEST = "frisket.schema.v1:c59c7a43f961df588701133210d36503"


def _a62_bundle(tmp_path):
    from frisket.engine.store.runs import RunResultStore

    prior_ddl = (
        _without_project_qa(
            _without_current_cells_foundation(
                _without_rowid_cell_layout(_without_import_sessions(SCHEMA))
            )
        )
        .replace(
            "  edition_run_context TEXT,\n  consent_principal TEXT",
            "  edition_run_context TEXT",
        )
        .replace(_REVIEW_METADATA_DDL, "")
        .replace(
            "  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,\n"
            "  state TEXT NOT NULL CHECK (state IN",
            "  state TEXT NOT NULL CHECK (state IN",
        )
        .replace(
            "  UNIQUE (run_id, seq),\n  UNIQUE (receipt_id, seq),\n"
            "  CHECK (run_id IS NULL OR receipt_id IS NULL)",
            "  UNIQUE (run_id, seq)",
        )
        .replace(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_receipt_execution_attempts_one_dispatching\n"
            "  ON execution_attempts(receipt_id) WHERE state='dispatching';\n",
            "",
        )
    )
    path = tmp_path / "a62.frisket"
    project = Project.create(path, name="Reporting project")
    sheet = project.add_sheet("Sources")
    column = project.add_column(sheet, "source", type="text")
    rows = project.add_rows(
        sheet, [{"source": "First"}, {"source": "Second"}], {"source": column}
    )
    op = project.append_op("map.classify", label="Existing work")
    run = RunResultStore(project).start_run(op, sheet, "map.classify", total_rows=2)
    project.close()
    with sqlite3.connect(path / "project.db") as db:
        _physically_remove_current_cells_foundation(db)
        # No attempts yet: restore precisely the tagged prior table and indexes.
        db.execute("ALTER TABLE runs DROP COLUMN consent_principal")
        db.execute("ALTER TABLE results DROP COLUMN review_note")
        db.execute("ALTER TABLE results DROP COLUMN review_decision")
        db.execute("DROP TABLE execution_attempts")
        db.executescript(prior_ddl)
        db.execute(
            "INSERT INTO execution_attempts "
            "(id,run_id,seq,state,action_identity_hash,scope_json,created_at) "
            "VALUES ('old-attempt',?,1,'dispatching','old-action','{}','2026-09-01')",
            (run,),
        )
        db.execute(
            "INSERT INTO receipts (id,run_id,action_kind,status,body) "
            "VALUES ('old-receipt',?,'map.classify','running','{}')",
            (run,),
        )
        db.execute(
            "UPDATE meta SET value=? WHERE key=?", (_A62_DIGEST, SCHEMA_DIGEST_META_KEY)
        )
    return path, sheet, column, rows, run


def test_a62_bundle_preserves_data_and_attempts_on_upgrade(tmp_path):
    path, sheet, column, rows, run = _a62_bundle(tmp_path)
    project = Project(path)
    assert project.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
    assert project.get_meta("name") == "Reporting project"
    assert project.get_values(sheet, column) == dict(zip(rows, ["First", "Second"]))
    assert (
        project.db.execute("SELECT total_rows FROM runs WHERE id=?", (run,)).fetchone()[
            0
        ]
        == 2
    )
    assert (
        project.db.execute(
            "SELECT consent_principal FROM runs WHERE id=?", (run,)
        ).fetchone()[0]
        is None
    )
    attempt = project.db.execute("SELECT * FROM execution_attempts").fetchone()
    assert (
        attempt["id"],
        attempt["run_id"],
        attempt["receipt_id"],
        attempt["state"],
    ) == ("old-attempt", run, None, "dispatching")
    assert (
        project.db.execute(
            "SELECT run_id FROM receipts WHERE id='old-receipt'"
        ).fetchone()[0]
        == run
    )
    assert project.db.execute("PRAGMA foreign_key_check").fetchall() == []
    project.close()
    Project(path).close()


@pytest.mark.parametrize("upgraded", [False, True])
def test_receipt_attempt_constraints_match_fresh_and_upgraded_bundles(
    tmp_path, upgraded
):
    if upgraded:
        path, *_rest, run = _a62_bundle(tmp_path)
        project = Project(path)
    else:
        from frisket.engine.store.runs import RunResultStore

        project = Project.create(tmp_path / "fresh.frisket", name="Fresh")
        sheet = project.add_sheet("Rows")
        op = project.append_op("map.classify")
        run = RunResultStore(project).start_run(op, sheet, "map.classify")
    db = project.db
    db.execute(
        "INSERT INTO receipts (id,action_kind,status) VALUES ('preview','map.classify','running')"
    )

    def insert(identity, *, run_id=None, receipt_id=None, seq=1, state="created"):
        db.execute(
            "INSERT INTO execution_attempts "
            "(id,run_id,receipt_id,seq,state,action_identity_hash,scope_json,created_at) "
            "VALUES (?,?,?,?,?,'action','{}','2026-09-01')",
            (identity, run_id, receipt_id, seq, state),
        )

    insert("preview-first", receipt_id="preview", state="dispatching")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert("duplicate-seq", receipt_id="preview")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert("double-dispatch", receipt_id="preview", seq=2, state="dispatching")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert("two-owners", run_id=run, receipt_id="preview", seq=3)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert("missing-receipt", receipt_id="missing")
    insert("preview-second", receipt_id="preview", seq=2)
    insert("run-first", run_id=run, seq=9, state="created")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert("run-duplicate", run_id=run, seq=9)
    if not upgraded:
        insert("run-dispatch", run_id=run, seq=10, state="dispatching")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert("double-run-dispatch", run_id=run, seq=11, state="dispatching")
    insert("orphan-one", state="dispatching")
    insert("orphan-two", state="dispatching")
    db.execute("DELETE FROM receipts WHERE id='preview'")
    assert (
        db.execute(
            "SELECT receipt_id FROM execution_attempts WHERE id='preview-first'"
        ).fetchone()[0]
        is None
    )
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    db.commit()
    project.close()


def test_unknown_prior_digest_does_not_add_receipt_owner(tmp_path):
    path, *_ = _a62_bundle(tmp_path)
    _stamp(path, "frisket.schema.v1:unknown")
    with pytest.raises(BundleSchemaMismatch):
        Project(path)
    with sqlite3.connect(path / "project.db") as db:
        assert "consent_principal" not in {
            row[1] for row in db.execute("PRAGMA table_info(runs)")
        }
        assert "receipt_id" not in {
            row[1] for row in db.execute("PRAGMA table_info(execution_attempts)")
        }


def test_interrupted_receipt_owner_migration_rolls_back_ddl_and_stamp(tmp_path):
    path, sheet, column, rows, _run = _a62_bundle(tmp_path)
    with sqlite3.connect(path / "project.db") as db:
        db.execute(
            "CREATE TRIGGER interrupt_schema_stamp BEFORE UPDATE ON meta "
            "WHEN NEW.key='schema_digest' BEGIN "
            "SELECT RAISE(ABORT, 'migration interrupted'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="migration interrupted"):
        Project(path)
    with sqlite3.connect(path / "project.db") as db:
        assert "consent_principal" not in {
            row[1] for row in db.execute("PRAGMA table_info(runs)")
        }
        assert "receipt_id" not in {
            row[1] for row in db.execute("PRAGMA table_info(execution_attempts)")
        }
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name LIKE 'uq_receipt_execution_attempts%'"
        ).fetchall()
        assert (
            db.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
            ).fetchone()[0]
            == _A62_DIGEST
        )
        db.execute("DROP TRIGGER interrupt_schema_stamp")
    project = Project(path)
    assert project.get_values(sheet, column) == dict(zip(rows, ["First", "Second"]))
    project.close()


def _stamp(path, value: str | None) -> None:
    db = sqlite3.connect(path / "project.db")
    if value is None:
        db.execute("DELETE FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,))
    else:
        db.execute(
            "UPDATE meta SET value=? WHERE key=?", (value, SCHEMA_DIGEST_META_KEY)
        )
    db.commit()
    db.close()


def test_a_fresh_bundle_is_stamped_and_reopens(tmp_path):
    project = Project.create(tmp_path / "fresh.frisket", name="Fresh")
    assert project.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
    project.close()
    reopened = Project(tmp_path / "fresh.frisket")
    reopened.close()


def test_the_digest_moves_when_the_ddl_moves(tmp_path):
    """The stamp is DERIVED from SCHEMA, so nobody has to remember to bump a
    version number when they add a column -- the failure mode that makes
    hand-maintained schema versions worse than none."""
    assert schema_digest(SCHEMA) == SCHEMA_DIGEST
    assert schema_digest(SCHEMA + "\nCREATE TABLE later (id INTEGER);") != SCHEMA_DIGEST


def test_comments_and_whitespace_do_not_move_the_digest():
    """A typo fix in a schema comment must not invalidate every bundle."""
    assert schema_digest(SCHEMA + "\n-- a clarifying remark\n") == SCHEMA_DIGEST
    assert schema_digest(SCHEMA.replace("\n", "\n  ")) == SCHEMA_DIGEST


@pytest.mark.parametrize("stamp", ["frisket.schema.v1:0000", None])
def test_a_bundle_from_another_build_is_refused_not_opened(tmp_path, stamp):
    path = tmp_path / "stale.frisket"
    project = Project.create(path, name="Stale")
    project.close()
    _stamp(path, stamp)
    with pytest.raises(BundleSchemaMismatch) as excinfo:
        Project(path)
    message = str(excinfo.value)
    # A journalist has to be able to act on this without reading a traceback.
    assert "different build of frisket" in message
    assert "Keep the project intact" in message
    assert SCHEMA_DIGEST in message


def test_the_refusal_is_not_a_valueerror():
    """Project opens are wrapped in broad ``except ValueError`` handlers that
    mean "bad project id" and answer 404. A stale bundle quietly becoming
    "no such project" is the mangling this refusal exists to prevent."""
    assert not issubclass(BundleSchemaMismatch, ValueError)


def test_the_refusal_reaches_the_user_as_its_own_http_error(tmp_path):
    """The app-level fallback handler answers any unmapped exception with a
    sanitized ``{"detail": "Internal Server Error"}``, so without the mapping
    in ``Workspace._open_project`` the one actionable sentence never leaves
    the server."""
    root = tmp_path / "data"
    root.mkdir()
    path = root / "stale.frisket"
    Project.create(path, name="Stale").close()
    _stamp(path, "frisket.schema.v1:0000")

    workspace = Workspace(root)
    with pytest.raises(RouteError) as excinfo:
        workspace.get("stale")
    # 409, not 404: the project is really there and really the user's.
    assert excinfo.value.status_code == 409
    assert "different build of frisket" in str(excinfo.value.content)


def test_the_double_billing_backstop_exists_on_every_fresh_bundle(tmp_path):
    """``uq_execution_attempts_one_dispatching`` backstops a PROVEN
    double-billing race, and it used to be (re)created by the schema replay
    that ran on every open. With that replay gone, ``Project.create`` is the
    only thing that can put it there."""
    project = Project.create(tmp_path / "indexed.frisket", name="Indexed")
    row = project.db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='uq_execution_attempts_one_dispatching'"
    ).fetchone()
    assert row is not None
    assert "WHERE state='dispatching'" in row["sql"]
    # The attempt outlives its run, so ``run_id`` is nullable.
    run_id = [
        column
        for column in project.db.execute("PRAGMA table_info(execution_attempts)")
        if column["name"] == "run_id"
    ]
    assert run_id and run_id[0]["notnull"] == 0
    project.close()
