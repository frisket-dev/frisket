from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from frisket.engine.store import Project
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.execution_metadata_migration import (
    EXECUTION_METADATA_FROM_DIGEST,
    EXECUTION_METADATA_TO_DIGEST,
    migrate_execution_metadata,
)
from frisket.engine.store.runs import (
    RESULT_WARNING_MAX_CHARS,
    RESULT_WARNING_MAX_ITEMS,
    RunResultStore,
)
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY
from frisket.sdk.capture import _capture_result_warnings, capture_maprunner_facts
from frisket.sdk.maprunner import build_receipt_from_provenance
from frisket.sdk.provenance import Provenance
from helpers import run_writer_authority_fixture
from tests.engine.test_bundle_schema_fence import _remove_execution_metadata


@pytest.fixture
def warning_run(tmp_path, request):
    project = Project.create(tmp_path / "warnings.frisket", name="Warnings")
    sheet_id = project.add_sheet("Rows")
    source_id = project.add_column(sheet_id, "source")
    output_id = project.add_column(sheet_id, "generated", ai_generated=True)
    row_ids = project.add_rows(
        sheet_id, [{"source": "scan"}, {"source": "blank"}], {"source": source_id}
    )
    op_id = project.append_op("map.regex_extract", {"pattern": "(.*)"})
    store = RunResultStore(project)
    run_id = store.start_run(
        op_id, sheet_id, "map.regex_extract", row_ids=row_ids, total_rows=2
    )
    authority = run_writer_authority_fixture(
        project, run_id, output_column_ids={output_id}
    )
    generations = ResultGenerationStore(project)
    generations.declare(
        run_id,
        output_id,
        output_role="generated",
        compatibility_key="sha256:text-warning-proof",
        write_mode="create",
        claim_token=authority.claim_token,
    )
    store.write_results(
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": output_id,
                "value": "pages 1 and 3" if index == 0 else "",
                "warnings": (
                    ["Page 2 could not be read", "Page 2 could not be read"]
                    if index == 0
                    else []
                ),
                "publication_effect": "publish_value",
            }
            for index, row_id in enumerate(row_ids)
        ],
        **authority.kwargs(),
    )
    if getattr(request, "param", True):
        generations.seal(
            run_id,
            [output_id],
            claim_token=authority.claim_token,
            terminal_disposition="completed",
        )
        store.finish_run(run_id)
        store.point_column_at_run(op_id, output_id, run_id)
    try:
        yield project, sheet_id, source_id, output_id, row_ids, run_id
    finally:
        project.close()


def _facts(project, sheet_id, source_id, run_id):
    return capture_maprunner_facts(
        project,
        runner_spec={
            "action_kind": "map.regex_extract",
            "sheet_id": sheet_id,
            "input_columns": ["source"],
            "output_name": "generated",
        },
        run_id=run_id,
        params_hash="sha256:params",
        input_column_ids={"source": source_id},
        input_column_types={"source": "text"},
        output_fields=[{"name": "generated", "type": "text", "nullable": True}],
    )


def test_warnings_survive_heads_capture_receipt_and_reopen(warning_run):
    project, sheet_id, source_id, output_id, row_ids, run_id = warning_run
    facts = _facts(project, sheet_id, source_id, run_id)
    assert facts.warnings == ["Page 2 could not be read"]
    assert (facts.run_status, facts.completed_rows, facts.failed_rows) == (
        "completed",
        2,
        0,
    )
    provenance = Provenance(
        inputs=[],
        output_refs=[],
        provider_use=[],
        evidence=[],
        status="completed",
        warnings=["Existing action warning", *facts.warnings],
    )
    receipt = build_receipt_from_provenance(
        provenance,
        facts,
        receipt_id="warnings-receipt",
        project_id="project",
        action_id="map.regex_extract",
        action_kind="map.regex_extract",
        idempotency_key="same-request",
    )
    assert receipt.status == "completed"
    assert receipt.warnings == ["Existing action warning", "Page 2 could not be read"]
    assert receipt.errors == []
    assert (
        build_receipt_from_provenance(
            provenance,
            facts,
            receipt_id="warnings-receipt",
            project_id="project",
            action_id="map.regex_extract",
            action_kind="map.regex_extract",
            idempotency_key="same-request",
        ).warnings
        == receipt.warnings
    )
    path = project.path
    project.close()
    reopened = Project(path)
    try:
        heads = ResultGenerationStore(reopened).read_cell_heads(output_id, row_ids)
        assert [heads[row_id].value for row_id in row_ids] == ["pages 1 and 3", ""]
        assert heads[row_ids[0]].warnings == ("Page 2 could not be read",)
        assert heads[row_ids[1]].warnings == ()
        assert all(
            head.outcome == "ok" and head.error is None for head in heads.values()
        )
        assert _facts(reopened, sheet_id, source_id, run_id).warnings == facts.warnings
    finally:
        reopened.close()


