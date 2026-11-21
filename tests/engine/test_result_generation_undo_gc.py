from __future__ import annotations


import pytest

from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.op_log import RestorationUnavailable
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.search_index import drain_index, index_batch
from test_result_generation_store import (
    _declare,
    _publish_initial_generation,
    _release,
    _seal,
    _seed_project,
    _start_claimed_run,
    _write,
)
from frisket.engine.store.transactions import write_transaction


def _publish_replacement(make_project):
    project, sheet_id, column_id, row_ids = _seed_project(make_project)
    generations, first = _publish_initial_generation(
        project, sheet_id, column_id, row_ids
    )
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row_ids[-1],))
    project.db.commit()
    second = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=column_id,
        row_ids=row_ids,
        label="replacement generation",
    )
    _declare(generations, second, column_id, write_mode="replace_scope")
    _write(
        project,
        second,
        [
            {
                "row_id": row_ids[0],
                "column_id": column_id,
                "value": "second:value",
                "publication_effect": "publish_value",
            },
            {
                "row_id": row_ids[1],
                "column_id": column_id,
                "value": None,
                "publication_effect": "publish_null",
            },
            {
                "row_id": row_ids[2],
                "column_id": column_id,
                "value": None,
                "error": "deliberate error",
                "error_code": "model_error",
                "outcome": "model_error",
                "publication_effect": "publish_error",
            },
            {
                "row_id": row_ids[3],
                "column_id": column_id,
                "value": "second:hidden",
                "publication_effect": "publish_value",
            },
        ],
    )
    _seal(generations, second, column_id)
    with write_transaction(project.db):
        RunResultStore(project).point_column_at_run(
            second.op_id, column_id, second.run_id
        )
    return project, column_id, row_ids, generations, first, second


def test_undo_releases_owned_claim_then_redo_restores_every_managed_head(
    make_project,
) -> None:
    project, column_id, row_ids, generations, first, second = _publish_replacement(
        make_project
    )
    assert project.undo() == second.op_id
    assert (
        project.db.execute(
            "SELECT status FROM output_column_claims WHERE claim_token=?",
            (second.authority.claim_token,),
        ).fetchone()[0]
        == "released"
    )
    assert (
        project.db.execute(
            "SELECT publication_closed FROM ops WHERE id=?", (second.op_id,)
        ).fetchone()[0]
        == 1
    )
    restored = generations.read_cell_heads(column_id)
    assert set(restored) == set(row_ids)
    assert {head.run_id for head in restored.values()} == {first.run_id}

    # Restoring retained output must not silently restore a deleted row.
    with pytest.raises(RestorationUnavailable, match="destination is unavailable"):
        project.redo()
    with project.db:
        project.db.execute("UPDATE rows SET hidden=0 WHERE id=?", (row_ids[-1],))
    assert project.redo() == second.op_id
    reapplied = generations.read_cell_heads(column_id)
    assert set(reapplied) == set(row_ids)
    assert {head.run_id for head in reapplied.values()} == {second.run_id}
    assert reapplied[row_ids[1]].publication_effect == "publish_null"
    assert reapplied[row_ids[2]].publication_effect == "publish_error"
    assert reapplied[row_ids[3]].value == "second:hidden"


