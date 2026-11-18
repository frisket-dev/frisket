from contextlib import closing
import pytest
from frisket.engine.store import Project
from frisket.engine.store.purge import (
    PurgedMaterialUnavailable,
    begin_purge,
    finish_purge,
    require_no_pending_purge,
    require_publication_targets,
    require_restore_available,
)


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "purge.frisket"
    Project.create(path).close()
    with closing(Project(path, maintenance=True)) as project:
        yield project


def add(project, name, value):
    sheet = project.add_sheet(name)
    column = project.add_column(sheet, "value", type="json")
    row = project.add_rows(sheet, [{"value": value}], {"value": column})[0]
    return sheet, column, row


def test_source_erasure_removes_shared_bytes_and_blocks_stale_publication(project):
    blob = project.add_blob(b"original")
    first = add(project, "first", {"blob": blob})
    second = add(project, "copy", {"blob": blob})
    purge = begin_purge(project, row_ids=[first[2]])
    with pytest.raises(PurgedMaterialUnavailable):
        require_no_pending_purge(project.db)
    with pytest.raises(PurgedMaterialUnavailable):
        require_publication_targets(project.db, row_ids=[first[2]])
    with pytest.raises(PurgedMaterialUnavailable):
        require_restore_available(project.db, row_ids=[first[2]])
    require_publication_targets(project.db, row_ids=[second[2]])
    result = finish_purge(project, purge, batch_size=1)
    assert result["blobs_removed"] == 1
    assert not (project.path / "blobs" / blob[:2] / blob).exists()
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM cells WHERE row_id=?", (first[2],)
        ).fetchone()[0]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM cells WHERE row_id=?", (second[2],)
        ).fetchone()[0]
        == 1
    )
    require_no_pending_purge(project.db)
    assert finish_purge(project, purge)["phase"] == "complete"
    final = begin_purge(project, sheet_ids=[second[0]])
    assert finish_purge(project, final)["blobs_removed"] == 0
    assert project.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_purged_rows_and_columns_are_fenced_from_restore_and_mutation(project):
    from frisket.engine.store.cell_writes import EditCellWrite, insert_edits
    from frisket.engine.store.op_log import OperationUnavailable, step_operation

    sheet, purged_column, purged_row = add(project, "purged", "secret")
    finish_purge(project, begin_purge(project, row_ids=[purged_row]))
    assert (
        project.db.execute(
            "SELECT hidden FROM rows WHERE id=?", (purged_row,)
        ).fetchone()[0]
        == 1
    )
    # Automatic navigation skips the permanently unavailable creation.
    assert project.undo() is None
    assert (
        project.db.execute(
            "SELECT hidden FROM rows WHERE id=?", (purged_row,)
        ).fetchone()[0]
        == 1
    )
    with pytest.raises(PurgedMaterialUnavailable):
        with project.db:
            project.db.execute("BEGIN IMMEDIATE")
            op_id = project.append_op("test.edit", commit=False)
            insert_edits(
                project.db,
                op_id=op_id,
                edits=[EditCellWrite(purged_row, purged_column, "late")],
            )

    # Purge fences are target-specific: a retained row still accepts a write.
    retained_sheet, retained_column, retained_row = add(project, "retained", "keep")
    with project.db:
        project.db.execute("BEGIN IMMEDIATE")
        retained_edit = project.append_op("test.edit", commit=False)
        insert_edits(
            project.db,
            op_id=retained_edit,
            edits=[EditCellWrite(retained_row, retained_column, "updated")],
        )
    assert (
        project.get_values(retained_sheet, retained_column)[retained_row] == "updated"
    )

    third_sheet = project.add_sheet("column-purge")
    doomed = project.add_column(third_sheet, "doomed")
    safe = project.add_column(third_sheet, "safe")
    third_row = project.add_rows(
        third_sheet,
        [{"doomed": "x", "safe": "y"}],
        {"doomed": doomed, "safe": safe},
    )[0]
    from frisket.engine.store.changes import start_change
    from frisket.engine.store.op_log import append_op

    with project.db:
        project.db.execute("BEGIN IMMEDIATE")
        column_op = append_op(
            project,
            "test.created_column",
            undo_info={"created_columns": [doomed]},
            commit=False,
        )
        start_change(project.db, column_op)
    finish_purge(project, begin_purge(project, column_ids=[doomed]))
    with pytest.raises(PurgedMaterialUnavailable):
        with project.db:
            project.db.execute("BEGIN IMMEDIATE")
            op_id = project.append_op("test.edit", commit=False)
            insert_edits(
                project.db,
                op_id=op_id,
                edits=[EditCellWrite(third_row, doomed, "late")],
            )
    with project.db:
        project.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(OperationUnavailable):
            step_operation(project, "revert", op_id=column_op)
    with pytest.raises(OperationUnavailable):
        with project.db:
            project.db.execute("BEGIN IMMEDIATE")
            step_operation(project, "restore", op_id=column_op)


def test_restoring_source_change_keeps_purged_cell_absent(project):
    from frisket.engine.store.cell_writes import (
        BaseCellWrite,
        create_base_cell_producer,
        replace_base_cells,
    )
    from frisket.engine.store.changes import ChangeStore
    from frisket.engine.store.op_log import step_operation

    sheet = project.add_sheet("source restore")
    column = project.add_column(sheet, "value", type="text")
    rows = project.add_rows(
        sheet, [{"value": "old one"}, {"value": "old two"}], {"value": column}
    )
    project.db.execute("BEGIN IMMEDIATE")
    op = project.append_op("source.refresh", {}, label="source refresh", commit=False)
    producer = create_base_cell_producer(project.db, stage_id=f"op:{op}", op_id=op)
    replace_base_cells(
        project.db,
        producer_id=producer,
        column_ids=[column],
        row_ids=rows,
        cells=[
            BaseCellWrite(rows[0], column, "new one"),
            BaseCellWrite(rows[1], column, "new two"),
        ],
    )
    ChangeStore(project).close(op)
    project.db.commit()

    assert project.undo() == op
    finish_purge(project, begin_purge(project, row_ids=[rows[0]]))

    project.db.execute("BEGIN IMMEDIATE")
    step_operation(project, "restore", op_id=op)
    project.db.commit()

    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM cells WHERE row_id=? AND column_id=?",
            (rows[0], column),
        ).fetchone()[0]
        == 0
    )
    values = project.get_values(sheet, column, row_ids=rows)
    assert rows[0] not in values
    assert values[rows[1]] == "new two"


