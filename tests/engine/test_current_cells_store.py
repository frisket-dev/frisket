from __future__ import annotations

import sqlite3

import pytest

from helpers import run_writer_authority_fixture, write_claimed_test_results

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store.cell_writes import (
    BaseCellWrite,
    EditCellWrite,
    bind_base_cell_producer,
    create_base_cell_producer,
    discard_pending_base_cell_producer,
    initialize_base_cells,
    insert_edits,
    remove_base_cells,
    replace_base_cells,
)
from frisket.engine.store.current_cells import (
    rebuild_current_cells,
    refresh_current_cell_pairs,
    refresh_current_cells,
)
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.transactions import write_transaction


def _project_sheet(make_project):
    project = make_project("project")
    sheet_id = project.add_sheet("Sheet")
    column_id = project.add_column(sheet_id, "value")
    row_ids = project.add_rows(sheet_id, [{}, {}, {}], {})
    return project, sheet_id, column_id, row_ids


def test_base_and_edit_sinks_are_transactional_and_preserve_source_ref_shape(
    make_project,
) -> None:
    project, _sheet_id, column_id, row_ids = _project_sheet(make_project)
    with pytest.raises(RuntimeError, match="caller transaction"):
        create_base_cell_producer(project.db, stage_id="op:1", op_id=1)

    project.db.execute("BEGIN")
    op_1 = project.append_change("source.write", commit=False)
    producer_id = create_base_cell_producer(
        project.db, stage_id=f"op:{op_1}", op_id=op_1
    )
    assert (
        create_base_cell_producer(project.db, stage_id=f"op:{op_1}", op_id=op_1)
        == producer_id
    )
    assert (
        initialize_base_cells(
            project.db,
            producer_id=producer_id,
            cells=[
                BaseCellWrite(row_ids[0], column_id, {"nested": True}),
                BaseCellWrite(row_ids[1], column_id, None),
            ],
        )
        == 2
    )
    source = project.db.execute(
        "SELECT * FROM current_cells WHERE column_id=? AND row_id=?",
        (column_id, row_ids[0]),
    ).fetchone()
    assert source["origin_kind"] == "source_cell"
    assert source["origin_op_id"] is None
    assert source["origin_run_id"] is None
    assert source["base_producer_id"] == producer_id
    explicit_null = project.db.execute(
        "SELECT value,origin_kind FROM current_cell_values "
        "WHERE column_id=? AND row_id=?",
        (column_id, row_ids[1]),
    ).fetchone()
    assert tuple(explicit_null) == (None, "source_cell")

    op_2 = project.append_change("edit", commit=False)
    insert_edits(
        project.db, op_id=op_2, edits=[EditCellWrite(row_ids[0], column_id, None)]
    )
    edited = project.db.execute(
        "SELECT * FROM current_cell_values WHERE column_id=? AND row_id=?",
        (column_id, row_ids[0]),
    ).fetchone()
    assert tuple(
        edited[key] for key in ("value", "origin_kind", "origin_op_id", "origin_run_id")
    ) == (None, "manual_edit", op_2, None)

    project.db.rollback()
    assert project.db.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM current_cells").fetchone()[0] == 0


def test_pending_producer_binds_once_and_abort_discards_only_unreferenced(
    make_project,
) -> None:
    project, sheet_id, column_id, row_ids = _project_sheet(make_project)
    project.db.execute("BEGIN")
    # Only StreamingImportSession inserts hidden staging sheets; add_sheet has no hidden flag.
    project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (sheet_id,))
    producer_id = create_base_cell_producer(project.db, stage_id="stream:receipt-1")
    initialize_base_cells(
        project.db,
        producer_id=producer_id,
        cells=[BaseCellWrite(row_ids[0], column_id, "staged")],
    )
    with pytest.raises(ValueError, match="referenced"):
        discard_pending_base_cell_producer(project.db, producer_id)
    op_1 = project.append_change("source.write", commit=False)
    bind_base_cell_producer(project.db, producer_id, op_id=op_1)
    project.db.execute("UPDATE sheets SET hidden=0 WHERE id=?", (sheet_id,))
    bind_base_cell_producer(project.db, producer_id, op_id=op_1)
    op_2 = project.append_change("source.write", commit=False)
    with pytest.raises(ValueError, match="already bound"):
        bind_base_cell_producer(project.db, producer_id, op_id=op_2)
    project.db.rollback()

    with write_transaction(project.db):
        aborted = create_base_cell_producer(project.db, stage_id="stream:receipt-2")
        discard_pending_base_cell_producer(project.db, aborted)
        discard_pending_base_cell_producer(project.db, aborted)
    assert (
        project.db.execute(
            "SELECT 1 FROM base_cell_producers WHERE id=?", (aborted,)
        ).fetchone()
        is None
    )


