from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import (
    GenerationDeclarationConflict,
    GenerationSealedError,
    GenerationStateError,
    ResultGenerationStore,
)
from frisket.engine.store.runs import RunResultStore
from helpers import RunWriterAuthorityFixture, run_writer_authority_fixture


@dataclass(frozen=True)
class _ClaimedRun:
    op_id: int
    run_id: int
    authority: RunWriterAuthorityFixture


def _seed_project(
    tmp_path: Path,
) -> tuple[Project, int, int, list[int]]:
    project = Project.create(tmp_path / "result-generations.frisket", name="Heads")
    sheet_id = project.add_sheet("Rows")
    source_column_id = project.add_column(sheet_id, "source")
    output_column_id = project.add_column(
        sheet_id, "generated", type="text", ai_generated=True
    )
    project.add_rows(
        sheet_id,
        [
            {"source": "alpha"},
            {"source": "beta"},
            {"source": "gamma"},
            {"source": "delta"},
        ],
        {"source": source_column_id},
    )
    row_ids = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
        )
    ]
    return project, sheet_id, output_column_id, row_ids


def _start_claimed_run(
    project: Project,
    *,
    sheet_id: int,
    output_column_id: int,
    row_ids: list[int],
    label: str,
    replace_existing: bool = False,
) -> _ClaimedRun:
    spec = {"pattern": "(.*)", "output": "generated"}
    if replace_existing:
        spec["replace_existing"] = True
    op_id = project.append_op("map.regex_extract", spec, label=label)
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.regex_extract",
        row_ids=row_ids,
        total_rows=len(row_ids),
    )
    authority = run_writer_authority_fixture(
        project,
        run_id,
        output_column_ids={output_column_id},
    )
    return _ClaimedRun(op_id=op_id, run_id=run_id, authority=authority)


def _write(
    project: Project,
    claimed_run: _ClaimedRun,
    batch: list[dict[str, object]],
) -> None:
    RunResultStore(project).write_results(
        claimed_run.run_id,
        batch,
        **claimed_run.authority.kwargs(),
    )


def _release(project: Project, claimed_run: _ClaimedRun) -> None:
    token = claimed_run.authority.claim_token
    assert token is not None
    assert OutputColumnClaimStore(project).release(claim_token=token) == 1


def _declare(
    generations: ResultGenerationStore,
    claimed_run: _ClaimedRun,
    output_column_id: int,
    *,
    write_mode: str,
    compatibility_key: str = "sha256:regex-text-v1",
    defer_publication: bool = False,
    target_descriptor: dict[str, object] | None = None,
) -> None:
    token = claimed_run.authority.claim_token
    assert token is not None
    generations.declare(
        claimed_run.run_id,
        output_column_id,
        output_role="match",
        compatibility_key=compatibility_key,
        write_mode=write_mode,
        claim_token=token,
        target_descriptor=target_descriptor,
        defer_publication=defer_publication,
    )


def _seal(
    generations: ResultGenerationStore,
    claimed_run: _ClaimedRun,
    output_column_id: int,
    *,
    disposition: str = "completed",
) -> int:
    token = claimed_run.authority.claim_token
    assert token is not None
    return generations.seal(
        claimed_run.run_id,
        [output_column_id],
        claim_token=token,
        terminal_disposition=disposition,
    )


def _publish_initial_generation(
    project: Project,
    sheet_id: int,
    output_column_id: int,
    row_ids: list[int],
) -> tuple[ResultGenerationStore, _ClaimedRun]:
    generations = ResultGenerationStore(project)
    first = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=output_column_id,
        row_ids=row_ids,
        label="first generation",
    )
    _declare(generations, first, output_column_id, write_mode="create")
    _write(
        project,
        first,
        [
            {
                "row_id": row_id,
                "column_id": output_column_id,
                "value": f"first:{position}",
                "confidence": 0.9,
                "justification": f"first evidence {position}",
                "publication_effect": "publish_value",
            }
            for position, row_id in enumerate(row_ids)
        ],
    )
    _seal(generations, first, output_column_id)
    # Transitional scalar mirror: claims acquired by current writers still
    # capture this as the replacement fence while live readers cut over to
    # exact heads.
    RunResultStore(project).point_column_at_run(
        first.op_id, output_column_id, first.run_id
    )
    _release(project, first)
    return generations, first