def test_purged_undone_row_cannot_be_restored(project):
    from frisket.engine.store.op_log import OperationUnavailable, step_operation

    _sheet, _column, row = add(project, "restore", "secret")
    row_op = project.db.execute(
        "SELECT op_id FROM structural_changes WHERE kind='row' AND object_id=? ORDER BY op_id DESC LIMIT 1",
        (row,),
    ).fetchone()[0]
    assert project.undo() == row_op
    finish_purge(project, begin_purge(project, row_ids=[row]))
    with pytest.raises(OperationUnavailable):
        with project.db:
            project.db.execute("BEGIN IMMEDIATE")
            step_operation(project, "restore", op_id=row_op)
    assert (
        project.db.execute("SELECT hidden FROM rows WHERE id=?", (row,)).fetchone()[0]
        == 1
    )


def test_purge_keeps_independent_reapply_results_replayable(project):
    from frisket.engine.store.changes import start_change
    from frisket.engine.store.current_cells import refresh_current_cells
    from frisket.engine.store.result_generations import ResultGenerationStore
    from frisket.engine.store.runs import RunResultStore
    from helpers import run_writer_authority_fixture

    source_sheet = project.add_sheet("source")
    source_column = project.add_column(
        source_sheet, "generated", type="text", ai_generated=True
    )
    source_row = project.add_rows(
        source_sheet, [{"generated": "input"}], {"generated": source_column}
    )[0]
    source_op = project.append_op("test.run")
    runs = RunResultStore(project)
    source_run = runs.start_run(
        source_op, source_sheet, "test.run", row_ids=[source_row], total_rows=1
    )
    authority = run_writer_authority_fixture(
        project, source_run, output_column_ids={source_column}
    )
    generations = ResultGenerationStore(project)
    generations.declare(
        source_run,
        source_column,
        output_role="saved",
        compatibility_key="sha256:independent-saved-output",
        write_mode="create",
        claim_token=authority.claim_token,
    )
    runs.write_results(
        source_run,
        [
            {
                "row_id": source_row,
                "column_id": source_column,
                "value": "saved",
                "publication_effect": "publish_value",
            }
        ],
        **authority.kwargs(),
    )
    generations.seal(
        source_run,
        {source_column},
        claim_token=authority.claim_token,
        terminal_disposition="completed",
    )
    runs.finish_run(
        source_run,
        writer_attempt_id=authority.writer_attempt_id,
        claim_token=authority.claim_token,
    )
    destination = project.add_sheet("copies")
    output = project.add_column(destination, "saved", type="text", ai_generated=True)
    first, second = project.add_rows(
        destination,
        [{"saved": None}, {"saved": None}],
        {"saved": output},
    )
    copy_op = project.append_op("reapply.results")
    with project.db:
        project.db.execute("BEGIN IMMEDIATE")
        start_change(project.db, copy_op)
        project.db.executemany(
            "INSERT INTO copied_result_outcomes(op_id,row_id,column_id,source_run_id,source_row_id,source_column_id) VALUES (?,?,?,?,?,?)",
            [
                (copy_op, row_id, output, source_run, source_row, source_column)
                for row_id in (first, second)
            ],
        )
        refresh_current_cells(project.db, column_ids=[output])

    assert project.get_values(destination, output)[second] == "saved"
    finish_purge(project, begin_purge(project, row_ids=[first]))
    assert [
        int(row[0])
        for row in project.db.execute(
            "SELECT row_id FROM copied_result_outcomes WHERE op_id=?", (copy_op,)
        )
    ] == [second]
    finish_purge(project, begin_purge(project, row_ids=[source_row]))
    assert (
        project.db.execute(
            "SELECT count(*) FROM results WHERE run_id=? AND row_id=?",
            (source_run, source_row),
        ).fetchone()[0]
        == 1
    )
    assert project.get_values(destination, output)[second] == "saved"
    assert project.undo() == copy_op
    assert project.redo() == copy_op
    assert project.get_values(destination, output)[second] == "saved"
    assert first not in project.visible_row_ids(destination)