def _restore_prior_layout(project):
    # Downgrade this fixture to the immediately preceding on-disk layout.
    _remove_execution_metadata(project.db)
    project.db.execute(
        "UPDATE meta SET value=? WHERE key=?",
        (EXECUTION_METADATA_FROM_DIGEST, SCHEMA_DIGEST_META_KEY),
    )
    project.db.commit()


def _assert_warning_guard(project, run_id, row_id, output_id):
    args = (run_id, row_id, output_id)
    project.db.execute(
        "UPDATE results SET warnings=warnings WHERE run_id=? AND row_id=? AND column_id=?",
        args,
    )
    with pytest.raises(
        sqlite3.IntegrityError, match="immutable|open applied generation"
    ):
        project.db.execute(
            "UPDATE results SET warnings='[\"Changed\"]' "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            args,
        )
    project.db.rollback()


def test_published_and_sealed_warnings_are_immutable(warning_run):
    project, _, _, output_id, row_ids, run_id = warning_run
    _assert_warning_guard(project, run_id, row_ids[0], output_id)
    # A sealed result stays protected after its head is superseded or removed.
    project.db.execute("DELETE FROM cell_result_heads WHERE column_id=?", (output_id,))
    project.db.commit()
    _assert_warning_guard(project, run_id, row_ids[0], output_id)


@pytest.mark.parametrize("warning_run", [False], indirect=True)
def test_published_warnings_are_immutable_while_generation_is_active(warning_run):
    project, _, _, output_id, row_ids, run_id = warning_run
    assert ResultGenerationStore(project).read_cell_heads(output_id, row_ids)
    _assert_warning_guard(project, run_id, row_ids[0], output_id)


def test_prior_digest_migration_preserves_history_and_installs_guards(warning_run):
    project, _, _, output_id, row_ids, run_id = warning_run
    _restore_prior_layout(project)
    before = [
        tuple(row)
        for row in project.db.execute("SELECT * FROM results ORDER BY row_id")
    ]
    heads_before = [
        tuple(row) for row in project.db.execute("SELECT * FROM cell_result_heads")
    ]
    ops_before = [
        tuple(row) for row in project.db.execute("SELECT * FROM ops ORDER BY id")
    ]
    cells_before = [
        tuple(row)
        for row in project.db.execute(
            "SELECT * FROM current_cells ORDER BY column_id,row_id"
        )
    ]
    path = project.path
    project.close()
    reopened = Project(path)
    try:
        assert EXECUTION_METADATA_TO_DIGEST == SCHEMA_DIGEST
        assert reopened.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
        after = [
            dict(row)
            for row in reopened.db.execute("SELECT * FROM results ORDER BY row_id")
        ]
        assert [
            tuple(value for key, value in row.items() if key != "warnings")
            for row in after
        ] == before
        assert [row["warnings"] for row in after] == ["[]", "[]"]
        assert [
            tuple(row) for row in reopened.db.execute("SELECT * FROM cell_result_heads")
        ] == heads_before
        assert [
            tuple(row) for row in reopened.db.execute("SELECT * FROM ops ORDER BY id")
        ] == ops_before
        assert [
            tuple(row)
            for row in reopened.db.execute(
                "SELECT * FROM current_cells ORDER BY column_id,row_id"
            )
        ] == cells_before
        migrate_execution_metadata(reopened.db, bundle_path=reopened.db_path)
        _assert_warning_guard(reopened, run_id, row_ids[0], output_id)
    finally:
        reopened.close()