def _descriptor(project: Project, column_id: int) -> dict[str, object]:
    row = project.db.execute(
        "SELECT type,format,semantic_type FROM columns WHERE id=?", (column_id,)
    ).fetchone()
    assert row is not None
    return {
        "type": row["type"],
        "format": row["format"],
        "semantic_type": row["semantic_type"],
    }


def test_descriptor_replacement_publishes_values_and_descriptor_at_seal_and_undo(
    tmp_path: Path,
) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(tmp_path)
    generations, first = _publish_initial_generation(
        project, sheet_id, column_id, row_ids
    )
    replacement: _ClaimedRun | None = None
    retry: _ClaimedRun | None = None
    original_retry: _ClaimedRun | None = None
    try:
        project.apply_edits(
            [{"row_id": row_ids[0], "column_id": column_id, "value": "manual"}]
        )
        replacement = _start_claimed_run(
            project,
            sheet_id=sheet_id,
            output_column_id=column_id,
            row_ids=row_ids,
            label="text to integer replacement",
            replace_existing=True,
        )
        target = {"type": "integer", "format": ",.2f", "semantic_type": "score"}
        _declare(
            generations,
            replacement,
            column_id,
            write_mode="replace_scope",
            compatibility_key="sha256:integer-score-v1",
            target_descriptor=target,
        )
        _write(
            project,
            replacement,
            [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "value": position,
                    "publication_effect": "publish_value",
                }
                for position, row_id in enumerate(row_ids)
            ],
        )

        assert _descriptor(project, column_id) == {
            "type": "text",
            "format": None,
            "semantic_type": None,
        }
        assert project.get_values(sheet_id, column_id)[row_ids[0]] == "manual"

        assert _seal(generations, replacement, column_id) == len(row_ids)
        assert _descriptor(project, column_id) == target
        assert project.get_values(sheet_id, column_id) == {
            row_id: position for position, row_id in enumerate(row_ids)
        }
        _release(project, replacement)

        assert project.undo() == replacement.op_id
        assert _descriptor(project, column_id) == {
            "type": "text",
            "format": None,
            "semantic_type": None,
        }
        assert project.get_values(sheet_id, column_id)[row_ids[0]] == "manual"

        assert project.redo() == replacement.op_id
        assert _descriptor(project, column_id) == target
        assert project.get_values(sheet_id, column_id)[row_ids[0]] == 0

        retry = _start_claimed_run(
            project,
            sheet_id=sheet_id,
            output_column_id=column_id,
            row_ids=row_ids,
            label="compatible integer replacement",
            replace_existing=True,
        )
        _declare(
            generations,
            retry,
            column_id,
            write_mode="replace_scope",
            compatibility_key="sha256:integer-score-v1",
            target_descriptor=target,
        )
        _write(
            project,
            retry,
            [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "value": position + 10,
                    "publication_effect": "publish_value",
                }
                for position, row_id in enumerate(row_ids)
            ],
        )
        assert _seal(generations, retry, column_id) == len(row_ids)
        _release(project, retry)
        retry = None

        assert project.undo() is not None
        assert project.undo() == replacement.op_id
        assert _descriptor(project, column_id)["type"] == "text"
        original_retry = _start_claimed_run(
            project,
            sheet_id=sheet_id,
            output_column_id=column_id,
            row_ids=row_ids,
            label="text replacement after integer undo",
            replace_existing=True,
        )
        _declare(
            generations,
            original_retry,
            column_id,
            write_mode="replace_scope",
            compatibility_key="sha256:regex-text-v1",
            target_descriptor={
                "type": "text",
                "format": None,
                "semantic_type": None,
            },
        )
    finally:
        if retry is not None:
            _release(project, retry)
        if original_retry is not None:
            _release(project, original_retry)
        project.close()