def test_real_reapply_after_source_cohort_purge_refuses_only_purged_row(project):
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store.result_generations import ResultGenerationStore
    from frisket.engine.store.runs import RunResultStore
    from helpers import run_writer_authority_fixture

    sheet = project.add_sheet("source-cohort")
    source = project.add_column(sheet, "source", type="text")
    output = project.add_column(sheet, "generated", type="text", ai_generated=True)
    first, second = project.add_rows(
        sheet,
        [{"source": "remove"}, {"source": "retain"}],
        {"source": source},
    )
    op = project.append_op("map.ask")
    runs = RunResultStore(project)
    run_id = runs.start_run(op, sheet, "map.ask", row_ids=[first, second], total_rows=2)
    authority = run_writer_authority_fixture(
        project, run_id, output_column_ids={output}
    )
    generations = ResultGenerationStore(project)
    generations.declare(
        run_id,
        output,
        output_role="answer",
        compatibility_key="sha256:source-cohort-answer-v1",
        write_mode="create",
        claim_token=authority.claim_token,
    )
    runs.write_results(
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": output,
                "value": value,
                "publication_effect": "publish_value",
            }
            for row_id, value in ((first, "saved remove"), (second, "saved retain"))
        ],
        **authority.kwargs(),
    )
    generations.seal(
        run_id,
        [output],
        claim_token=authority.claim_token,
        terminal_disposition="completed",
    )
    runs.finish_run(
        run_id,
        writer_attempt_id=authority.writer_attempt_id,
        claim_token=authority.claim_token,
    )
    # The independently retained outcome on the row being purged keeps its
    # source result alive while that source row itself is removed.
    saved_sheet = project.add_sheet("independent-copy")
    saved_column = project.add_column(
        saved_sheet, "saved", type="text", ai_generated=True
    )
    [saved_row] = project.add_rows(saved_sheet, [{}], {})
    from frisket.engine.store.changes import ChangeStore, start_change
    from frisket.engine.store.current_cells import refresh_current_cells

    saved_op = project.append_op("run.reapply", {"action_kind": "run.reapply"})
    with project.db:
        project.db.execute("BEGIN IMMEDIATE")
        start_change(project.db, saved_op)
        project.db.execute(
            "INSERT INTO copied_result_outcomes(op_id,row_id,column_id,source_run_id,source_row_id,source_column_id) VALUES (?,?,?,?,?,?)",
            (saved_op, saved_row, saved_column, run_id, first, output),
        )
        refresh_current_cells(project.db, column_ids=[saved_column])
        ChangeStore(project).close(saved_op)
    finish_purge(project, begin_purge(project, row_ids=[first]))

    def reapply(row_id: int, key: str):
        return run_action_spec(
            project,
            {
                "action_id": "run.reapply",
                "idempotency_key": key,
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet,
                    "row_ids": [row_id],
                },
                "params": {"source_run_id": run_id},
                "output_names": {"answer": "Copied"},
            },
            project_id="purge-test",
        )

    refused = reapply(first, "reapply-purged-source-row")
    assert refused.status == "failed"
    assert (
        project.db.execute(
            "SELECT count(*) FROM ops WHERE kind='run.reapply'"
        ).fetchone()[0]
        == 1
    )
    retained = reapply(second, "reapply-retained-source-row")
    assert retained.status == "completed", retained.errors
    copied = next(
        column for column in project.columns(sheet) if column["name"] == "Copied"
    )
    assert project.get_values(sheet, copied["id"]) == {second: "saved retain"}
    assert first not in project.visible_row_ids(sheet)


@pytest.mark.parametrize("scope", ["row", "column"])
def test_purge_removes_unowned_published_result_but_retains_shared_result(
    project, scope
):
    import sqlite3

    from frisket.engine.store.changes import ChangeStore, start_change
    from frisket.engine.store.current_cells import refresh_current_cells
    from frisket.engine.store.result_generations import ResultGenerationStore
    from frisket.engine.store.runs import RunResultStore
    from helpers import run_writer_authority_fixture

    sheet = project.add_sheet("published-results")
    source = project.add_column(sheet, "source", type="text")
    output = project.add_column(sheet, "generated", type="text", ai_generated=True)
    unowned, shared = project.add_rows(
        sheet,
        [{"source": "unowned"}, {"source": "shared"}],
        {"source": source},
    )
    op = project.append_op("map.ask")
    runs = RunResultStore(project)
    run_id = runs.start_run(
        op, sheet, "map.ask", row_ids=[unowned, shared], total_rows=2
    )
    authority = run_writer_authority_fixture(
        project, run_id, output_column_ids={output}
    )
    generations = ResultGenerationStore(project)
    generations.declare(
        run_id,
        output,
        output_role="answer",
        compatibility_key="sha256:purge-owned-result-v1",
        write_mode="create",
        claim_token=authority.claim_token,
    )
    runs.write_results(
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": output,
                "value": value,
                "publication_effect": "publish_value",
            }
            for row_id, value in ((unowned, "remove me"), (shared, "keep me"))
        ],
        **authority.kwargs(),
    )
    generations.seal(
        run_id,
        [output],
        claim_token=authority.claim_token,
        terminal_disposition="completed",
    )
    runs.finish_run(
        run_id,
        writer_attempt_id=authority.writer_attempt_id,
        claim_token=authority.claim_token,
    )

    # The global immutability backstop remains active without exact purge scope.
    with pytest.raises(sqlite3.IntegrityError, match="published result is immutable"):
        with project.db:
            project.db.execute(
                "DELETE FROM results WHERE run_id=? AND row_id=? AND column_id=?",
                (run_id, unowned, output),
            )

    saved_sheet = project.add_sheet("independent-owner")
    saved_column = project.add_column(
        saved_sheet, "saved", type="text", ai_generated=True
    )
    [saved_row] = project.add_rows(saved_sheet, [{}], {})
    copy_op = project.append_op("run.reapply", {"action_kind": "run.reapply"})
    with project.db:
        project.db.execute("BEGIN IMMEDIATE")
        start_change(project.db, copy_op)
        project.db.execute(
            "INSERT INTO copied_result_outcomes(op_id,row_id,column_id,source_run_id,source_row_id,source_column_id) VALUES (?,?,?,?,?,?)",
            (copy_op, saved_row, saved_column, run_id, shared, output),
        )
        refresh_current_cells(project.db, column_ids=[saved_column])
        ChangeStore(project).close(copy_op)

    selected = {"row_ids": [unowned]} if scope == "row" else {"column_ids": [output]}
    finish_purge(project, begin_purge(project, **selected))
    if scope == "column":
        assert (
            project.db.execute(
                "SELECT count(*) FROM run_output_generations WHERE column_id=?",
                (output,),
            ).fetchone()[0]
            == 0
        )
        assert project.db.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='trg_run_output_generations_delete_discarded_only'"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            with project.db:
                project.db.execute(
                    "UPDATE results SET value='changed' WHERE run_id=? AND row_id=?",
                    (run_id, shared),
                )
    assert (
        project.db.execute(
            "SELECT count(*) FROM results WHERE run_id=? AND row_id=?",
            (run_id, unowned),
        ).fetchone()[0]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT count(*) FROM results WHERE run_id=? AND row_id=?",
            (run_id, shared),
        ).fetchone()[0]
        == 1
    )

    finish_purge(project, begin_purge(project, row_ids=[shared]))
    assert (
        project.db.execute(
            "SELECT count(*) FROM results WHERE run_id=? AND row_id=?",
            (run_id, shared),
        ).fetchone()[0]
        == 1
    )
    assert project.get_values(saved_sheet, saved_column)[saved_row] == "keep me"

    finish_purge(project, begin_purge(project, row_ids=[saved_row]))
    assert (
        project.db.execute(
            "SELECT count(*) FROM results WHERE run_id=? AND row_id=?",
            (run_id, shared),
        ).fetchone()[0]
        == 0
    )


