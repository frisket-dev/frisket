from __future__ import annotations

from pathlib import Path

import pytest

from frisket.engine.store import bundle_io
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from test_result_generation_store import (
    _declare,
    _publish_initial_generation,
    _release,
    _seal,
    _seed_project,
    _start_claimed_run,
    _write,
)


def _publish_replacement(tmp_path: Path):
    project, sheet_id, column_id, row_ids = _seed_project(tmp_path)
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
    RunResultStore(project).point_column_at_run(second.op_id, column_id, second.run_id)
    return project, column_id, row_ids, generations, first, second


def test_undo_redo_refuses_active_claim_then_rebuilds_every_managed_head(
    tmp_path: Path,
) -> None:
    project, column_id, row_ids, generations, first, second = _publish_replacement(
        tmp_path
    )
    try:
        with pytest.raises(ValueError, match="output_column_busy"):
            project.undo()
        assert project.op_cursor == second.op_id
        assert {
            head.run_id for head in generations.read_cell_heads(column_id).values()
        } == {second.run_id}

        _release(project, second)
        assert project.undo() == second.op_id
        restored = generations.read_cell_heads(column_id)
        assert set(restored) == set(row_ids)
        assert {head.run_id for head in restored.values()} == {first.run_id}

        assert project.redo() == second.op_id
        reapplied = generations.read_cell_heads(column_id)
        assert set(reapplied) == set(row_ids)
        assert {head.run_id for head in reapplied.values()} == {second.run_id}
        assert reapplied[row_ids[1]].publication_effect == "publish_null"
        assert reapplied[row_ids[2]].publication_effect == "publish_error"
        assert reapplied[row_ids[3]].value == "second:hidden"
    finally:
        project.close()


def test_undo_rolls_back_op_flip_when_head_rebuild_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, column_id, _row_ids, generations, first, second = _publish_replacement(
        tmp_path
    )
    try:
        _release(project, second)
        before = generations.read_cell_heads(column_id)

        def fail_rebuild(*_args, **_kwargs):
            raise RuntimeError("head rebuild failed")

        monkeypatch.setattr(ResultGenerationStore, "rebuild_heads", fail_rebuild)
        with pytest.raises(RuntimeError, match="head rebuild failed"):
            project.undo()

        op = project.db.execute(
            "SELECT status FROM ops WHERE id=?", (second.op_id,)
        ).fetchone()
        assert op is not None and op["status"] == "applied"
        assert project.op_cursor == second.op_id
        assert generations.read_cell_heads(column_id) == before
        assert first.run_id != second.run_id
    finally:
        project.close()


def test_direct_edit_undo_and_redo_refuse_claim_on_managed_column(
    tmp_path: Path,
) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(tmp_path)
    _generations, _first = _publish_initial_generation(
        project, sheet_id, column_id, row_ids
    )
    claims = OutputColumnClaimStore(project)
    try:
        edit_op_id = project.apply_edits(
            [
                {
                    "row_id": row_ids[0],
                    "column_id": column_id,
                    "value": "manual",
                }
            ]
        )
        created, conflict = claims.acquire(
            sheet_id=sheet_id,
            output_names=["generated"],
            action_kind="map.regex_extract",
            claim_token="claim:edit-transition",
        )
        assert len(created) == 1 and conflict is None
        with pytest.raises(ValueError, match="output_column_busy"):
            project.undo()

        claims.release(claim_token="claim:edit-transition")
        assert project.undo() == edit_op_id
        created, conflict = claims.acquire(
            sheet_id=sheet_id,
            output_names=["generated"],
            action_kind="map.regex_extract",
            claim_token="claim:edit-redo",
        )
        assert len(created) == 1 and conflict is None
        with pytest.raises(ValueError, match="output_column_busy"):
            project.redo()
        claims.release(claim_token="claim:edit-redo")
        assert project.redo() == edit_op_id
    finally:
        project.close()


