from __future__ import annotations

import queue
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store import review_stats as review_stats_module
from frisket.engine.store.review_stats import (
    current_review_run_predicate,
    ensure_run_review_stats,
    mark_run_review_stats_dirty,
    rebuild_run_review_stats,
)
from frisket.engine.store.review_stats_migration import REVIEW_STATS_FROM_DIGEST
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.schema import SCHEMA_DIGEST_META_KEY
from frisket.engine.store.value_codec import encode_stored_value


def _managed_run(
    project: Project,
    *,
    sheet_id: int,
    columns: list[int],
    row_ids: list[int],
    action_kind: str = "map.extract",
) -> int:
    op_id = project.append_op(action_kind, {"fixture": "review stats"})
    store = RunResultStore(project)
    run_id = store.start_run(
        op_id,
        sheet_id,
        action_kind,
        row_ids=row_ids,
        total_rows=len(row_ids),
    )
    project.db.executemany(
        "INSERT INTO run_output_generations "
        "(run_id,column_id,output_role,compatibility_key,write_mode,state,"
        "claim_token) VALUES (?,?,?,?,?,?,?)",
        [
            (
                run_id,
                column_id,
                f"output:{column_id}",
                f"review-stats:{column_id}",
                "create",
                "active",
                f"claim:{run_id}",
            )
            for column_id in columns
        ],
    )
    project.db.executemany(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value_kind,value,outcome,publication_effect) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (
                run_id,
                row_id,
                column_id,
                *encode_stored_value(f"{run_id}:{row_id}:{column_id}"),
                "ok",
                "publish_value",
            )
            for row_id in row_ids
            for column_id in columns
        ],
    )
    project.db.executemany(
        "INSERT INTO cell_result_heads(column_id,row_id,run_id) VALUES (?,?,?) "
        "ON CONFLICT(column_id,row_id) DO UPDATE SET run_id=excluded.run_id",
        [(column_id, row_id, run_id) for row_id in row_ids for column_id in columns],
    )
    store.finish_run(run_id)
    return run_id


def _field(project: Project, run_id: int, column_id: int) -> sqlite3.Row:
    row = project.db.execute(
        "SELECT * FROM run_review_fields WHERE run_id=? AND column_id=?",
        (run_id, column_id),
    ).fetchone()
    assert row is not None
    return row