def test_invalid_scope_rolls_back_purge(project):
    add(project, "first", "hello")
    with pytest.raises(ValueError, match="Unknown row"):
        begin_purge(project, row_ids=[900])
    assert project.db.execute("SELECT COUNT(*) FROM project_purges").fetchone()[0] == 0


def test_purge_requires_explicit_maintenance_project(tmp_path):
    path = tmp_path / "online.frisket"
    with closing(Project.create(path)) as project:
        _sheet, _column, row = add(project, "first", "hello")
        with pytest.raises(ValueError, match="maintenance Project"):
            begin_purge(project, row_ids=[row])
        assert (
            project.db.execute("SELECT COUNT(*) FROM project_purges").fetchone()[0] == 0
        )


def test_interrupted_content_cleanup_resumes(project, monkeypatch):
    from frisket.engine.store import purge as module

    first = add(project, "first", "private")
    untouched = add(project, "second", "public")
    purge = begin_purge(project, column_ids=[first[1]])
    original = module._purge_files
    monkeypatch.setattr(
        module,
        "_purge_files",
        lambda *args: (_ for _ in ()).throw(OSError("interrupted")),
    )
    with pytest.raises(OSError):
        finish_purge(project, purge)
    assert (
        project.db.execute(
            "SELECT phase FROM project_purges WHERE id=?", (purge,)
        ).fetchone()[0]
        == "content"
    )
    with pytest.raises(PurgedMaterialUnavailable):
        require_no_pending_purge(project.db)
    monkeypatch.setattr(module, "_purge_files", original)
    finish_purge(project, purge)
    assert (
        project.db.execute(
            "SELECT value FROM cells WHERE row_id=?", (untouched[2],)
        ).fetchone()[0]
        == "public"
    )


def test_purge_cleans_copied_alert_text_and_only_related_trace(project):
    from frisket.engine.store.runs import RunResultStore

    first = add(project, "first", "secret")
    second = add(project, "second", "public")
    op = project.append_op("test.run")
    run = RunResultStore(project).start_run(op, first[0], "test.run")
    other_op = project.append_op("test.run")
    other_run = RunResultStore(project).start_run(other_op, second[0], "test.run")
    RunResultStore(project).finish_run(run)
    RunResultStore(project).finish_run(other_run)
    traces = project.path / "traces"
    traces.mkdir()
    (traces / f"run-{run}.jsonl.gz").write_bytes(b"secret")
    (traces / f"run-{other_run}.jsonl.gz").write_bytes(b"public")
    with project.db:
        view = project.db.execute(
            "INSERT INTO views(name,spec) VALUES ('w','{}')"
        ).lastrowid
        alert = project.db.execute(
            "INSERT INTO watches(name,view_id) VALUES ('w',?)", (view,)
        ).lastrowid
        wr = project.db.execute(
            "INSERT INTO watch_runs(watch_id,op_cursor_before,op_cursor_after) VALUES (?,0,0)",
            (alert,),
        ).lastrowid
        project.db.execute(
            "INSERT INTO watch_run_hits(run_id,sheet_id,row_id,rank,snippet) VALUES (?,?,?,1,?)",
            (wr, first[0], first[2], "secret"),
        )
        project.db.execute(
            "INSERT INTO watch_run_hits(run_id,sheet_id,row_id,rank,snippet) VALUES (?,?,?,2,?)",
            (wr, second[0], second[2], "public"),
        )
        project.db.execute(
            "UPDATE watches SET baseline_run_id=?,last_run_id=? WHERE id=?",
            (wr, wr, alert),
        )
        project.db.executemany(
            "INSERT INTO watch_seen_rows(watch_id,sheet_id,row_id,first_seen_run_id,last_seen_run_id) VALUES (?,?,?,?,?)",
            [
                (alert, first[0], first[2], wr, wr),
                (alert, second[0], second[2], wr, wr),
            ],
        )
        project.db.executemany(
            "INSERT INTO watch_row_field_state(watch_id,sheet_id,row_id,column_id,value_hash,value_json,last_observed_run_id) VALUES (?,?,?,?,?,?,?)",
            [
                (alert, first[0], first[2], first[1], "secret", '"secret"', wr),
                (alert, second[0], second[2], second[1], "public", '"public"', wr),
            ],
        )
        project.db.execute(
            "INSERT INTO notification_items(source_kind,source_ref,dedupe_key,title,summary) VALUES ('watch',json_object('watch_id',?,'run_id',?),'private','private','secret')",
            (alert, wr),
        )
        project.db.execute(
            "INSERT INTO notification_items(source_kind,source_ref,dedupe_key,title,summary) VALUES ('other','{}','public','public','unrelated')"
        )
    purge = begin_purge(project, sheet_ids=[first[0]])
    finish_purge(project, purge)
    assert not (traces / f"run-{run}.jsonl.gz").exists()
    assert (traces / f"run-{other_run}.jsonl.gz").exists()
    assert [
        r[0] for r in project.db.execute("SELECT summary FROM notification_items")
    ] == ["unrelated"]
    assert project.db.execute("SELECT count(*) FROM watch_runs").fetchone()[0] == 1
    assert project.db.execute("SELECT count(*) FROM watch_run_hits").fetchone()[0] == 1
    assert (
        project.db.execute("SELECT row_id FROM watch_run_hits").fetchone()[0]
        == second[2]
    )
    assert project.db.execute("SELECT baseline_run_id FROM watches").fetchone()[0] == wr
    assert (
        project.db.execute("SELECT row_id FROM watch_seen_rows").fetchone()[0]
        == second[2]
    )
    assert (
        project.db.execute("SELECT row_id FROM watch_row_field_state").fetchone()[0]
        == second[2]
    )
    assert project.db.execute("SELECT count(*) FROM watches").fetchone()[0] == 1