def test_sinks_reject_unknown_authority_and_cross_sheet_targets(make_project) -> None:
    project, _sheet_id, column_id, row_ids = _project_sheet(make_project)
    other_sheet = project.add_sheet("Other")
    other_column = project.add_column(other_sheet, "other")

    with write_transaction(project.db):
        with pytest.raises(ValueError, match="unknown producing operation"):
            create_base_cell_producer(project.db, stage_id="op:404", op_id=404)
        with pytest.raises(ValueError, match="unknown base-cell producer"):
            initialize_base_cells(
                project.db,
                producer_id=404,
                cells=[BaseCellWrite(row_ids[0], column_id, "value")],
            )
        with pytest.raises(ValueError, match="unknown producing operation"):
            insert_edits(
                project.db,
                op_id=404,
                edits=[EditCellWrite(row_ids[0], column_id, "value")],
            )

        pending = create_base_cell_producer(project.db, stage_id="stream:visible")
        with pytest.raises(ValueError, match="hidden staging sheet"):
            initialize_base_cells(
                project.db,
                producer_id=pending,
                cells=[BaseCellWrite(row_ids[0], column_id, "not hidden")],
            )

        op_1 = project.append_change("source.write", commit=False)
        producer = create_base_cell_producer(
            project.db, stage_id=f"op:{op_1}", op_id=op_1
        )
        with pytest.raises(ValueError, match="same sheet"):
            initialize_base_cells(
                project.db,
                producer_id=producer,
                cells=[BaseCellWrite(row_ids[0], other_column, "wrong")],
            )
        with pytest.raises(ValueError, match="same sheet"):
            insert_edits(
                project.db,
                op_id=op_1,
                edits=[EditCellWrite(row_ids[0], other_column, "wrong")],
            )
        # Simulates a mid-transaction discarded op (only pdf_split sets status='discarded').
        project.db.execute("UPDATE ops SET status='discarded' WHERE id=?", (op_1,))
        with pytest.raises(ValueError, match="not bound to an applied operation"):
            initialize_base_cells(
                project.db,
                producer_id=producer,
                cells=[BaseCellWrite(row_ids[0], column_id, "late")],
            )


def test_projection_preserves_explicit_result_null_error_and_edit_clear_heads(
    make_project,
) -> None:
    project = make_project("project")
    sheet_id = project.add_sheet("Sheet")
    column_id = project.add_column(sheet_id, "value")
    row_ids = project.add_rows(
        sheet_id,
        [{"value": "old-null"}, {"value": "old-error"}, {"value": "old-clear"}],
        {"value": column_id},
    )
    runs = RunResultStore(project)
    null_run = runs.start_run(
        project.append_change("map.test", {}),
        sheet_id,
        "map.test",
        row_ids=[row_ids[0]],
        total_rows=1,
    )
    write_claimed_test_results(
        project,
        null_run,
        [{"row_id": row_ids[0], "column_id": column_id, "value": None}],
    )
    runs.finish_run(null_run)

    error_run = runs.start_run(
        project.append_change("map.test", {}),
        sheet_id,
        "map.test",
        row_ids=[row_ids[1]],
        total_rows=1,
    )
    write_claimed_test_results(
        project,
        error_run,
        [
            {
                "row_id": row_ids[1],
                "column_id": column_id,
                "value": None,
                "error": "failed",
                "outcome": "model_error",
            }
        ],
    )
    runs.finish_run(error_run)

    project.apply_edits([{"row_id": row_ids[2], "column_id": column_id, "value": None}])
    with write_transaction(project.db):
        refresh_current_cells(project.db, column_ids=[column_id])

    projected = {
        int(row["row_id"]): (row["value"], row["origin_kind"])
        for row in project.db.execute(
            "SELECT row_id,value,origin_kind FROM current_cell_values ORDER BY row_id"
        )
    }
    assert projected == {
        row_ids[0]: (None, "run_result"),
        row_ids[1]: (None, "run_result"),
        row_ids[2]: (None, "manual_edit"),
    }
    assert dict(
        project.db.execute("SELECT row_id,outcome FROM current_cell_values")
    ) == {
        row_ids[0]: "completed",
        row_ids[1]: "failed",
        row_ids[2]: "completed",
    }