@pytest.mark.parametrize(
    ("disposition", "effect_count"), [("failed", 1), ("cancelled", 4)]
)
def test_aborted_descriptor_replacement_stays_unpublished_across_rebuild_and_retry(
    tmp_path: Path, disposition: str, effect_count: int
) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(tmp_path)
    generations, first = _publish_initial_generation(
        project, sheet_id, column_id, row_ids
    )
    replacement: _ClaimedRun | None = None
    retry: _ClaimedRun | None = None
    try:
        replacement = _start_claimed_run(
            project,
            sheet_id=sheet_id,
            output_column_id=column_id,
            row_ids=row_ids,
            label="aborted descriptor replacement",
            replace_existing=True,
        )
        _declare(
            generations,
            replacement,
            column_id,
            write_mode="replace_scope",
            compatibility_key="sha256:integer-v1",
            target_descriptor={
                "type": "integer",
                "format": None,
                "semantic_type": None,
            },
        )
        _write(
            project,
            replacement,
            [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "value": position,
                    "publication_effect": "publish_value",
                }
                for position, row_id in enumerate(row_ids[:effect_count])
            ],
        )

        assert _seal(generations, replacement, column_id, disposition=disposition) == 0
        assert _descriptor(project, column_id)["type"] == "text"
        assert {
            head.run_id for head in generations.read_cell_heads(column_id).values()
        } == {first.run_id}

        generations.rebuild_heads([column_id])
        assert {
            head.run_id for head in generations.read_cell_heads(column_id).values()
        } == {first.run_id}
        assert _seal(generations, replacement, column_id, disposition=disposition) == 0
        info = json.loads(
            project.db.execute(
                "SELECT undo_info FROM ops WHERE id=?", (replacement.op_id,)
            ).fetchone()["undo_info"]
        )
        assert info["unpublished_generation_columns"] == [column_id]

        _release(project, replacement)
        retry = _start_claimed_run(
            project,
            sheet_id=sheet_id,
            output_column_id=column_id,
            row_ids=row_ids,
            label="original descriptor retry",
            replace_existing=True,
        )
        _declare(
            generations,
            retry,
            column_id,
            write_mode="replace_scope",
            compatibility_key="sha256:regex-text-v1",
            target_descriptor={
                "type": "text",
                "format": None,
                "semantic_type": None,
            },
        )
    finally:
        if retry is not None:
            _release(project, retry)
        project.close()


@pytest.mark.parametrize("disposition", ["cancelled", "failed"])
def test_coupled_abort_keeps_previous_heads_and_cannot_publish_on_retry(
    tmp_path: Path, disposition: str
) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(tmp_path)
    try:
        generations, first = _publish_initial_generation(
            project, sheet_id, column_id, row_ids
        )
        second = _start_claimed_run(
            project,
            sheet_id=sheet_id,
            output_column_id=column_id,
            row_ids=row_ids,
            label="aborted coupled publication",
        )
        _declare(
            generations,
            second,
            column_id,
            write_mode="replace_scope",
            defer_publication=True,
        )
        _write(
            project,
            second,
            [
                {
                    "row_id": row_ids[0],
                    "column_id": column_id,
                    "value": "must not appear without child sheet",
                    "publication_effect": "publish_value",
                }
            ],
        )
        assert (
            generations.seal(
                second.run_id,
                [column_id],
                claim_token=second.authority.claim_token,
                terminal_disposition=disposition,
                publish=False,
            )
            == 0
        )
        assert _seal(generations, second, column_id, disposition=disposition) == 0
        heads = generations.read_cell_heads(column_id)
        assert all(head.run_id == first.run_id for head in heads.values())
        assert heads[row_ids[0]].value == "first:0"
        assert (
            project.db.execute(
                "SELECT value FROM results WHERE run_id=?", (second.run_id,)
            ).fetchone()
            is not None
        )
    finally:
        project.close()


def test_deferred_fresh_generation_stays_unpublished_until_seal(
    tmp_path: Path,
) -> None:
    project, sheet_id, output_column_id, row_ids = _seed_project(tmp_path)
    generations = ResultGenerationStore(project)
    claimed_run = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=output_column_id,
        row_ids=row_ids[:1],
        label="deferred fresh generation",
    )
    try:
        _declare(
            generations,
            claimed_run,
            output_column_id,
            write_mode="create",
            defer_publication=True,
        )
        binding = generations.get_binding(claimed_run.run_id, output_column_id)
        assert binding is not None and binding.state == "staged"
        with pytest.raises(
            GenerationDeclarationConflict,
            match="different immutable facts",
        ):
            _declare(
                generations,
                claimed_run,
                output_column_id,
                write_mode="create",
            )

        _write(
            project,
            claimed_run,
            [
                {
                    "row_id": row_ids[0],
                    "column_id": output_column_id,
                    "value": "deferred",
                    "publication_effect": "publish_value",
                }
            ],
        )
        assert generations.read_cell_heads(output_column_id) == {}
        assert generations.rebuild_heads([output_column_id]) == 0

        assert _seal(generations, claimed_run, output_column_id) == 1
        assert generations.read_cell_heads(output_column_id)[row_ids[0]].value == (
            "deferred"
        )
    finally:
        project.close()