def test_purge_scrubs_only_targeted_review_judgment_and_undo_is_safe(project):
    import json

    from frisket.engine.store.review_judgments import (
        project_judgments,
        record_judgment_changes,
        result_key,
    )
    from frisket.engine.store.runs import RunResultStore

    sheet, column, row = add(project, "review", "source")
    other_row = project.add_rows(sheet, [{"value": "other source"}], {"value": column})[
        0
    ]
    run_op = project.append_op("test.run")
    runs = RunResultStore(project)
    run_id = runs.start_run(run_op, sheet, "test.run")
    runs.finish_run(run_id)
    with project.db:
        project.db.execute(
            "INSERT INTO results(run_id,row_id,column_id,value_kind,value,published) VALUES (?,?,?,'text','answer',1)",
            (run_id, row, column),
        )
    target = result_key(run_id, None, row, column)
    retained_target = result_key(run_id, None, other_row, column)
    with project.db:
        project.db.execute(
            "INSERT INTO results(run_id,row_id,column_id,value_kind,value,published) VALUES (?,?,?,'text','other answer',1)",
            (run_id, other_row, column),
        )
    review_op = project.append_op("review.verify")
    info = {
        "review_states": {target: "unreviewed", retained_target: "unreviewed"},
        "review_states_after": {target: "verified", retained_target: "verified"},
        "review_metadata": {
            target: {"decision": None, "note": None},
            retained_target: {"decision": None, "note": None},
        },
        "review_metadata_after": {
            target: {"decision": "accept", "note": "private judgment note"},
            retained_target: {"decision": "accept", "note": "retained note"},
        },
    }
    with project.db:
        project.db.execute("BEGIN IMMEDIATE")
        project.set_undo_info(review_op, info, commit=False)
        from frisket.engine.store.changes import start_change

        start_change(project.db, review_op)
        record_judgment_changes(project.db, review_op, info)
        project_judgments(project, review_op)

    finish_purge(project, begin_purge(project, row_ids=[row]))
    cleaned = json.loads(
        project.db.execute(
            "SELECT undo_info FROM ops WHERE id=?", (review_op,)
        ).fetchone()[0]
    )
    assert not any(target in cleaned.get(field, {}) for field in info)
    assert (
        project.db.execute(
            "SELECT count(*) FROM result_judgment_changes WHERE target=?", (target,)
        ).fetchone()[0]
        == 0
    )
    assert cleaned["review_metadata_after"][retained_target]["note"] == "retained note"
    assert (
        project.db.execute(
            "SELECT count(*) FROM result_judgment_changes WHERE target=?",
            (retained_target,),
        ).fetchone()[0]
        > 0
    )
    with project.db:
        project.db.execute(
            "UPDATE meta SET value=? WHERE key='op_cursor'", (str(review_op),)
        )
    assert project.undo() == review_op


def test_purged_receipt_replays_as_valid_refusal(project):
    from frisket.contracts.action import Receipt
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.store.runs import RunResultStore

    sheet, _column, row = add(project, "receipt", "secret")
    op_id = project.append_op("test.receipted")
    run_id = RunResultStore(project).start_run(op_id, sheet, "test.receipted")
    RunResultStore(project).finish_run(run_id)
    receipt_id = "receipt-purged-material-test"
    ReceiptStore(project).insert(
        Receipt(
            receipt_id=receipt_id,
            project_id=project.storage_identity,
            action_id="act-purged-test",
            action_kind="test.receipted",
            run_id=run_id,
            idempotency_key="purged-material-key",
            params_hash="sha256:fixture",
            status="completed",
            provider_use=[{"model": "test", "cost_actual": 0.23, "input_tokens": 30}],
            outputs=[{"name": "value", "ref": {"kind": "cell", "row_id": row}}],
        )
    )
    with project.db:
        project.db.execute(
            "INSERT INTO results(run_id,row_id,column_id,value_kind,value,published) VALUES (?,?,?,'text','answer',1)",
            (run_id, row, _column),
        )
    finish_purge(project, begin_purge(project, row_ids=[row]))
    replay = ReceiptStore(project).find_by_idempotency_key("purged-material-key")
    assert replay is not None
    parsed = replay.parsed()
    assert parsed.status == "failed"
    assert parsed.errors[0].code == "purged_material_unavailable"
    assert parsed.outputs == []
    assert parsed.provider_use == [
        {"model": "test", "cost_actual": 0.23, "input_tokens": 30}
    ]


def test_late_result_refusal_keeps_paid_usage(project):
    from frisket.engine.store.result_generations import ResultGenerationStore
    from frisket.engine.store.runs import RunResultStore
    from helpers import run_writer_authority_fixture

    sheet = project.add_sheet("late-provider")
    source = project.add_column(sheet, "source")
    output = project.add_column(sheet, "generated", ai_generated=True)
    row = project.add_rows(sheet, [{"source": "paid request"}], {"source": source})[0]
    op_id = project.append_op(
        "map.regex_extract", {"pattern": "(.*)", "output": "generated"}
    )
    runs = RunResultStore(project)
    run_id = runs.start_run(
        op_id, sheet, "map.regex_extract", row_ids=[row], total_rows=1
    )
    authority = run_writer_authority_fixture(
        project, run_id, output_column_ids={output}
    )
    token = authority.claim_token
    assert token is not None
    ResultGenerationStore(project).declare(
        run_id,
        output,
        output_role="match",
        compatibility_key="sha256:purge-late-response",
        write_mode="create",
        claim_token=token,
        defer_publication=True,
    )
    with project.db:
        purge_id = int(
            project.db.execute(
                "INSERT INTO project_purges(phase) VALUES ('complete')"
            ).lastrowid
        )
        project.db.execute(
            "INSERT INTO project_purge_targets(kind,object_id,purge_id) VALUES ('row',?,?)",
            (row, purge_id),
        )

    batch = [
        {
            "row_id": row,
            "column_id": output,
            "value": "provider response",
            "publication_effect": "publish_value",
            "model_calls": [
                {
                    "id": "purge-late-call",
                    "fact_version": "frisket.model-call-fact.v1",
                    "capability": "classify",
                    "engine": "fixture",
                    "provider": "fixture",
                    "provider_kind": "test",
                    "model_ids": ["fixture-model"],
                    "credential_source": "platform_key",
                    "provider_cost_usd": 0.025,
                }
            ],
        }
    ]
    with pytest.raises(PurgedMaterialUnavailable):
        runs.write_results(run_id, batch, **authority.kwargs())
    assert (
        project.db.execute(
            "SELECT count(*) FROM model_calls WHERE id='purge-late-call'"
        ).fetchone()[0]
        == 1
    )
    assert project.db.execute(
        "SELECT cost_actual FROM runs WHERE id=?", (run_id,)
    ).fetchone()[0] == pytest.approx(0.025)
    assert (
        project.db.execute(
            "SELECT count(*) FROM results WHERE run_id=? AND row_id=?",
            (run_id, row),
        ).fetchone()[0]
        == 0
    )
    generations = ResultGenerationStore(project)
    assert (
        generations.seal(
            run_id,
            [output],
            claim_token=token,
            terminal_disposition="cancelled",
        )
        == 0
    )
    assert generations.get_binding(run_id, output).state == "sealed"