def test_partial_generation_undo_redo_refreshes_only_its_result_coordinates(
    make_project,
) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(make_project)
    generations, first = _publish_initial_generation(
        project, sheet_id, column_id, row_ids
    )
    second = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=column_id,
        row_ids=[row_ids[0]],
        label="one-row replacement",
    )
    _declare(generations, second, column_id, write_mode="replace_scope")
    _write(
        project,
        second,
        [
            {
                "row_id": row_ids[0],
                "column_id": column_id,
                "value": "second:value",
                "publication_effect": "publish_value",
            }
        ],
    )
    _seal(generations, second, column_id)
    _release(project, second)
    project.apply_edits(
        [
            {
                "row_id": row_ids[2],
                "column_id": column_id,
                "value": "untouched manual overlay",
            }
        ]
    )
    manual_op_id = project.op_cursor

    project.db.execute("CREATE TEMP TABLE projection_audit(kind,row_id,column_id)")
    for table in ("cell_result_heads", "current_cells"):
        for event, ref in (
            ("DELETE", "OLD"),
            ("INSERT", "NEW"),
            ("UPDATE", "NEW"),
        ):
            project.db.execute(
                f"CREATE TEMP TRIGGER audit_{table}_{event.lower()} "
                f"AFTER {event} ON {table} BEGIN "
                "INSERT INTO projection_audit VALUES "
                f"('{table}:{event.lower()}',{ref}.row_id,{ref}.column_id); END"
            )
    project.db.commit()

    assert project.undo() == manual_op_id
    project.db.execute("DELETE FROM projection_audit")
    project.db.execute("DELETE FROM search_dirty_scopes")
    project.db.commit()
    assert project.undo() == second.op_id
    restored = generations.read_cell_heads(column_id)
    assert {head.run_id for head in restored.values()} == {first.run_id}
    assert project.get_values(sheet_id, column_id)[row_ids[0]] == "first:0"
    result_writes = project.db.execute(
        "SELECT kind,row_id,column_id FROM projection_audit "
        "WHERE kind LIKE 'cell_result_heads:%'"
    ).fetchall()
    current_writes = project.db.execute(
        "SELECT kind,row_id,column_id FROM projection_audit "
        "WHERE kind LIKE 'current_cells:%'"
    ).fetchall()
    assert {
        (int(row["row_id"]), int(row["column_id"])) for row in result_writes
    } == set()  # Legacy head cache is not the current-value authority.
    assert {(int(row["row_id"]), int(row["column_id"])) for row in current_writes} == {
        (row_ids[0], column_id)
    }  # Small scopes repair only their own coordinate.
    assert [
        tuple(row)
        for row in project.db.execute(
            "SELECT sheet_id,column_id,row_id_start,row_id_end "
            "FROM search_dirty_scopes ORDER BY id"
        )
    ] == [(sheet_id, column_id, row_ids[0], row_ids[0])]

    project.db.execute("DELETE FROM projection_audit")
    project.db.execute("DELETE FROM search_dirty_scopes")
    project.db.commit()
    assert project.redo() == second.op_id
    assert generations.read_cell_heads(column_id)[row_ids[0]].run_id == second.run_id
    assert project.get_values(sheet_id, column_id)[row_ids[0]] == "second:value"
    assert {
        (int(row["row_id"]), int(row["column_id"]))
        for row in project.db.execute("SELECT row_id,column_id FROM projection_audit")
    } == {(row_ids[0], column_id)}
    assert [
        tuple(row)
        for row in project.db.execute(
            "SELECT sheet_id,column_id,row_id_start,row_id_end "
            "FROM search_dirty_scopes ORDER BY id"
        )
    ] == [(sheet_id, column_id, row_ids[0], row_ids[0])]