def test_managed_run_treats_every_effect_as_attempted_and_refuses_same_run_retry(
    tmp_path: Path,
) -> None:
    project, sheet_id, output_column_id, row_ids = _seed_project(tmp_path)
    generations = ResultGenerationStore(project)
    claimed_run = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=output_column_id,
        row_ids=row_ids,
        label="managed retry admission",
    )
    store = RunResultStore(project)
    _declare(generations, claimed_run, output_column_id, write_mode="create")
    batch = [
        {
            "row_id": row_ids[0],
            "column_id": output_column_id,
            "value": "value",
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
            "error": "fixture error",
            "error_code": "fixture_error",
            "publication_effect": "publish_error",
        },
        {
            "row_id": row_ids[3],
            "column_id": output_column_id,
            "value": "hidden value",
            "publication_effect": "publish_value",
        },
    ]
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row_ids[3],))
    project.db.commit()
    try:
        _write(project, claimed_run, batch)
        assert (
            store.pending_run_row_scope_count(claimed_run.run_id, [output_column_id])
            == 0
        )
        assert (
            store.pending_run_row_scope_page_after(
                claimed_run.run_id, [output_column_id], after_position=-1, limit=10
            )
            == []
        )
        assert store.completed_result_row_ids(
            claimed_run.run_id, [output_column_id]
        ) == set(row_ids)

        before = (
            tuple(
                project.db.execute(
                    "SELECT row_id,column_id,value,error,error_code,outcome,"
                    "publication_effect FROM results WHERE run_id=? ORDER BY row_id",
                    (claimed_run.run_id,),
                ).fetchall()
            ),
            tuple(
                project.db.execute(
                    "SELECT completed_rows,failed_rows,cost_actual FROM runs "
                    "WHERE id=?",
                    (claimed_run.run_id,),
                ).fetchone()
            ),
            generations.read_cell_heads(output_column_id),
        )
        assert store.run_row_scope(claimed_run.run_id) == row_ids
        store.record_run_row_scope(claimed_run.run_id, row_ids)
        with pytest.raises(GenerationStateError, match="cannot change its row scope"):
            store.record_run_row_scope(claimed_run.run_id, row_ids[:-1])
        after = (
            tuple(
                project.db.execute(
                    "SELECT row_id,column_id,value,error,error_code,outcome,"
                    "publication_effect FROM results WHERE run_id=? ORDER BY row_id",
                    (claimed_run.run_id,),
                ).fetchall()
            ),
            tuple(
                project.db.execute(
                    "SELECT completed_rows,failed_rows,cost_actual FROM runs "
                    "WHERE id=?",
                    (claimed_run.run_id,),
                ).fetchone()
            ),
            generations.read_cell_heads(output_column_id),
        )
        assert after == before

        _seal(generations, claimed_run, output_column_id)
        store.finish_run(claimed_run.run_id)
        _release(project, claimed_run)
        admission = store.begin_run_resume(claimed_run.run_id)
        assert not admission.admitted
        assert (
            project.db.execute(
                "SELECT status FROM runs WHERE id=?", (claimed_run.run_id,)
            ).fetchone()[0]
            == "completed"
        )
    finally:
        project.close()


def test_staged_idempotent_write_refuses_undone_op_without_partial_mutation(
    tmp_path: Path,
) -> None:
    project, sheet_id, output_column_id, row_ids = _seed_project(tmp_path)
    generations, first = _publish_initial_generation(
        project, sheet_id, output_column_id, row_ids
    )
    second = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=output_column_id,
        row_ids=row_ids[:1],
        label="staged exact replay",
    )
    _declare(generations, second, output_column_id, write_mode="replace_scope")
    batch = [
        {
            "row_id": row_ids[0],
            "column_id": output_column_id,
            "value": "replacement",
            "publication_effect": "publish_value",
        }
    ]
    try:
        _write(project, second, batch)
        before = (
            tuple(
                project.db.execute(
                    "SELECT * FROM results WHERE run_id=?", (second.run_id,)
                ).fetchone()
            ),
            tuple(
                project.db.execute(
                    "SELECT completed_rows,failed_rows,cost_actual FROM runs "
                    "WHERE id=?",
                    (second.run_id,),
                ).fetchone()
            ),
            generations.read_cell_heads(output_column_id),
        )
        assert {head.run_id for head in before[2].values()} == {first.run_id}
        project.db.execute("UPDATE ops SET status='undone' WHERE id=?", (second.op_id,))
        project.db.commit()

        with pytest.raises(GenerationStateError, match="op is 'undone'"):
            _write(project, second, batch)

        after = (
            tuple(
                project.db.execute(
                    "SELECT * FROM results WHERE run_id=?", (second.run_id,)
                ).fetchone()
            ),
            tuple(
                project.db.execute(
                    "SELECT completed_rows,failed_rows,cost_actual FROM runs "
                    "WHERE id=?",
                    (second.run_id,),
                ).fetchone()
            ),
            generations.read_cell_heads(output_column_id),
        )
        assert after == before
    finally:
        project.close()