def test_edit_outranks_later_replacement_unless_reject_clear_or_retyped(
    make_project,
) -> None:
    project, sheet_id, column_id, row_ids = _project_sheet(make_project)
    row_ids = row_ids[:2]
    runs = RunResultStore(project)
    base_run = runs.start_run(
        project.append_change("map.test", {}),
        sheet_id,
        "map.test",
        row_ids=row_ids,
        total_rows=2,
    )
    write_claimed_test_results(
        project,
        base_run,
        [
            {"row_id": row_ids[0], "column_id": column_id, "value": "base"},
            {"row_id": row_ids[1], "column_id": column_id, "value": "base"},
        ],
    )
    runs.finish_run(base_run)

    project.apply_edits(
        [{"row_id": row_ids[0], "column_id": column_id, "value": "old edit"}]
    )
    rejected = run_action_spec(
        project,
        {
            "action_id": "review.decision",
            "scope": {"kind": "project"},
            "params": {
                "run_id": base_run,
                "row_id": row_ids[1],
                "column_id": column_id,
                "decision": "reject_clear",
            },
            "idempotency_key": "reject-clear",
        },
        project_id="project",
    )
    assert rejected.status == "completed", rejected.errors

    replace_run = runs.start_run(
        project.append_change("map.test", {"replace_existing": True}),
        sheet_id,
        "map.test",
        row_ids=row_ids,
        total_rows=2,
    )
    write_claimed_test_results(
        project,
        replace_run,
        [
            {"row_id": row_ids[0], "column_id": column_id, "value": "replacement"},
            {"row_id": row_ids[1], "column_id": column_id, "value": "replacement two"},
        ],
    )
    runs.finish_run(replace_run)
    with write_transaction(project.db):
        refresh_current_cells(project.db, column_ids=[column_id])
    current = "SELECT row_id,value,origin_kind FROM current_cell_values ORDER BY row_id"
    assert [tuple(row) for row in project.db.execute(current)] == [
        (row_ids[0], "old edit", "manual_edit"),
        (row_ids[1], "replacement two", "run_result"),
    ]

    edit_after = project.apply_edits(
        [{"row_id": row_ids[0], "column_id": column_id, "value": "after"}]
    )
    assert tuple(
        project.db.execute(
            "SELECT value,origin_kind,origin_op_id FROM current_cell_values "
            "WHERE column_id=? AND row_id=?",
            (column_id, row_ids[0]),
        ).fetchone()
    ) == ("after", "manual_edit", edit_after)

    # A replacement that changes the column descriptor supersedes every edit.
    retype_run = runs.start_run(
        project.append_change("map.test", {"replace_existing": True}),
        sheet_id,
        "map.test",
        row_ids=[row_ids[0]],
        total_rows=1,
    )
    authority = run_writer_authority_fixture(
        project, retype_run, output_column_ids={column_id}
    )
    assert authority.claim_token is not None
    generations = ResultGenerationStore(project)
    generations.declare(
        retype_run,
        column_id,
        output_role="value",
        compatibility_key="sha256:retyped",
        write_mode="replace_scope",
        claim_token=authority.claim_token,
        target_descriptor={"type": "text", "format": "plain", "semantic_type": None},
    )
    runs.write_results(
        retype_run,
        [
            {
                "row_id": row_ids[0],
                "column_id": column_id,
                "value": "retyped",
                "publication_effect": "publish_value",
            }
        ],
        **authority.kwargs(),
    )
    generations.seal(
        retype_run,
        {column_id},
        claim_token=authority.claim_token,
        terminal_disposition="completed",
    )
    project.db.commit()
    OutputColumnClaimStore(project).finish_current_writer(
        run_id=retype_run,
        writer_attempt_id=authority.writer_attempt_id,
        claim_token=authority.claim_token,
        attempt_state="effected",
    )
    runs.finish_run(retype_run)
    with write_transaction(project.db):
        refresh_current_cells(project.db, column_ids=[column_id])
    assert [tuple(row) for row in project.db.execute(current)] == [
        (row_ids[0], "retyped", "run_result"),
        (row_ids[1], "replacement two", "run_result"),
    ]


