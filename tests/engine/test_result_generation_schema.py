from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore


def _seed_generation(
    tmp_path: Path,
) -> tuple[Project, int, int, int, int, int]:
    project = Project.create(tmp_path / "generation-schema.frisket", name="Heads")
    sheet_id = project.add_sheet("Rows")
    source_column_id = project.add_column(sheet_id, "source")
    output_column_id = project.add_column(sheet_id, "generated", ai_generated=True)
    project.add_rows(
        sheet_id,
        [{"source": "alpha"}, {"source": "beta"}],
        {"source": source_column_id},
    )
    row_ids = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
        )
    ]
    op_id = project.append_op("map.regex_extract", {"pattern": "(.*)"})
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.regex_extract",
        row_ids=row_ids,
        total_rows=len(row_ids),
    )
    return (
        project,
        sheet_id,
        output_column_id,
        row_ids[0],
        row_ids[1],
        run_id,
    )


def _declare_generation(
    project: Project,
    *,
    run_id: int,
    column_id: int,
    state: str = "active",
    write_mode: str = "create",
) -> None:
    terminal = "completed" if state == "sealed" else None
    sealed_at = "2026-08-16T00:00:00Z" if state == "sealed" else None
    project.db.execute(
        "INSERT INTO run_output_generations "
        "(run_id,column_id,output_role,compatibility_key,write_mode,state,"
        "claim_token,terminal_disposition,sealed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            column_id,
            "output",
            "sha256:test-output",
            write_mode,
            state,
            "claim:test",
            terminal,
            sealed_at,
        ),
    )


def test_generation_schema_uses_exact_composite_identity_and_monotone_run_ids(
    tmp_path: Path,
) -> None:
    project, _sheet_id, column_id, row_id, _other_row_id, run_id = _seed_generation(
        tmp_path
    )

    generation_sql = str(
        project.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='run_output_generations'"
        ).fetchone()[0]
    )
    heads_sql = str(
        project.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='cell_result_heads'"
        ).fetchone()[0]
    )
    runs_sql = str(
        project.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone()[0]
    )

    assert "PRIMARY KEY (run_id, column_id)" in generation_sql
    assert "PRIMARY KEY (column_id, row_id)" in heads_sql
    assert "AUTOINCREMENT" in runs_sql.upper()

    with pytest.raises(
        sqlite3.IntegrityError,
        match="publication effect requires an open applied generation",
    ):
        project.db.execute(
            "INSERT INTO results "
            "(run_id,row_id,column_id,value,publication_effect) "
            "VALUES (?,?,?,?,?)",
            (run_id, row_id, column_id, json.dumps("alpha"), "publish_value"),
        )

    _declare_generation(project, run_id=run_id, column_id=column_id)
    project.db.execute(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value,publication_effect) VALUES (?,?,?,?,?)",
        (run_id, row_id, column_id, json.dumps("alpha"), "publish_value"),
    )
    project.db.execute(
        "INSERT INTO cell_result_heads (column_id,row_id,run_id) VALUES (?,?,?)",
        (column_id, row_id, run_id),
    )

    assert tuple(
        project.db.execute(
            "SELECT column_id,row_id,run_id FROM cell_result_heads"
        ).fetchone()
    ) == (column_id, row_id, run_id)