def test_undo_rolls_back_op_flip_when_head_rebuild_fails(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, column_id, _row_ids, generations, first, second = _publish_replacement(
        make_project
    )
    _release(project, second)
    before = generations.read_cell_heads(column_id)

    def fail_rebuild(*_args, **_kwargs):
        raise RuntimeError("head rebuild failed")

    monkeypatch.setattr(
        "frisket.engine.store.op_log.refresh_current_cells", fail_rebuild
    )
    with pytest.raises(RuntimeError, match="head rebuild failed"):
        project.undo()

    op = project.db.execute(
        "SELECT status FROM ops WHERE id=?", (second.op_id,)
    ).fetchone()
    assert op is not None and op["status"] == "applied"
    assert project.op_cursor == second.op_id
    assert generations.read_cell_heads(column_id) == before
    assert first.run_id != second.run_id


def test_direct_edit_undo_and_redo_preserve_values_with_claimed_column(
    make_project,
) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(make_project)
    _generations, _first = _publish_initial_generation(
        project, sheet_id, column_id, row_ids
    )
    claims = OutputColumnClaimStore(project)
    edit_op_id = project.apply_edits(
        [
            {
                "row_id": row_ids[0],
                "column_id": column_id,
                "value": "manual",
            }
        ]
    )
    with write_transaction(claims.db):
        created, conflict = claims.acquire(
            sheet_id=sheet_id,
            output_names=["generated"],
            action_kind="map.regex_extract",
            claim_token="claim:edit-transition",
        )
    assert len(created) == 1 and conflict is None
    assert project.undo() == edit_op_id
    assert project.get_values(sheet_id, column_id)[row_ids[0]] == "first:0"
    with write_transaction(claims.db):
        claims.release(claim_token="claim:edit-transition")
    with write_transaction(claims.db):
        created, conflict = claims.acquire(
            sheet_id=sheet_id,
            output_names=["generated"],
            action_kind="map.regex_extract",
            claim_token="claim:edit-redo",
        )
    assert len(created) == 1 and conflict is None
    assert project.redo() == edit_op_id
    assert project.get_values(sheet_id, column_id)[row_ids[0]] == "manual"
    with write_transaction(claims.db):
        claims.release(claim_token="claim:edit-redo")


def test_edit_undo_redo_refreshes_only_the_changed_search_cell(make_project) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(make_project)
    source_column_id = next(
        int(column["id"])
        for column in project.columns(sheet_id)
        if column["name"] == "source"
    )
    row_ids.extend(
        project.add_rows(
            sheet_id,
            [{"source": f"many-row-{index}"} for index in range(64)],
            {"source": source_column_id},
        )
    )
    drain_index(project)
    edit_op_id = project.apply_edits(
        [
            {
                "row_id": row_ids[0],
                "column_id": source_column_id,
                "value": "manual",
            }
        ]
    )
    assert project.get_values(sheet_id, source_column_id)[row_ids[0]] == "manual"
    assert index_batch(project, batch_size=100).processed == 1
    project.db.execute("CREATE TEMP TABLE current_cell_audit(kind,row_id,column_id)")
    project.db.execute(
        "CREATE TEMP TRIGGER audit_current_cell_delete AFTER DELETE ON current_cells "
        "BEGIN INSERT INTO current_cell_audit VALUES ('delete',OLD.row_id,OLD.column_id); END"
    )
    project.db.execute(
        "CREATE TEMP TRIGGER audit_current_cell_insert AFTER INSERT ON current_cells "
        "BEGIN INSERT INTO current_cell_audit VALUES ('insert',NEW.row_id,NEW.column_id); END"
    )
    project.db.execute(
        "CREATE TEMP TRIGGER audit_current_cell_update AFTER UPDATE ON current_cells "
        "BEGIN INSERT INTO current_cell_audit VALUES ('update',NEW.row_id,NEW.column_id); END"
    )
    project.db.commit()

    assert project.undo() == edit_op_id
    assert project.get_values(sheet_id, source_column_id)[row_ids[0]] != "manual"
    assert index_batch(project, batch_size=100).processed == 1
    assert {
        (int(row["row_id"]), int(row["column_id"]))
        for row in project.db.execute("SELECT row_id,column_id FROM current_cell_audit")
    } == {(row_ids[0], source_column_id)}
    project.db.execute("DELETE FROM current_cell_audit")
    project.db.commit()

    assert project.redo() == edit_op_id
    assert project.get_values(sheet_id, source_column_id)[row_ids[0]] == "manual"
    assert index_batch(project, batch_size=100).processed == 1
    assert {
        (int(row["row_id"]), int(row["column_id"]))
        for row in project.db.execute("SELECT row_id,column_id FROM current_cell_audit")
    } == {(row_ids[0], source_column_id)}


def test_compaction_preserves_discarded_generation_and_its_blob(
    make_project,
) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(make_project)
    generations = ResultGenerationStore(project)
    with write_transaction(project.db):
        project.set_column_type(column_id, "file")
    first_blob = project.add_blob(
        b"first generation", filename="first.bin", mime="application/octet-stream"
    )
    second_blob = project.add_blob(
        b"second generation", filename="second.bin", mime="application/octet-stream"
    )
    first = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=column_id,
        row_ids=row_ids,
        label="first blob generation",
    )
    _declare(generations, first, column_id, write_mode="create")
    _write(
        project,
        first,
        [
            {
                "row_id": row_ids[0],
                "column_id": column_id,
                "value": {"blob": first_blob},
                "publication_effect": "publish_value",
            }
        ],
    )
    _seal(generations, first, column_id)
    with write_transaction(project.db):
        RunResultStore(project).point_column_at_run(
            first.op_id, column_id, first.run_id
        )
    _release(project, first)

    second = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=column_id,
        row_ids=[row_ids[0]],
        label="second blob generation",
    )
    _declare(generations, second, column_id, write_mode="replace_scope")
    _write(
        project,
        second,
        [
            {
                "row_id": row_ids[0],
                "column_id": column_id,
                "value": {"blob": second_blob},
                "publication_effect": "publish_value",
            }
        ],
    )
    _seal(generations, second, column_id)
    with write_transaction(project.db):
        RunResultStore(project).point_column_at_run(
            second.op_id, column_id, second.run_id
        )
    _release(project, second)

    assert project.undo() == second.op_id
    project.gc_blobs()
    assert project.db.execute(
        "SELECT 1 FROM blobs WHERE hash=?", (second_blob,)
    ).fetchone()

    project.append_change("branch.after.undo")
    project.db.execute("UPDATE ops SET status='applied' WHERE id=?", (second.op_id,))
    project.db.execute(
        "UPDATE cell_result_heads SET run_id=? WHERE column_id=? AND row_id=?",
        (second.run_id, column_id, row_ids[0]),
    )
    project.db.execute("UPDATE ops SET status='discarded' WHERE id=?", (second.op_id,))
    project.db.execute(
        "UPDATE columns SET current_run_id=? WHERE id=?",
        (second.run_id, column_id),
    )
    project.db.commit()

    summary = project.compact(vacuum=False)
    assert summary["results_pruned"] == 0
    # Stale legacy pointers cannot resurrect a discarded Change.
    head = generations.read_cell_heads(column_id)[row_ids[0]]
    assert head.run_id == first.run_id
    assert project.get_values(sheet_id, column_id)[row_ids[0]] == {"blob": first_blob}
    assert project.db.execute(
        "SELECT 1 FROM run_output_generations WHERE run_id=?", (second.run_id,)
    ).fetchone()
    assert project.db.execute(
        "SELECT 1 FROM results WHERE run_id=?", (second.run_id,)
    ).fetchone()
    assert project.db.execute(
        "SELECT 1 FROM runs WHERE id=?", (second.run_id,)
    ).fetchone()
    assert (
        project.db.execute(
            "SELECT current_run_id FROM columns WHERE id=?", (column_id,)
        ).fetchone()[0]
        == second.run_id
    )
    assert project.db.execute(
        "SELECT 1 FROM blobs WHERE hash=?", (first_blob,)
    ).fetchone()
    assert project.db.execute(
        "SELECT 1 FROM blobs WHERE hash=?", (second_blob,)
    ).fetchone()