def test_replace_remove_and_rebuild_share_the_same_projector(make_project) -> None:
    project, _sheet_id, column_id, row_ids = _project_sheet(make_project)
    with write_transaction(project.db):
        op_1 = project.append_change("source.write", commit=False)
        first = create_base_cell_producer(project.db, stage_id=f"op:{op_1}", op_id=op_1)
        initialize_base_cells(
            project.db,
            producer_id=first,
            cells=[
                BaseCellWrite(row_ids[0], column_id, "first"),
                BaseCellWrite(row_ids[1], column_id, "keep"),
            ],
        )
    initial_revisions = {
        int(row["row_id"]): int(row["retrieval_revision"])
        for row in project.db.execute(
            "SELECT row_id,retrieval_revision FROM current_cells "
            "WHERE column_id=? ORDER BY row_id",
            (column_id,),
        )
    }
    with write_transaction(project.db):
        op_2 = project.append_change("source.write", commit=False)
        second = create_base_cell_producer(
            project.db, stage_id=f"op:{op_2}", op_id=op_2
        )
        replace_base_cells(
            project.db,
            producer_id=second,
            column_ids=[column_id],
            row_ids=[row_ids[0]],
            cells=[BaseCellWrite(row_ids[0], column_id, "second")],
        )
    replaced = project.db.execute(
        "SELECT value,base_producer_id FROM current_cell_values WHERE row_id=?",
        (row_ids[0],),
    ).fetchone()
    assert replaced["value"] == "second"
    second_version = replaced["base_producer_id"]
    assert second_version != second
    assert (
        project.db.execute(
            "SELECT op_id FROM base_cell_producers WHERE id=?", (second_version,)
        ).fetchone()[0]
        == op_2
    )
    assert (
        project.db.execute(
            "SELECT value FROM cells WHERE row_id=? AND column_id=? AND producer_id=?",
            (row_ids[0], column_id, first),
        ).fetchone()[0]
        == "first"
    )
    replaced_revisions = {
        int(row["row_id"]): int(row["retrieval_revision"])
        for row in project.db.execute(
            "SELECT row_id,retrieval_revision FROM current_cells "
            "WHERE column_id=? ORDER BY row_id",
            (column_id,),
        )
    }
    assert replaced_revisions[row_ids[0]] > initial_revisions[row_ids[0]]
    assert replaced_revisions[row_ids[1]] == initial_revisions[row_ids[1]]

    with write_transaction(project.db):
        replace_base_cells(
            project.db,
            producer_id=second,
            column_ids=[column_id],
            row_ids=[row_ids[0]],
            cells=[BaseCellWrite(row_ids[0], column_id, "third")],
        )
    same_producer_revision = int(
        project.db.execute(
            "SELECT retrieval_revision FROM current_cells "
            "WHERE column_id=? AND row_id=?",
            (column_id, row_ids[0]),
        ).fetchone()[0]
    )
    assert same_producer_revision > replaced_revisions[row_ids[0]]
    assert (
        project.db.execute(
            "SELECT value FROM cells WHERE row_id=? AND column_id=? AND producer_id=?",
            (row_ids[0], column_id, second_version),
        ).fetchone()[0]
        == "second"
    )
    assert (
        project.db.execute(
            "SELECT base_producer_id FROM current_cell_values "
            "WHERE row_id=? AND column_id=?",
            (row_ids[0], column_id),
        ).fetchone()[0]
        != second_version
    )

    # Rebuilding the disposable cache preserves identity/value, while its
    # search invalidation token may advance independently of either.
    def current():
        return [
            tuple(row)
            for row in project.db.execute(
                "SELECT row_id,value,base_producer_id FROM current_cell_values "
                "ORDER BY row_id"
            )
        ]

    before_repair = current()
    with write_transaction(project.db):
        refresh_current_cells(project.db, column_ids=[column_id])
    assert current() == before_repair
    with write_transaction(project.db):
        assert rebuild_current_cells(project.db) == 2
    assert current() == before_repair
    with write_transaction(project.db):
        assert (
            remove_base_cells(
                project.db,
                producer_id=second,
                column_ids=[column_id],
                row_ids=[row_ids[1]],
            )
            == 1
        )
    cleared = current()
    assert [(row[0], row[1]) for row in cleared] == [
        (row_ids[0], "third"),
        (row_ids[1], None),
    ]
    # Removal is a new explicit-null contribution, not physical deletion of
    # history. Reverting its owning Change restores both sources.
    assert project.undo() == op_2
    assert current() == [(row_ids[0], "first", first), (row_ids[1], "keep", first)]
    assert project.redo() == op_2
    assert current() == cleared
    # Simulates a cleared disposable projection cache so rebuild_current_cells must reconstruct it.
    with write_transaction(project.db):
        project.db.execute("DELETE FROM current_cells")
        assert rebuild_current_cells(project.db) == 2
    assert current() == cleared