def test_compacted_run_id_is_not_reused_by_surviving_row_effect_checkpoint(
    tmp_path: Path,
) -> None:
    project, sheet_id, _column_id, row_id, _other_row_id, run_id = _seed_generation(
        tmp_path
    )
    op_id = int(
        project.db.execute("SELECT op_id FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    )
    project.db.execute(
        "INSERT INTO effect_checkpoints "
        "(id,family,group_key,unit_key,action_kind,identity,state) "
        "VALUES ('orphan-row-effect','row_effect',?,?,"
        "'map.regex_extract','old-request','reserved')",
        (str(run_id), str(row_id)),
    )
    project.db.execute("UPDATE ops SET status='discarded' WHERE id=?", (op_id,))
    project.db.commit()

    project.compact(vacuum=False)
    assert (
        project.db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone()
        is None
    )
    assert (
        project.db.execute(
            "SELECT 1 FROM effect_checkpoints WHERE id='orphan-row-effect'"
        ).fetchone()
        is not None
    )

    next_op_id = project.append_op("map.regex_extract", {"pattern": "(.*)"})
    next_run_id = RunResultStore(project).start_run(
        next_op_id,
        sheet_id,
        "map.regex_extract",
    )
    assert next_run_id > run_id
    assert RunResultStore(project).row_effect_checkpoint(next_run_id, row_id) is None


@pytest.mark.parametrize(
    ("value", "error", "effect"),
    [
        (None, None, "publish_value"),
        (json.dumps("value"), None, "publish_null"),
        (None, None, "publish_error"),
        (json.dumps("value"), "boom", "publish_error"),
        (json.dumps("value"), None, "not_a_publication_effect"),
    ],
)
def test_publication_effect_is_explicit_and_payload_coherent(
    tmp_path: Path,
    value: str | None,
    error: str | None,
    effect: str,
) -> None:
    project, _sheet_id, column_id, row_id, _other_row_id, run_id = _seed_generation(
        tmp_path
    )
    _declare_generation(project, run_id=run_id, column_id=column_id)

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        project.db.execute(
            "INSERT INTO results "
            "(run_id,row_id,column_id,value,error,publication_effect) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, row_id, column_id, value, error, effect),
        )


def test_published_result_payload_is_immutable_but_review_state_is_not(
    tmp_path: Path,
) -> None:
    project, _sheet_id, column_id, row_id, other_row_id, run_id = _seed_generation(
        tmp_path
    )
    _declare_generation(project, run_id=run_id, column_id=column_id)
    project.db.execute(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value,confidence,justification,"
        "publication_effect) VALUES (?,?,?,?,?,?,?)",
        (
            run_id,
            row_id,
            column_id,
            json.dumps("alpha"),
            0.75,
            "fixture",
            "publish_value",
        ),
    )

    other_op_id = project.append_op("map.regex_extract", {"pattern": "(.*)"})
    other_run_id = RunResultStore(project).start_run(
        other_op_id,
        _sheet_id,
        "map.regex_extract",
    )
    other_column_id = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND id<>? ORDER BY id LIMIT 1",
            (_sheet_id, column_id),
        ).fetchone()[0]
    )

    for identity_column, replacement in (
        ("run_id", other_run_id),
        ("row_id", other_row_id),
        ("column_id", other_column_id),
    ):
        with pytest.raises(
            sqlite3.IntegrityError, match="published result semantics are immutable"
        ):
            project.db.execute(
                f"UPDATE results SET {identity_column}=? "
                "WHERE run_id=? AND row_id=? AND column_id=?",
                (replacement, run_id, row_id, column_id),
            )

    # The owning producer may finish semantics before publication, but the
    # exact result identity is immutable from insertion onward.
    project.db.execute(
        "UPDATE results SET value=? WHERE run_id=? AND row_id=? AND column_id=?",
        (json.dumps("finalized"), run_id, row_id, column_id),
    )
    project.db.execute(
        "INSERT INTO cell_result_heads (column_id,row_id,run_id) VALUES (?,?,?)",
        (column_id, row_id, run_id),
    )
    with pytest.raises(
        sqlite3.IntegrityError, match="published result semantics are immutable"
    ):
        project.db.execute(
            "UPDATE results SET value=? WHERE run_id=? AND row_id=? AND column_id=?",
            (json.dumps("changed"), run_id, row_id, column_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="published result is immutable"):
        project.db.execute(
            "DELETE FROM results WHERE run_id=? AND row_id=? AND column_id=?",
            (run_id, row_id, column_id),
        )

    project.db.execute(
        "UPDATE results SET review_state='verified' "
        "WHERE run_id=? AND row_id=? AND column_id=?",
        (run_id, row_id, column_id),
    )
    assert (
        project.db.execute(
            "SELECT review_state FROM results "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            (run_id, row_id, column_id),
        ).fetchone()[0]
        == "verified"
    )

    op_id = int(
        project.db.execute("SELECT op_id FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    )
    project.db.execute("UPDATE ops SET status='undone' WHERE id=?", (op_id,))
    # Exact idempotent writes remain harmless even after the owning op moves;
    # no semantic payload or publication identity changes.
    project.db.execute(
        "UPDATE results SET value=value WHERE run_id=? AND row_id=? AND column_id=?",
        (run_id, row_id, column_id),
    )
    project.db.execute(
        "UPDATE results SET review_state='rejected' "
        "WHERE run_id=? AND row_id=? AND column_id=?",
        (run_id, row_id, column_id),
    )
    assert (
        project.db.execute(
            "SELECT review_state FROM results "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            (run_id, row_id, column_id),
        ).fetchone()[0]
        == "rejected"
    )

    with pytest.raises(
        sqlite3.IntegrityError, match="cell head requires an applied publishable result"
    ):
        project.db.execute(
            "INSERT INTO cell_result_heads (column_id,row_id,run_id) VALUES (?,?,?)",
            (column_id, other_row_id, run_id),
        )


def test_generation_managed_results_require_an_explicit_effect(
    tmp_path: Path,
) -> None:
    project, sheet_id, column_id, row_id, other_row_id, run_id = _seed_generation(
        tmp_path
    )
    _declare_generation(project, run_id=run_id, column_id=column_id)

    with pytest.raises(
        sqlite3.IntegrityError,
        match="publication effect",
    ):
        project.db.execute(
            "INSERT INTO results (run_id,row_id,column_id,value) VALUES (?,?,?,?)",
            (run_id, row_id, column_id, json.dumps("ambiguous")),
        )

    legacy_column_id = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND id<>? ORDER BY id LIMIT 1",
            (sheet_id, column_id),
        ).fetchone()[0]
    )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="publication effect",
    ):
        project.db.execute(
            "INSERT INTO results (run_id,row_id,column_id,value) VALUES (?,?,?,?)",
            (run_id, other_row_id, legacy_column_id, json.dumps("legacy")),
        )

    legacy_op_id = project.append_op("legacy")
    legacy_run_id = RunResultStore(project).start_run(
        legacy_op_id, sheet_id, "test.legacy"
    )
    project.db.execute(
        "INSERT INTO results (run_id,row_id,column_id,value) VALUES (?,?,?,?)",
        (legacy_run_id, other_row_id, legacy_column_id, json.dumps("legacy")),
    )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="publication effect",
    ):
        project.db.execute(
            "UPDATE results SET run_id=?, column_id=? "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            (
                run_id,
                column_id,
                legacy_run_id,
                other_row_id,
                legacy_column_id,
            ),
        )