def test_removed_notification_does_not_become_route_test(project):
    from frisket.engine.jobs.notifications_delivery import (
        _render_message,
        NotificationContentUnavailable,
    )

    request = {
        "digest_run_id": None,
        "notification_id": 42,
        "delivery_kind": "immediate",
    }
    with pytest.raises(NotificationContentUnavailable):
        _render_message(project, project_id="test", request=request)


def test_independent_saved_result_copy_keeps_payload_until_last_owner(project):
    from frisket.engine.store.runs import RunResultStore

    first = add(project, "first", "input")
    second = add(project, "independent", "seed")
    blob = project.add_blob(b"independent saved output")
    op = project.append_op("test.run")
    run = RunResultStore(project).start_run(op, first[0], "test.run")
    RunResultStore(project).finish_run(run)
    copy_op = project.append_op("test.reapply")
    with project.db:
        project.db.execute(
            "INSERT INTO results(run_id,row_id,column_id,value_kind,value) VALUES (?,?,?,'json',json_object('blob',?))",
            (run, first[2], first[1], blob),
        )
        project.db.execute(
            "INSERT INTO copied_result_outcomes(op_id,row_id,column_id,source_run_id,source_row_id,source_column_id) VALUES (?,?,?,?,?,?)",
            (copy_op, second[2], second[1], run, first[2], first[1]),
        )
    original = begin_purge(project, sheet_ids=[first[0]])
    assert finish_purge(project, original)["blobs_removed"] == 0
    assert (
        project.db.execute(
            "SELECT count(*) FROM results WHERE run_id=?", (run,)
        ).fetchone()[0]
        == 1
    )
    copy = begin_purge(project, sheet_ids=[second[0]])
    assert finish_purge(project, copy)["blobs_removed"] == 1
    assert project.db.execute("SELECT count(*) FROM results").fetchone()[0] == 0


def test_retrieval_purge_drops_only_selected_binding_and_unowned_text(project):
    from frisket.retrieval.store import RetrievalStore

    first = add(project, "first", "private")
    other = add(project, "other", "public")
    with RetrievalStore(project.path) as store:
        for num, coordinates in enumerate((first, other), 1):
            store.db.execute(
                "INSERT INTO sources(source_key,source_id,source_kind,version_kind,version_value,version_type) VALUES (?,?,'cell','hash',?,'text')",
                (num, str(num), str(num)),
            )
            store.db.execute(
                "INSERT INTO bindings(binding_id,source_key,sheet_id,row_id,column_id) VALUES (?,?,?,?,?)",
                (str(num), num, coordinates[0], coordinates[2], coordinates[1]),
            )
        store.db.commit()
    finish_purge(project, begin_purge(project, row_ids=[first[2]]))
    with RetrievalStore(project.path) as store:
        assert [r[0] for r in store.db.execute("SELECT binding_id FROM bindings")] == [
            "2"
        ]
        assert [r[0] for r in store.db.execute("SELECT source_id FROM sources")] == [
            "2"
        ]


def test_partial_content_batch_failure_resumes_without_touching_other_sheet(
    project, monkeypatch
):
    from frisket.engine.store import purge as module

    first = add(project, "first", "private")
    second = add(project, "second", "keep")
    identity = begin_purge(project, row_ids=[first[2]])
    original = module._delete_batches

    def interrupted(db, table, *args, **kwargs):
        original(db, table, *args, **kwargs)
        if table == "cells":
            raise OSError("stopped after an acknowledged batch")

    monkeypatch.setattr(module, "_delete_batches", interrupted)
    with pytest.raises(OSError):
        finish_purge(project, identity, batch_size=1)
    assert (
        project.db.execute(
            "SELECT phase FROM project_purges WHERE id=?", (identity,)
        ).fetchone()[0]
        == "content"
    )
    monkeypatch.setattr(module, "_delete_batches", original)
    finish_purge(project, identity, batch_size=1)
    assert (
        project.db.execute(
            "SELECT value FROM cells WHERE row_id=?", (second[2],)
        ).fetchone()[0]
        == "keep"
    )


def test_column_purge_preserves_unselected_source_baseline_fields(project):
    import json

    sheet, name, row = add(project, "incoming", "name")
    key = project.add_column(sheet, "key", type="text")
    with project.db:
        project.db.execute(
            "INSERT INTO keyed_relationships VALUES (?,?,?)",
            ("source", sheet, json.dumps([[key, "text"]])),
        )
        project.db.execute(
            "INSERT INTO keyed_observations(relationship_id,row_id,key,record) VALUES (?,?,?,?)",
            (
                "source",
                row,
                "identity",
                json.dumps({str(key): "id-1", str(name): "secret"}),
            ),
        )
    finish_purge(project, begin_purge(project, column_ids=[name]))
    assert json.loads(
        project.db.execute("SELECT record FROM keyed_observations").fetchone()[0]
    ) == {str(key): "id-1"}
    finish_purge(project, begin_purge(project, column_ids=[key]))
    assert (
        project.db.execute("SELECT count(*) FROM keyed_relationships").fetchone()[0]
        == 0
    )