def test_old_source_continuation_preserves_newer_edit_and_immutable_versions(
    make_project,
) -> None:
    project, _sheet_id, column_id, row_ids = _project_sheet(make_project)
    with write_transaction(project.db):
        op_1 = project.append_change("source.write", commit=False)
        producer = create_base_cell_producer(
            project.db, stage_id=f"op:{op_1}", op_id=op_1
        )
        initialize_base_cells(
            project.db,
            producer_id=producer,
            cells=[BaseCellWrite(row_ids[0], column_id, "source")],
        )
    edit_op = project.apply_edits(
        [{"row_id": row_ids[0], "column_id": column_id, "value": "overlay"}]
    )
    before = project.db.execute(
        "SELECT value,retrieval_revision FROM current_cell_values "
        "JOIN current_cells USING (column_id,row_id) "
        "WHERE column_id=? AND row_id=?",
        (column_id, row_ids[0]),
    ).fetchone()

    with write_transaction(project.db):
        replace_base_cells(
            project.db,
            producer_id=producer,
            column_ids=[column_id],
            row_ids=[row_ids[0]],
            cells=[BaseCellWrite(row_ids[0], column_id, "changed source")],
        )

    after = project.db.execute(
        "SELECT value,retrieval_revision FROM current_cell_values "
        "JOIN current_cells USING (column_id,row_id) "
        "WHERE column_id=? AND row_id=?",
        (column_id, row_ids[0]),
    ).fetchone()
    assert after["value"] == before["value"] == "overlay"
    assert after["retrieval_revision"] > before["retrieval_revision"]
    assert [
        row[0]
        for row in project.db.execute(
            "SELECT value FROM cells WHERE column_id=? AND row_id=? ORDER BY producer_id",
            (column_id, row_ids[0]),
        )
    ] == ["source", "changed source"]
    assert project.undo() == edit_op
    assert (
        project.db.execute(
            "SELECT value FROM current_cell_values WHERE column_id=? AND row_id=?",
            (column_id, row_ids[0]),
        ).fetchone()[0]
        == "changed source"
    )
    assert project.redo() == edit_op
    assert (
        project.db.execute(
            "SELECT value FROM current_cell_values WHERE column_id=? AND row_id=?",
            (column_id, row_ids[0]),
        ).fetchone()[0]
        == "overlay"
    )


def test_exact_pair_refresh_groups_dense_sets_without_expanding_sparse_pairs(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    import frisket.engine.store.current_cells as current_cells_module

    project = make_project("project")
    calls: list[tuple[tuple[int, ...], tuple[int, ...]]] = []

    def record_region(
        _db: sqlite3.Connection,
        *,
        column_ids: list[int],
        row_ids: tuple[int, ...],
        eager_limit: int,
    ) -> int:
        assert eager_limit == 512
        calls.append((tuple(column_ids), tuple(row_ids)))
        return len(column_ids) * len(row_ids)

    monkeypatch.setattr(current_cells_module, "refresh_current_cells", record_region)
    with write_transaction(project.db):
        refreshed = refresh_current_cell_pairs(
            project.db,
            {
                (100, 10),
                (101, 10),
                (100, 11),
                (101, 11),
                (102, 12),
                (103, 13),
            },
        )

    assert calls == [
        ((10, 11), (100, 101)),
        ((12,), (102,)),
        ((13,), (103,)),
    ]
    assert refreshed == 6