def test_fresh_results_stream_then_staged_subset_seals_exact_heads(
    tmp_path: Path,
) -> None:
    project, sheet_id, output_column_id, row_ids = _seed_project(tmp_path)
    generations = ResultGenerationStore(project)
    first = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=output_column_id,
        row_ids=row_ids,
        label="fresh progressive generation",
    )
    _declare(generations, first, output_column_id, write_mode="create")

    _write(
        project,
        first,
        [
            {
                "row_id": row_ids[0],
                "column_id": output_column_id,
                "value": "first:0",
                "confidence": 0.9,
                "justification": "first evidence 0",
                "publication_effect": "publish_value",
            }
        ],
    )
    assert set(generations.read_cell_heads(output_column_id)) == {row_ids[0]}
    assert generations.get_binding(first.run_id, output_column_id).state == "active"  # type: ignore[union-attr]

    _write(
        project,
        first,
        [
            {
                "row_id": row_id,
                "column_id": output_column_id,
                "value": f"first:{position}",
                "confidence": 0.9,
                "justification": f"first evidence {position}",
                "publication_effect": "publish_value",
            }
            for position, row_id in enumerate(row_ids[1:], start=1)
        ],
    )
    first_heads = generations.read_cell_heads(output_column_id)
    assert {head.run_id for head in first_heads.values()} == {first.run_id}
    assert first_heads[row_ids[0]].value == "first:0"
    assert first_heads[row_ids[0]].confidence == pytest.approx(0.9)
    assert first_heads[row_ids[0]].justification == "first evidence 0"

    _seal(generations, first, output_column_id)
    RunResultStore(project).point_column_at_run(
        first.op_id, output_column_id, first.run_id
    )
    _release(project, first)

    targeted = row_ids[:3]
    second = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=output_column_id,
        row_ids=targeted,
        label="exact subset replacement",
    )
    _declare(generations, second, output_column_id, write_mode="replace_scope")
    _write(
        project,
        second,
        [
            {
                "row_id": targeted[0],
                "column_id": output_column_id,
                "value": "second:value",
                "publication_effect": "publish_value",
            },
            {
                "row_id": targeted[1],
                "column_id": output_column_id,
                "value": None,
                "publication_effect": "publish_null",
            },
            {
                "row_id": targeted[2],
                "column_id": output_column_id,
                "value": None,
                "error": "deliberate fixture error",
                "error_code": "fixture_error",
                "publication_effect": "publish_error",
            },
        ],
    )

    # Existing output replacement is staged: no partial visibility and no
    # carry-forward copies into the new run.
    assert generations.read_cell_heads(output_column_id) == first_heads
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=? AND column_id=?",
            (second.run_id, output_column_id),
        ).fetchone()[0]
        == 3
    )

    _seal(generations, second, output_column_id)
    mixed = generations.read_cell_heads(output_column_id)
    assert [mixed[row_id].run_id for row_id in targeted] == [second.run_id] * 3
    assert mixed[row_ids[3]].run_id == first.run_id
    assert mixed[targeted[0]].value == "second:value"
    assert mixed[targeted[0]].publication_effect == "publish_value"
    assert mixed[targeted[1]].value is None
    assert mixed[targeted[1]].publication_effect == "publish_null"
    assert mixed[targeted[2]].value is None
    assert mixed[targeted[2]].publication_effect == "publish_error"
    assert mixed[targeted[2]].error == "deliberate fixture error"
    assert mixed[targeted[2]].error_code == "fixture_error"
    assert mixed[targeted[2]].op_id == second.op_id
    assert generations.origin_run_ids(output_column_id) == [
        second.run_id,
        first.run_id,
    ]
    assert generations.origin_run_ids(output_column_id, [targeted[0], targeted[1]]) == [
        second.run_id
    ]
    assert generations.has_mixed_origins(output_column_id)
    assert generations.is_generation_managed(output_column_id)
    assert generations.column_ids_for_op(second.op_id) == frozenset({output_column_id})