def test_source_purge_scrubs_citation_text_but_preserves_materialized_child(project):
    from frisket.engine.store.evidence import (
        record_source_artifact,
        record_text_surface,
    )

    original = project.add_blob(b"private original")
    child = project.add_blob(b"independent derived file")
    source = add(project, "source", {"blob": original})
    add(project, "saved", {"blob": child})
    project.record_blob_derivation(
        derived_hash=child, source_hash=original, op="render"
    )
    artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        blob_hash=original,
        source_sheet_id=source[0],
        source_row_id=source[2],
        source_column_id=source[1],
        title="secret title",
    )
    surface = record_text_surface(
        project,
        surface_kind="cell",
        content_hash="sha256:" + "a" * 64,
        offset_unit="unicode_codepoint",
        text_sheet_id=source[0],
        text_row_id=source[2],
        text_column_id=source[1],
        surface_ref={"private": "copied diagnostic"},
    )
    with project.db:
        project.db.execute(
            "INSERT INTO source_spans(stable_id,artifact_id,span_kind,quote,snippet,text_surface_id,metadata) VALUES ('test-span',?,'page','secret quote','secret snippet',?,'{}')",
            (artifact["id"], surface["id"]),
        )
    result = finish_purge(project, begin_purge(project, sheet_ids=[source[0]]))
    assert result["blobs_removed"] == 1
    span = project.db.execute("SELECT quote,snippet FROM source_spans").fetchone()
    assert tuple(span) == (None, None)
    assert (
        project.db.execute("SELECT surface_ref_json FROM text_surfaces").fetchone()[0]
        == "{}"
    )
    assert project.db.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='trg_text_surfaces_no_update'"
    ).fetchone()
    assert project.blob_derivation_chain(child)[0]["source_hash"] == original
    with project.materialize_blob(child) as path:
        assert path.read_bytes() == b"independent derived file"


def test_active_or_unaccounted_work_must_resolve_before_purge(project):
    from frisket.engine.store.runs import RunResultStore

    first = add(project, "first", "private")
    owner = project.append_op("test.run")
    run = RunResultStore(project).start_run(owner, first[0], "test.run")
    with pytest.raises(ValueError, match="Cancel or finish"):
        begin_purge(project, sheet_ids=[first[0]])
    assert project.db.execute("SELECT count(*) FROM project_purges").fetchone()[0] == 0
    RunResultStore(project).finish_run(run)
    with project.db:
        project.db.execute(
            "INSERT INTO effect_checkpoints(id,family,group_key,unit_key,action_kind,identity,state,payload) VALUES ('checkpoint','row_effect',?,'1','test.run','fingerprint','returned','{}')",
            (str(run),),
        )
    with pytest.raises(ValueError, match="unrecorded usage"):
        begin_purge(project, sheet_ids=[first[0]])
    with project.db:
        project.db.execute(
            "UPDATE effect_checkpoints SET accounting_persisted=1,payload='{\"secret\":42}'"
        )
    finish_purge(project, begin_purge(project, sheet_ids=[first[0]]))
    checkpoint = project.db.execute(
        "SELECT id,state,accounting_persisted,payload FROM effect_checkpoints"
    ).fetchone()
    assert tuple(checkpoint) == ("checkpoint", "returned", 1, "{}")


def test_purge_source_forgets_consumed_capture_and_related_trace_only(project):
    from frisket.engine.store.consumed_sources import (
        read_consumed_source,
        store_capture,
        verify_bundle,
    )
    from frisket.engine.store.runs import RunResultStore

    source = add(project, "source", "private")
    output = add(project, "independent answer", "keep me")
    _, _, capture = read_consumed_source(project, source[0], [source[1]])
    owner = project.append_op("test.capture", {})
    run = RunResultStore(project).start_run(owner, output[0], "test.capture")
    with project.db:
        project.db.execute("BEGIN")
        bundle = store_capture(
            project,
            capture,
            owner_op_id=owner,
            output_sheet_id=output[0],
            run_id=run,
            row_id=output[2],
        )
    RunResultStore(project).finish_run(run)
    traces = project.path / "traces"
    traces.mkdir()
    trace = traces / f"run-{run}.jsonl.gz"
    trace.write_bytes(b"captured input")
    finish_purge(project, begin_purge(project, row_ids=[source[2]]), batch_size=1)
    assert not trace.exists()
    assert verify_bundle(project, bundle) == "unknown"
    assert (
        project.db.execute(
            "SELECT value FROM cells WHERE row_id=?", (output[2],)
        ).fetchone()[0]
        == "keep me"
    )


def test_purge_preserves_shared_descriptor_authority_for_other_columns(project):
    from frisket.engine.store.changes import start_change
    from frisket.engine.store.column_descriptors import (
        record_descriptor_changes,
        descriptor_identity,
    )

    sheet, selected, _ = add(project, "sheet", "secret")
    retained = project.add_column(sheet, "retained", type="number")
    info = {
        "column_formats": {str(selected): None, str(retained): None},
        "column_formats_after": {str(selected): "currency", str(retained): "percent"},
    }
    with project.db:
        project.db.execute("BEGIN")
        operation = project.append_op("column.format", commit=False)
        start_change(project.db, operation)
        project.set_undo_info(operation, info, commit=False)
        record_descriptor_changes(project.db, operation, info)
    assert descriptor_identity(project, retained)["format"] == "percent"
    finish_purge(project, begin_purge(project, column_ids=[selected]))
    assert descriptor_identity(project, retained)["format"] == "percent"