def test_compaction_roots_redoable_journal_then_prunes_discarded_generation(
    tmp_path: Path,
) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(tmp_path)
    generations = ResultGenerationStore(project)
    first_blob = project.add_blob(
        b"first generation", filename="first.bin", mime="application/octet-stream"
    )
    second_blob = project.add_blob(
        b"second generation", filename="second.bin", mime="application/octet-stream"
    )
    try:
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
        RunResultStore(project).point_column_at_run(
            second.op_id, column_id, second.run_id
        )
        _release(project, second)

        assert project.undo() == second.op_id
        project.gc_blobs()
        assert project.db.execute(
            "SELECT 1 FROM blobs WHERE hash=?", (second_blob,)
        ).fetchone()

        project.append_op("branch.after.undo")
        project.db.execute(
            "UPDATE ops SET status='applied' WHERE id=?", (second.op_id,)
        )
        project.db.execute(
            "UPDATE cell_result_heads SET run_id=? WHERE column_id=? AND row_id=?",
            (second.run_id, column_id, row_ids[0]),
        )
        project.db.execute(
            "UPDATE ops SET status='discarded' WHERE id=?", (second.op_id,)
        )
        project.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id=?",
            (second.run_id, column_id),
        )
        project.db.commit()

        summary = project.compact(vacuum=False)
        assert summary["results_pruned"] == 1
        head = generations.read_cell_heads(column_id)[row_ids[0]]
        assert head.run_id == first.run_id
        assert (
            project.db.execute(
                "SELECT 1 FROM run_output_generations WHERE run_id=?", (second.run_id,)
            ).fetchone()
            is None
        )
        assert (
            project.db.execute(
                "SELECT 1 FROM results WHERE run_id=?", (second.run_id,)
            ).fetchone()
            is None
        )
        assert (
            project.db.execute(
                "SELECT 1 FROM runs WHERE id=?", (second.run_id,)
            ).fetchone()
            is None
        )
        assert (
            project.db.execute(
                "SELECT current_run_id FROM columns WHERE id=?", (column_id,)
            ).fetchone()[0]
            is None
        )
        assert project.db.execute(
            "SELECT 1 FROM blobs WHERE hash=?", (first_blob,)
        ).fetchone()
        assert (
            project.db.execute(
                "SELECT 1 FROM blobs WHERE hash=?", (second_blob,)
            ).fetchone()
            is None
        )
    finally:
        project.close()


def test_compaction_retains_discarded_base_referenced_by_surviving_generation(
    tmp_path: Path,
) -> None:
    project, column_id, row_ids, generations, first, second = _publish_replacement(
        tmp_path
    )
    try:
        _release(project, second)
        binding = generations.get_binding(second.run_id, column_id)
        assert binding is not None and binding.expected_base_run_id == first.run_id
        project.db.execute(
            "UPDATE ops SET status='discarded' WHERE id=?", (first.op_id,)
        )
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
    finally:
        project.close()


def test_compaction_rolls_back_all_generation_pruning_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, column_id, _row_ids, generations, _first, second = _publish_replacement(
        tmp_path
    )
    try:
        _release(project, second)
        assert project.undo() == second.op_id
        project.append_op("branch.after.undo")
        assert (
            project.db.execute(
                "SELECT status FROM ops WHERE id=?", (second.op_id,)
            ).fetchone()[0]
            == "discarded"
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
        original_prune = bundle_io._prune_dead_runs

        def fail_after_pruning(prune_project) -> int:  # noqa: ANN001
            original_prune(prune_project)
            raise RuntimeError("injected compact failure")

        monkeypatch.setattr(bundle_io, "_prune_dead_runs", fail_after_pruning)
        with pytest.raises(RuntimeError, match="injected compact failure"):
            project.compact(vacuum=False)
        assert snapshot() == before
        assert generations.get_binding(second.run_id, column_id) is not None
    finally:
        project.close()