def test_compaction_retains_discarded_base_referenced_by_surviving_generation(
    make_project,
) -> None:
    project, column_id, row_ids, generations, first, second = _publish_replacement(
        make_project
    )
    _release(project, second)
    binding = generations.get_binding(second.run_id, column_id)
    assert binding is not None and binding.expected_base_run_id == first.run_id
    project.db.execute("UPDATE ops SET status='discarded' WHERE id=?", (first.op_id,))
    project.db.commit()

    summary = project.compact(vacuum=False)
    assert summary["results_pruned"] == 0
    assert generations.get_binding(first.run_id, column_id) is not None
    assert project.db.execute(
        "SELECT 1 FROM results WHERE run_id=?", (first.run_id,)
    ).fetchone()
    assert project.db.execute(
        "SELECT 1 FROM runs WHERE id=?", (first.run_id,)
    ).fetchone()
    assert {
        head.run_id for head in generations.read_cell_heads(column_id).values()
    } == {second.run_id}
    assert set(generations.read_cell_heads(column_id)) == set(row_ids)


def test_compaction_preserves_discarded_staged_replacement(make_project) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(make_project)
    generations = ResultGenerationStore(project)
    blob = project.add_blob(b"staged replacement history")
    staged = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=column_id,
        row_ids=[row_ids[0]],
        label="staged replacement",
    )
    _declare(generations, staged, column_id, write_mode="replace_scope")
    _write(
        project,
        staged,
        [
            {
                "row_id": row_ids[0],
                "column_id": column_id,
                "value": {"blob": blob},
                "publication_effect": "publish_value",
            }
        ],
    )
    _release(project, staged)
    project.db.execute("UPDATE ops SET status='discarded' WHERE id=?", (staged.op_id,))
    project.db.commit()

    summary = project.compact(vacuum=False)

    assert summary["results_pruned"] == 0
    assert generations.get_binding(staged.run_id, column_id) is not None
    assert project.db.execute(
        "SELECT 1 FROM results WHERE run_id=?", (staged.run_id,)
    ).fetchone()
    assert project.db.execute("SELECT 1 FROM blobs WHERE hash=?", (blob,)).fetchone()


def test_compaction_does_not_mutate_generation_history(make_project) -> None:
    project, column_id, _row_ids, generations, _first, second = _publish_replacement(
        make_project
    )
    _release(project, second)
    assert project.undo() == second.op_id
    project.append_change("branch.after.undo")
    assert (
        project.db.execute(
            "SELECT status FROM ops WHERE id=?", (second.op_id,)
        ).fetchone()[0]
        == "undone"
    )

    def snapshot() -> tuple[object, ...]:
        return (
            tuple(
                tuple(row)
                for row in project.db.execute(
                    "SELECT * FROM run_output_generations ORDER BY run_id,column_id"
                ).fetchall()
            ),
            tuple(
                tuple(row)
                for row in project.db.execute(
                    "SELECT * FROM results ORDER BY run_id,row_id,column_id"
                ).fetchall()
            ),
            tuple(
                tuple(row)
                for row in project.db.execute(
                    "SELECT * FROM cell_result_heads ORDER BY column_id,row_id"
                ).fetchall()
            ),
            tuple(
                project.db.execute(
                    "SELECT current_run_id FROM columns WHERE id=?", (column_id,)
                ).fetchone()
            ),
            tuple(
                tuple(row)
                for row in project.db.execute(
                    "SELECT id,status FROM runs ORDER BY id"
                ).fetchall()
            ),
        )

    before = snapshot()
    summary = project.compact(vacuum=False)
    assert summary["results_pruned"] == 0
    assert snapshot() == before
    assert generations.get_binding(second.run_id, column_id) is not None