def test_warning_migration_rolls_back_on_digest_stamp_failure(warning_run):
    project, *_ = warning_run
    _restore_prior_layout(project)
    project.db.execute(
        "CREATE TRIGGER interrupt_warning_upgrade BEFORE UPDATE ON meta "
        "WHEN NEW.key='schema_digest' AND NEW.value<>OLD.value "
        "BEGIN SELECT RAISE(ABORT,'migration interrupted'); END"
    )
    project.db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="migration interrupted"):
        migrate_execution_metadata(project.db, bundle_path=project.db_path)
    assert "warnings" not in {
        row[1] for row in project.db.execute("PRAGMA table_info(results)")
    }
    assert "work_progress" not in {
        row[1] for row in project.db.execute("PRAGMA table_info(runs)")
    }
    assert project.get_meta(SCHEMA_DIGEST_META_KEY) == EXECUTION_METADATA_FROM_DIGEST
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 2


@pytest.mark.parametrize("migrated", [False, True])
def test_progress_metadata_is_nullable_separate_from_inputs_and_retained(
    warning_run, migrated
):
    original, _, _, _, row_ids, run_id = warning_run
    path = original.path
    params_before = original.db.execute(
        "SELECT params FROM runs WHERE id=?", (run_id,)
    ).fetchone()[0]
    if migrated:
        _restore_prior_layout(original)
    original.close()
    project = Project(path)
    try:
        assert (
            project.db.execute(
                "SELECT work_progress FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
            is None
        )
        for invalid in ("[]", "invalid JSON", '"text"'):
            with pytest.raises(sqlite3.DatabaseError):
                project.db.execute(
                    "UPDATE runs SET work_progress=? WHERE id=?", (invalid, run_id)
                )
            project.db.rollback()
        progress = {
            "kind": "ocr_pages",
            "row_id": row_ids[0],
            "completed": 7,
            "total": 30,
        }
        project.db.execute(
            "UPDATE runs SET work_progress=? WHERE id=?", (json.dumps(progress), run_id)
        )
        project.db.commit()
        migrate_execution_metadata(project.db, bundle_path=project.db_path)
    finally:
        project.close()
    reopened = Project(path)
    try:
        row = reopened.db.execute(
            "SELECT params,work_progress FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        assert row["params"] == params_before
        assert json.loads(row["work_progress"]) == progress
    finally:
        reopened.close()


def test_warning_storage_is_bounded_and_redacts_secrets(tmp_path):
    project = Project.create(tmp_path / "bounded.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "value")
        row_id = project.add_rows(sheet_id, [{"value": "x"}], {"value": column_id})[0]
        op_id = project.append_op("map.regex_extract", {})
        store = RunResultStore(project)
        run_id = store.start_run(op_id, sheet_id, "map.regex_extract", row_ids=[row_id])
        authority = run_writer_authority_fixture(
            project, run_id, output_column_ids={column_id}
        )
        store.write_results(
            run_id,
            [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "value": "still successful",
                    "warnings": [
                        "api_key=super-secret-key",
                        "x" * 4_000,
                        *[f"warning {i}" for i in range(30)],
                    ],
                }
            ],
            **authority.kwargs(),
        )
        row = project.db.execute(
            "SELECT warnings,outcome,error FROM results WHERE run_id=?", (run_id,)
        ).fetchone()
        warnings = json.loads(row["warnings"])
        assert len(warnings) == RESULT_WARNING_MAX_ITEMS
        assert all(len(warning) <= RESULT_WARNING_MAX_CHARS for warning in warnings)
        assert "super-secret-key" not in row["warnings"]
        assert warnings[-1].endswith("additional warnings omitted")
        assert row["outcome"] == "ok" and row["error"] is None
    finally:
        project.close()


@pytest.mark.parametrize(("row_count", "warnings_per_cell"), [(101, 1), (6, 20)])
def test_terminal_warning_summary_bounds_rows_and_items(row_count, warnings_per_cell):
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE results(run_id INTEGER,row_id INTEGER,column_id INTEGER,warnings TEXT)"
        )
        db.executemany(
            "INSERT INTO results VALUES (1,?,1,?)",
            [
                (
                    row,
                    json.dumps(
                        [f"Page {row}:{item}" for item in range(warnings_per_cell)]
                    ),
                )
                for row in range(row_count)
            ],
        )
        summary = _capture_result_warnings(SimpleNamespace(db=db), 1)
    assert len(summary) == 100
    assert summary[0] == "Page 0:0"
    assert len(summary) == len(set(summary))
    assert summary[-1] == "Additional cell warnings omitted"