def test_review_stats_distinguish_resolved_unknown_from_human_decisions(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "review-stats.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        first = project.add_column(sheet_id, "answer", ai_generated=True)
        second = project.add_column(sheet_id, "category", ai_generated=True)
        support = project.add_column(sheet_id, "answer_confidence", ai_generated=True)
        row_ids = project.add_rows(sheet_id, [{}, {}], {})
        run_id = _managed_run(
            project,
            sheet_id=sheet_id,
            columns=[first, second, support],
            row_ids=row_ids,
        )
        store = RunResultStore(project)

        # A migrated verified result has resolved work but no invented human
        # decision. Rebuilding preserves that distinction.
        project.db.execute(
            "UPDATE results SET review_state='verified' "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            (run_id, row_ids[0], second),
        )
        mark_run_review_stats_dirty(project.db, run_id)
        rebuild_run_review_stats(project.db, run_id)
        project.db.commit()

        first_stats = _field(project, run_id, first)
        second_stats = _field(project, run_id, second)
        support_stats = _field(project, run_id, support)
        assert (first_stats["eligible_count"], first_stats["resolved_count"]) == (
            2,
            0,
        )
        assert (
            second_stats["eligible_count"],
            second_stats["resolved_count"],
            second_stats["reviewed_count"],
        ) == (2, 1, 0)
        assert support_stats["is_primary"] == 0
        assert support_stats["eligible_count"] == 0

        run = project.db.execute(
            "SELECT review_bundle_count,review_resolved_bundle_count FROM runs "
            "WHERE id=?",
            (run_id,),
        ).fetchone()
        assert tuple(run) == (2, 0)

        # The production writer separates state and metadata. Both directions
        # keep row-bundle resolution exact throughout that ordering.
        assert (
            store.set_result_review_state(
                run_id, row_ids[0], first, "verified", commit=False
            )
            == 1
        )
        assert (
            project.db.execute(
                "SELECT review_resolved_bundle_count FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            store.set_result_review_metadata(
                run_id, row_ids[0], first, "accept", None, commit=False
            )
            == 1
        )
        assert _field(project, run_id, first)["reviewed_count"] == 1

        # Undo order restores state first while the decision is still present;
        # only clearing the decision makes the item and bundle pending again.
        store.set_result_review_state(
            run_id, row_ids[0], first, "unreviewed", commit=False
        )
        assert (
            project.db.execute(
                "SELECT review_resolved_bundle_count FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
            == 1
        )
        store.set_result_review_metadata(
            run_id, row_ids[0], first, None, None, commit=False
        )
        assert (
            project.db.execute(
                "SELECT review_resolved_bundle_count FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
            == 0
        )
        assert _field(project, run_id, first)["reviewed_count"] == 0
        project.db.commit()
    finally:
        project.close()


@pytest.mark.parametrize("transition", ["state", "metadata"])
def test_review_transition_serializes_prior_state_with_counter_delta(
    tmp_path: Path, transition: str
) -> None:
    path = tmp_path / f"concurrent-{transition}.frisket"
    project = Project.create(path)
    peer: Project | None = None
    try:
        sheet_id = project.add_sheet("Rows")
        output = project.add_column(sheet_id, "answer", ai_generated=True)
        rows = project.add_rows(sheet_id, [{}, {}], {})
        run_id = _managed_run(
            project, sheet_id=sheet_id, columns=[output], row_ids=rows
        )
        peer = Project(path)
        first_store = RunResultStore(project)
        project.db.execute("BEGIN IMMEDIATE")
        if transition == "state":
            first_store.set_result_review_state(
                run_id, rows[0], output, "verified", commit=False
            )
        else:
            first_store.set_result_review_metadata(
                run_id, rows[0], output, "accept", None, commit=False
            )

        statements: queue.Queue[str] = queue.Queue()

        def competing_transition() -> int:
            assert peer is not None
            connection = peer.db

            def trace(statement: str) -> None:
                normalized = " ".join(statement.upper().split())
                if normalized == "BEGIN IMMEDIATE":
                    statements.put("begin")
                elif normalized.startswith(
                    "SELECT REVIEW_STATE,REVIEW_DECISION FROM RESULTS"
                ):
                    statements.put("select")
                elif normalized.startswith("UPDATE RESULTS SET REVIEW_"):
                    statements.put("update")

            connection.set_trace_callback(trace)
            try:
                store = RunResultStore(peer)
                if transition == "state":
                    return store.set_result_review_state(
                        run_id, rows[0], output, "verified"
                    )
                return store.set_result_review_metadata(
                    run_id, rows[0], output, "accept", None
                )
            finally:
                connection.set_trace_callback(None)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(competing_transition)
            first_relevant_statement = statements.get(timeout=5)
            # Fixed code waits here before reading the prior result. For the
            # old ordering, wait until UPDATE so its stale SELECT has finished
            # before releasing the first writer; this avoids timing guesses.
            marker = first_relevant_statement
            while marker not in {"begin", "update"}:
                marker = statements.get(timeout=5)
            project.db.commit()
            assert future.result(timeout=5) == 1

        assert first_relevant_statement == "begin"
        field = _field(project, run_id, output)
        expected_reviewed = int(transition == "metadata")
        assert (
            field["resolved_count"],
            field["reviewed_count"],
            field["accepted_count"],
        ) == (1, expected_reviewed, expected_reviewed)
        run = project.db.execute(
            "SELECT review_resolved_bundle_count FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        assert run[0] == 1
    finally:
        if project.db.in_transaction:
            project.db.rollback()
        if peer is not None:
            peer.close()
        project.close()


def test_concurrent_review_stats_ensure_rechecks_ready_under_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "concurrent-rebuild.frisket"
    project = Project.create(path)
    first: Project | None = None
    second: Project | None = None
    release_first: threading.Event | None = None
    try:
        sheet_id = project.add_sheet("Rows")
        output = project.add_column(sheet_id, "answer", ai_generated=True)
        rows = project.add_rows(sheet_id, [{}, {}], {})
        run_id = _managed_run(
            project, sheet_id=sheet_id, columns=[output], row_ids=rows
        )
        first = Project(path)
        second = Project(path)
        mark_run_review_stats_dirty(project.db, run_id)
        project.db.commit()

        entered = queue.Queue()
        release_first = threading.Event()
        calls = 0
        calls_lock = threading.Lock()
        original_rebuild = review_stats_module.rebuild_run_review_stats

        def counted_rebuild(db: sqlite3.Connection, target_run_id: int) -> None:
            nonlocal calls
            with calls_lock:
                calls += 1
                invocation = calls
            entered.put(f"rebuild:{invocation}")
            assert release_first.wait(timeout=5)
            original_rebuild(db, target_run_id)

        monkeypatch.setattr(
            review_stats_module, "rebuild_run_review_stats", counted_rebuild
        )

        def first_ensure() -> None:
            assert first is not None
            ensure_run_review_stats(first.db, run_id)

        def second_ensure() -> None:
            assert second is not None
            connection = second.db
            connection.set_trace_callback(
                lambda statement: (
                    entered.put("begin")
                    if statement.strip().upper() == "BEGIN IMMEDIATE"
                    else None
                )
            )
            try:
                ensure_run_review_stats(connection, run_id)
            finally:
                connection.set_trace_callback(None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first_ensure)
            assert entered.get(timeout=5) == "rebuild:1"
            second_future = pool.submit(second_ensure)
            second_marker = entered.get(timeout=5)
            release_first.set()
            first_future.result(timeout=5)
            second_future.result(timeout=5)

        assert second_marker == "begin"
        assert calls == 1
    finally:
        if release_first is not None:
            release_first.set()
        if first is not None:
            first.close()
        if second is not None:
            second.close()
        project.close()


def test_ready_review_stats_ensure_does_not_take_write_lock(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "ready-read.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        output = project.add_column(sheet_id, "answer", ai_generated=True)
        rows = project.add_rows(sheet_id, [{}], {})
        run_id = _managed_run(
            project, sheet_id=sheet_id, columns=[output], row_ids=rows
        )
        statements: list[str] = []
        project.db.set_trace_callback(statements.append)
        try:
            ensure_run_review_stats(project.db, run_id)
        finally:
            project.db.set_trace_callback(None)

        assert not project.db.in_transaction
        assert all(
            statement.strip().upper() != "BEGIN IMMEDIATE" for statement in statements
        )
    finally:
        project.close()


def test_primary_membership_and_totals_survive_rename_and_partial_replacement(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "stable-review-run.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        first = project.add_column(sheet_id, "answer", ai_generated=True)
        second = project.add_column(sheet_id, "category", ai_generated=True)
        support = project.add_column(sheet_id, "answer_confidence", ai_generated=True)
        row_ids = project.add_rows(sheet_id, [{}, {}], {})
        original = _managed_run(
            project,
            sheet_id=sheet_id,
            columns=[first, second, support],
            row_ids=row_ids,
        )
        original_totals = tuple(
            project.db.execute(
                "SELECT review_bundle_count,review_resolved_bundle_count "
                "FROM runs WHERE id=?",
                (original,),
            ).fetchone()
        )

        project.db.execute(
            "UPDATE columns SET name='renamed_primary_looking' WHERE id=?", (support,)
        )
        mark_run_review_stats_dirty(project.db, original)
        rebuild_run_review_stats(project.db, original)
        frozen_support = _field(project, original, support)
        assert frozen_support["column_name"] == "answer_confidence"
        assert frozen_support["is_primary"] == 0

        replacement = _managed_run(
            project,
            sheet_id=sheet_id,
            columns=[first],
            row_ids=row_ids,
        )
        # Named-result columns can be hidden implementation details while
        # their outputs remain current and reviewable.
        project.db.execute(
            "UPDATE columns SET hidden=1 WHERE id IN (?,?)", (second, support)
        )
        predicate = current_review_run_predicate("candidate")
        still_current = project.db.execute(
            f"SELECT 1 FROM runs candidate WHERE candidate.id=? AND {predicate}",
            (original,),
        ).fetchone()
        assert still_current is not None
        assert (
            tuple(
                project.db.execute(
                    "SELECT review_bundle_count,review_resolved_bundle_count "
                    "FROM runs WHERE id=?",
                    (original,),
                ).fetchone()
            )
            == original_totals
        )

        _managed_run(
            project,
            sheet_id=sheet_id,
            columns=[second, support],
            row_ids=row_ids,
        )
        no_longer_current = project.db.execute(
            f"SELECT 1 FROM runs candidate WHERE candidate.id=? AND {predicate}",
            (original,),
        ).fetchone()
        assert no_longer_current is None
        assert (
            project.db.execute(
                f"SELECT 1 FROM runs candidate WHERE candidate.id=? AND {predicate}",
                (replacement,),
            ).fetchone()
            is not None
        )
    finally:
        project.close()


def _downgrade_review_stats_schema(path: Path) -> None:
    db = sqlite3.connect(path / "project.db")
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DROP TABLE run_review_fields")
    db.execute("DROP INDEX idx_cell_result_heads_run")
    db.execute("DROP INDEX idx_columns_current_run")
    db.execute("ALTER TABLE runs DROP COLUMN review_resolved_bundle_count")
    db.execute("ALTER TABLE runs DROP COLUMN review_bundle_count")
    db.execute("ALTER TABLE runs DROP COLUMN review_stats_ready")
    db.execute(
        "UPDATE meta SET value=? WHERE key=?",
        (REVIEW_STATS_FROM_DIGEST, SCHEMA_DIGEST_META_KEY),
    )
    db.commit()
    db.close()


def test_review_stats_migration_backfills_history_and_rolls_back_as_one_unit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "migration.frisket"
    project = Project.create(path)
    sheet_id = project.add_sheet("Rows")
    output = project.add_column(sheet_id, "answer", ai_generated=True)
    rows = project.add_rows(sheet_id, [{}, {}], {})
    run_id = _managed_run(project, sheet_id=sheet_id, columns=[output], row_ids=rows)
    project.db.execute(
        "UPDATE results SET review_state='verified',review_decision='accept' "
        "WHERE run_id=? AND row_id=? AND column_id=?",
        (run_id, rows[0], output),
    )
    project.db.commit()
    project.close()
    _downgrade_review_stats_schema(path)

    blocker = sqlite3.connect(path / "project.db")
    blocker.execute(
        "CREATE TRIGGER interrupt_review_stats_stamp BEFORE UPDATE ON meta "
        "WHEN NEW.key='schema_digest' BEGIN "
        "SELECT RAISE(ABORT,'review stats migration interrupted'); END"
    )
    blocker.commit()
    blocker.close()
    with pytest.raises(sqlite3.IntegrityError, match="migration interrupted"):
        Project(path)
    unchanged = sqlite3.connect(path / "project.db")
    assert "review_stats_ready" not in {
        row[1] for row in unchanged.execute("PRAGMA table_info(runs)")
    }
    assert (
        unchanged.execute(
            "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
        ).fetchone()[0]
        == REVIEW_STATS_FROM_DIGEST
    )
    unchanged.execute("DROP TRIGGER interrupt_review_stats_stamp")
    unchanged.commit()
    unchanged.close()

    migrated = Project(path)
    try:
        totals = migrated.db.execute(
            "SELECT review_stats_ready,review_bundle_count,"
            "review_resolved_bundle_count FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert tuple(totals) == (1, 2, 1)
        field = _field(migrated, run_id, output)
        assert (
            field["eligible_count"],
            field["reviewed_count"],
            field["resolved_count"],
        ) == (2, 1, 1)
    finally:
        migrated.close()


def test_migrated_running_run_remains_dirty_for_old_worker_completion(
    tmp_path: Path,
) -> None:
    path = tmp_path / "migration-running.frisket"
    project = Project.create(path)
    sheet_id = project.add_sheet("Rows")
    output = project.add_column(sheet_id, "answer", ai_generated=True)
    rows = project.add_rows(sheet_id, [{}, {}], {})
    op_id = project.append_op("map", {"fixture": "running migration"})
    store = RunResultStore(project)
    run_id = store.start_run(
        op_id,
        sheet_id,
        "map.extract",
        row_ids=rows,
        total_rows=len(rows),
    )
    project.db.execute(
        "INSERT INTO run_output_generations "
        "(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            run_id,
            output,
            "output:answer",
            "migration:answer",
            "create",
            "active",
            "claim",
        ),
    )
    project.db.execute(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value_kind,value,outcome,publication_effect) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            run_id,
            rows[0],
            output,
            *encode_stored_value("first"),
            "ok",
            "publish_value",
        ),
    )
    project.db.execute(
        "INSERT INTO cell_result_heads(column_id,row_id,run_id) VALUES (?,?,?)",
        (output, rows[0], run_id),
    )
    project.db.commit()
    project.close()
    _downgrade_review_stats_schema(path)

    migrated = Project(path)
    try:
        ready = migrated.db.execute(
            "SELECT review_stats_ready FROM runs WHERE id=?", (run_id,)
        ).fetchone()[0]
        assert ready == 0

        # A worker opened before migration does not know about the new dirty
        # flag. Its remaining writes and direct terminal status update must
        # therefore leave the migrated run eligible for a read-time rebuild.
        old_worker = sqlite3.connect(path / "project.db")
        try:
            old_worker.execute("PRAGMA foreign_keys=ON")
            old_worker.execute(
                "INSERT INTO results "
                "(run_id,row_id,column_id,value_kind,value,outcome,publication_effect) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    rows[1],
                    output,
                    *encode_stored_value("second"),
                    "ok",
                    "publish_value",
                ),
            )
            old_worker.execute(
                "INSERT INTO cell_result_heads(column_id,row_id,run_id) VALUES (?,?,?)",
                (output, rows[1], run_id),
            )
            old_worker.execute(
                "UPDATE runs SET status='completed',finished_at=datetime('now') "
                "WHERE id=?",
                (run_id,),
            )
            old_worker.commit()
        finally:
            old_worker.close()

        assert migrated.refresh_pending_review_summary() == 2
        totals = migrated.db.execute(
            "SELECT review_stats_ready,review_bundle_count,"
            "review_resolved_bundle_count FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert tuple(totals) == (1, 2, 0)
    finally:
        migrated.close()