def test_normal_open_refuses_interrupted_purge_and_maintenance_can_finish(tmp_path):
    path = tmp_path / "resume.frisket"
    with closing(Project.create(path)) as original:
        sheet, column, row = add(original, "first", "private content")
    with closing(Project(path, maintenance=True)) as maintenance:
        purge = begin_purge(maintenance, row_ids=[row])
    with pytest.raises(PurgedMaterialUnavailable, match="unfinished"):
        Project(path)
    with closing(Project(path, maintenance=True)) as maintenance:
        finish_purge(maintenance, purge)
    with closing(Project(path)) as restored:
        assert restored.visible_row_ids(sheet) == []
        assert restored.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_shared_source_erases_previews_and_search_but_keeps_output_and_resumes(
    project, monkeypatch
):
    from pathlib import Path
    from frisket.engine.store.blob_backend import BlobNotFoundError
    from frisket.engine.store.evidence import record_source_artifact
    from frisket.engine.store.project_blobs import reclaim_local_blobs
    from frisket.retrieval.store import RetrievalStore

    original = project.add_blob(b"original source")
    preview = project.add_blob(b"source page preview")
    output = project.add_blob(b"independent transformed output")
    first = add(project, "source", {"blob": original})
    alias = add(project, "same bytes", {"blob": original})
    retained = add(project, "independent output", {"blob": output})
    for location in (first, alias):
        record_source_artifact(
            project,
            artifact_kind="file",
            media_type="application/pdf",
            blob_hash=original,
            source_sheet_id=location[0],
            source_row_id=location[2],
            source_column_id=location[1],
            metadata={
                "page_images": {"1": {"blob_hash": preview}},
                "captured_text": "original searchable text",
            },
        )
    with RetrievalStore(project.path) as store:
        for number, location, digest in (
            (1, first, original),
            (2, alias, original),
            (3, retained, output),
        ):
            store.db.execute(
                "INSERT OR IGNORE INTO sources(source_key,source_id,source_kind,version_kind,version_value,version_type) VALUES (?,?, 'blob','blob_digest',?,'text')",
                (number, f"blob:{digest}", digest),
            )
            source_key = store.db.execute(
                "SELECT source_key FROM sources WHERE version_value=?", (digest,)
            ).fetchone()[0]
            store.db.execute(
                "INSERT INTO bindings(binding_id,source_key,sheet_id,row_id,column_id) VALUES (?,?,?,?,?)",
                (str(number), source_key, location[0], location[2], location[1]),
            )
        store.db.commit()
    purge = begin_purge(project, row_ids=[first[2]])
    unlink = Path.unlink

    def fail_source(path, **kwargs):
        if path.name == original:
            raise OSError("source unlink interrupted")
        return unlink(path, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_source)
    with pytest.raises(OSError, match="interrupted"):
        finish_purge(project, purge, batch_size=1)
    assert (
        project.db.execute(
            "SELECT count(*) FROM cells WHERE row_id=?", (first[2],)
        ).fetchone()[0]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT count(*) FROM project_purge_blobs WHERE purge_id=?", (purge,)
        ).fetchone()[0]
        == 2
    )
    with pytest.raises(PurgedMaterialUnavailable):
        require_no_pending_purge(project.db)
    monkeypatch.setattr(Path, "unlink", unlink)
    assert finish_purge(project, purge, batch_size=1)["phase"] == "complete"
    assert project.get_values(alias[0], alias[1])[alias[2]] == {"blob": original}
    with pytest.raises(BlobNotFoundError):
        project.read_blob(original)
    with pytest.raises(BlobNotFoundError):
        project.read_blob(preview)
    assert project.read_blob(output) == b"independent transformed output"
    assert project.db.execute("SELECT count(*) FROM citation_texts").fetchone()[0] == 0
    assert (
        project.db.execute(
            "SELECT count(*) FROM source_artifacts WHERE metadata!='{}' OR blob_hash IS NOT NULL"
        ).fetchone()[0]
        == 0
    )
    with RetrievalStore(project.path) as store:
        assert [
            row[0] for row in store.db.execute("SELECT binding_id FROM bindings")
        ] == ["3"]
    assert reclaim_local_blobs(project, dry_run=False)["blobs_removed"] == 0
    assert project.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_citation_snapshot_failure_restores_immutability_and_reruns(project):
    import sqlite3
    from frisket.engine.store.evidence import record_source_artifact

    source = add(project, "source", "original text")
    record_source_artifact(
        project,
        artifact_kind="text",
        media_type="text/plain",
        source_sheet_id=source[0],
        source_row_id=source[2],
        source_column_id=source[1],
        metadata={"captured_text": "original text"},
    )
    with project.db:
        project.db.execute(
            "CREATE TRIGGER fail_citation_delete BEFORE DELETE ON citation_texts BEGIN SELECT RAISE(ABORT,'interrupted citation cleanup'); END"
        )
    purge = begin_purge(project, row_ids=[source[2]])
    with pytest.raises(sqlite3.IntegrityError, match="interrupted citation"):
        finish_purge(project, purge)
    assert project.db.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='trg_citation_texts_no_delete'"
    ).fetchone()
    assert (
        project.db.execute("SELECT text FROM citation_texts").fetchone()[0]
        == "original text"
    )
    with project.db:
        project.db.execute("DROP TRIGGER fail_citation_delete")
    finish_purge(project, purge)
    assert project.db.execute("SELECT count(*) FROM citation_texts").fetchone()[0] == 0
    assert project.db.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='trg_citation_texts_no_delete'"
    ).fetchone()


def test_resumed_source_erasure_refuses_replaced_blob_root(project):
    blob = project.add_blob(b"source")
    _, _, row = add(project, "source", {"blob": blob})
    purge = begin_purge(project, row_ids=[row])
    root = project.path / "blobs"
    outside = project.path.parent / "moved-blobs"
    root.rename(outside)
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked blob directory"):
        finish_purge(project, purge)
    assert (outside / blob[:2] / blob).read_bytes() == b"source"
    root.unlink()
    outside.rename(root)
    finish_purge(project, purge)
    assert not (root / blob[:2] / blob).exists()