def test_legacy_null_sibling_result_freezes_the_complete_declaration_set(
    tmp_path: Path,
) -> None:
    project, sheet_id, column_id, row_id, _other_row_id, run_id = _seed_generation(
        tmp_path
    )
    legacy_sibling_column_id = project.add_column(
        sheet_id, "legacy-sibling", ai_generated=True
    )
    project.db.execute(
        "INSERT INTO results (run_id,row_id,column_id,value) VALUES (?,?,?,?)",
        (run_id, row_id, legacy_sibling_column_id, json.dumps("legacy")),
    )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="complete output generation set must precede publication",
    ):
        _declare_generation(project, run_id=run_id, column_id=column_id)


def test_generation_declaration_and_state_are_forward_only(tmp_path: Path) -> None:
    project, _sheet_id, column_id, _row_id, _other_row_id, run_id = _seed_generation(
        tmp_path
    )
    _declare_generation(
        project,
        run_id=run_id,
        column_id=column_id,
        state="staged",
        write_mode="replace_scope",
    )

    with pytest.raises(
        sqlite3.IntegrityError, match="output generation declaration is immutable"
    ):
        project.db.execute(
            "UPDATE run_output_generations SET compatibility_key='changed' "
            "WHERE run_id=? AND column_id=?",
            (run_id, column_id),
        )
    with pytest.raises(
        sqlite3.IntegrityError, match="output generation state is forward-only"
    ):
        project.db.execute(
            "UPDATE run_output_generations SET state='active' "
            "WHERE run_id=? AND column_id=?",
            (run_id, column_id),
        )

    project.db.execute(
        "UPDATE run_output_generations SET state='sealed', "
        "terminal_disposition='completed', sealed_at=datetime('now') "
        "WHERE run_id=? AND column_id=?",
        (run_id, column_id),
    )
    with pytest.raises(
        sqlite3.IntegrityError, match="sealed output generation is immutable"
    ):
        project.db.execute(
            "UPDATE run_output_generations SET terminal_disposition='failed' "
            "WHERE run_id=? AND column_id=?",
            (run_id, column_id),
        )
    with pytest.raises(
        sqlite3.IntegrityError, match="sealed output generation is immutable"
    ):
        project.db.execute(
            "UPDATE run_output_generations SET sealed_at='2099-01-01T00:00:00Z' "
            "WHERE run_id=? AND column_id=?",
            (run_id, column_id),
        )
    with pytest.raises(
        sqlite3.IntegrityError, match="output generation state is forward-only"
    ):
        project.db.execute(
            "UPDATE run_output_generations SET state='staged', "
            "terminal_disposition=NULL, sealed_at=NULL "
            "WHERE run_id=? AND column_id=?",
            (run_id, column_id),
        )