def test_rebuild_uses_applied_ops_includes_hidden_rows_and_restores_prior_heads(
    tmp_path: Path,
) -> None:
    project, sheet_id, output_column_id, row_ids = _seed_project(tmp_path)
    generations, first = _publish_initial_generation(
        project, sheet_id, output_column_id, row_ids
    )
    second = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=output_column_id,
        row_ids=row_ids[:2],
        label="newer subset",
    )
    _declare(generations, second, output_column_id, write_mode="replace_scope")
    _write(
        project,
        second,
        [
            {
                "row_id": row_ids[0],
                "column_id": output_column_id,
                "value": "new value",
                "publication_effect": "publish_value",
            },
            {
                "row_id": row_ids[1],
                "column_id": output_column_id,
                "value": None,
                "error": "new error",
                "publication_effect": "publish_error",
            },
        ],
    )
    _seal(generations, second, output_column_id)
    expected_mixed = generations.read_cell_heads(output_column_id)

    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row_ids[3],))
    project.db.execute("DELETE FROM cell_result_heads")
    project.db.commit()
    assert generations.rebuild_heads() == 4
    assert generations.read_cell_heads(output_column_id) == expected_mixed

    project.db.execute("UPDATE ops SET status='undone' WHERE id=?", (second.op_id,))
    project.db.commit()
    assert generations.rebuild_heads([output_column_id]) == 4
    restored = generations.read_cell_heads(output_column_id)
    assert {head.run_id for head in restored.values()} == {first.run_id}
    assert restored[row_ids[0]].value == "first:0"
    assert restored[row_ids[1]].value == "first:1"
    assert row_ids[3] in restored

    project.db.execute("UPDATE ops SET status='applied' WHERE id=?", (second.op_id,))
    project.db.commit()
    assert generations.rebuild_heads([output_column_id]) == 4
    assert generations.read_cell_heads(output_column_id) == expected_mixed


def test_declaration_is_idempotent_open_rejects_sealed_and_checks_base_contract(
    tmp_path: Path,
) -> None:
    project, sheet_id, output_column_id, row_ids = _seed_project(tmp_path)
    generations, first = _publish_initial_generation(
        project, sheet_id, output_column_id, row_ids
    )
    second = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=output_column_id,
        row_ids=row_ids[:1],
        label="compatible replacement",
    )
    _declare(generations, second, output_column_id, write_mode="replace_scope")
    binding = generations.get_binding(second.run_id, output_column_id)
    _declare(generations, second, output_column_id, write_mode="replace_scope")
    assert generations.get_binding(second.run_id, output_column_id) == binding

    with pytest.raises(GenerationDeclarationConflict, match="different immutable"):
        generations.declare(
            second.run_id,
            output_column_id,
            output_role="other-role",
            compatibility_key="sha256:regex-text-v1",
            write_mode="replace_scope",
            claim_token=str(second.authority.claim_token),
        )

    _seal(generations, second, output_column_id, disposition="partial")
    with pytest.raises(GenerationSealedError, match="is sealed"):
        _declare(generations, second, output_column_id, write_mode="replace_scope")
    with pytest.raises(GenerationStateError, match="sealed as 'partial'"):
        _seal(
            generations,
            second,
            output_column_id,
            disposition="cancelled",
        )
    assert (
        generations.seal(
            second.run_id,
            [output_column_id],
            claim_token="already-released-or-successor",
            terminal_disposition="partial",
        )
        == 0
    )

    # A third claim points at the first scalar base. Its incompatible managed
    # descriptor is refused even before any result can be written.
    _release(project, second)
    third = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=output_column_id,
        row_ids=row_ids[:1],
        label="incompatible replacement",
    )
    with pytest.raises(GenerationDeclarationConflict, match="different compatibility"):
        _declare(
            generations,
            third,
            output_column_id,
            write_mode="replace_scope",
            compatibility_key="sha256:different-contract",
        )
    assert generations.get_binding(first.run_id, output_column_id) is not None