@pytest.mark.parametrize(
    ("generation_state", "write_mode", "op_status", "deletable"),
    [
        ("staged", "replace_scope", "applied", False),
        ("staged", "replace_scope", "undone", False),
        ("staged", "replace_scope", "discarded", True),
        ("sealed", "create", "applied", False),
        ("sealed", "create", "undone", False),
        ("sealed", "create", "discarded", True),
    ],
)
def test_generation_binding_delete_requires_permanent_op_discard(
    tmp_path: Path,
    generation_state: str,
    write_mode: str,
    op_status: str,
    deletable: bool,
) -> None:
    project, _sheet_id, column_id, _row_id, _other_row_id, run_id = _seed_generation(
        tmp_path
    )
    _declare_generation(
        project,
        run_id=run_id,
        column_id=column_id,
        state=generation_state,
        write_mode=write_mode,
    )
    op_id = int(
        project.db.execute("SELECT op_id FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    )
    project.db.execute("UPDATE ops SET status=? WHERE id=?", (op_status, op_id))

    if deletable:
        project.db.execute(
            "DELETE FROM run_output_generations WHERE run_id=? AND column_id=?",
            (run_id, column_id),
        )
        assert (
            project.db.execute(
                "SELECT 1 FROM run_output_generations WHERE run_id=? AND column_id=?",
                (run_id, column_id),
            ).fetchone()
            is None
        )
    else:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="output generation can be deleted only after op discard",
        ):
            project.db.execute(
                "DELETE FROM run_output_generations WHERE run_id=? AND column_id=?",
                (run_id, column_id),
            )


def test_discard_gc_must_remove_head_before_generation_and_result(
    tmp_path: Path,
) -> None:
    project, _sheet_id, column_id, row_id, _other_row_id, run_id = _seed_generation(
        tmp_path
    )
    _declare_generation(project, run_id=run_id, column_id=column_id)
    project.db.execute(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value,publication_effect) VALUES (?,?,?,?,?)",
        (run_id, row_id, column_id, json.dumps("published"), "publish_value"),
    )
    project.db.execute(
        "INSERT INTO cell_result_heads (column_id,row_id,run_id) VALUES (?,?,?)",
        (column_id, row_id, run_id),
    )
    project.db.execute(
        "UPDATE run_output_generations SET state='sealed', "
        "terminal_disposition='completed', sealed_at=datetime('now') "
        "WHERE run_id=? AND column_id=?",
        (run_id, column_id),
    )
    op_id = int(
        project.db.execute("SELECT op_id FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    )
    project.db.execute("UPDATE ops SET status='discarded' WHERE id=?", (op_id,))

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        project.db.execute(
            "DELETE FROM run_output_generations WHERE run_id=? AND column_id=?",
            (run_id, column_id),
        )

    project.db.execute(
        "DELETE FROM cell_result_heads WHERE column_id=? AND row_id=?",
        (column_id, row_id),
    )
    project.db.execute(
        "DELETE FROM run_output_generations WHERE run_id=? AND column_id=?",
        (run_id, column_id),
    )
    project.db.execute(
        "DELETE FROM results WHERE run_id=? AND row_id=? AND column_id=?",
        (run_id, row_id, column_id),
    )