def test_declaration_rejects_legacy_sibling_result_before_complete_binding_set(
    tmp_path: Path,
) -> None:
    project, sheet_id, output_column_id, row_ids = _seed_project(tmp_path)
    legacy_sibling_column_id = project.add_column(
        sheet_id, "legacy_sibling", type="text", ai_generated=True
    )
    generations = ResultGenerationStore(project)
    claimed = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=output_column_id,
        row_ids=row_ids,
        label="legacy result before declaration",
    )
    project.db.execute(
        "INSERT INTO results (run_id,row_id,column_id,value) VALUES (?,?,?,?)",
        (claimed.run_id, row_ids[0], legacy_sibling_column_id, '"legacy"'),
    )
    project.db.commit()

    with pytest.raises(
        GenerationDeclarationConflict, match="declaration set is already frozen"
    ):
        _declare(generations, claimed, output_column_id, write_mode="create")
    assert generations.get_binding(claimed.run_id, output_column_id) is None


def test_multi_output_declarations_freeze_before_effect_and_seal_as_a_group(
    tmp_path: Path,
) -> None:
    project, sheet_id, first_column_id, row_ids = _seed_project(tmp_path)
    second_column_id = project.add_column(
        sheet_id, "generated_second", type="text", ai_generated=True
    )
    late_column_id = project.add_column(
        sheet_id, "generated_late", type="text", ai_generated=True
    )
    op_id = project.append_op(
        "map.regex_extract", {"pattern": "(.*)"}, label="multi-output generation"
    )
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.regex_extract",
        row_ids=row_ids,
        total_rows=len(row_ids),
    )
    authority = run_writer_authority_fixture(
        project,
        run_id,
        output_column_ids={first_column_id, second_column_id, late_column_id},
    )
    claimed = _ClaimedRun(op_id=op_id, run_id=run_id, authority=authority)
    token = authority.claim_token
    assert token is not None

    generations = ResultGenerationStore(project)
    generations.declare(
        run_id,
        first_column_id,
        output_role="first",
        compatibility_key="sha256:multi-output-v1",
        write_mode="create",
        claim_token=token,
    )
    generations.declare(
        run_id,
        second_column_id,
        output_role="second",
        compatibility_key="sha256:multi-output-v1",
        write_mode="create",
        claim_token=token,
    )

    with pytest.raises(GenerationStateError, match="complete declared output set"):
        generations.seal(
            run_id,
            [first_column_id],
            claim_token=token,
            terminal_disposition="completed",
        )

    _write(
        project,
        claimed,
        [
            {
                "row_id": row_ids[0],
                "column_id": first_column_id,
                "value": "published",
                "publication_effect": "publish_value",
            }
        ],
    )
    with pytest.raises(
        GenerationDeclarationConflict, match="declaration set is already frozen"
    ):
        generations.declare(
            run_id,
            late_column_id,
            output_role="late",
            compatibility_key="sha256:multi-output-v1",
            write_mode="create",
            claim_token=token,
        )

    generations.seal(
        run_id,
        [second_column_id, first_column_id],
        claim_token=token,
        terminal_disposition="completed",
    )
    assert {binding.state for binding in generations.bindings_for_run(run_id)} == {
        "sealed"
    }
    with pytest.raises(
        GenerationDeclarationConflict, match="declaration set is already frozen"
    ):
        generations.declare(
            run_id,
            late_column_id,
            output_role="late",
            compatibility_key="sha256:multi-output-v1",
            write_mode="create",
            claim_token=token,
        )


def test_progressive_head_failure_rolls_back_result_and_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, sheet_id, output_column_id, row_ids = _seed_project(tmp_path)
    generations = ResultGenerationStore(project)
    claimed = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=output_column_id,
        row_ids=row_ids,
        label="atomic progressive write",
    )
    _declare(generations, claimed, output_column_id, write_mode="create")

    def fail_projection(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("injected head projection failure")

    monkeypatch.setattr(
        ResultGenerationStore,
        "_project_written_results_uncommitted",
        fail_projection,
    )
    with pytest.raises(RuntimeError, match="injected head projection failure"):
        _write(
            project,
            claimed,
            [
                {
                    "row_id": row_ids[0],
                    "column_id": output_column_id,
                    "value": "must roll back",
                    "publication_effect": "publish_value",
                }
            ],
        )

    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=?", (claimed.run_id,)
        ).fetchone()[0]
        == 0
    )
    run = RunResultStore(project).get_run(claimed.run_id)
    assert run is not None
    assert int(run["completed_rows"]) == 0
    assert generations.read_cell_heads(output_column_id) == {}
