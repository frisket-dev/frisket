from __future__ import annotations

from pathlib import Path

import pytest

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.runner.preparation import _validate_managed_resume
from frisket.engine.store.result_generations import (
    GenerationStateError,
    ResultGenerationStore,
)
from frisket.engine.store.runs import RunResultStore
from test_result_generation_store import (
    _declare,
    _release,
    _seed_project,
    _start_claimed_run,
)
from test_sheet_refresh import _build, _refresh


def _mutation_snapshot(project) -> tuple[object, ...]:  # noqa: ANN001
    return tuple(
        int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "ops",
            "runs",
            "run_rows",
            "run_scopes",
            "results",
            "model_calls",
            "receipts",
            "output_column_claims",
        )
    )


def _document_text_admission(project, tmp_path):
    from frisket.engine.store.document_text import ensure_document_text_role
    from tests.engine.test_native_pdf_text_action import _pdf_bytes, _request, _source

    sheet, source, row, _digest = _source(
        project, tmp_path, pdf=_pdf_bytes("first designated text")
    )
    text = project.add_column(sheet, "Text", type="text", ai_generated=True)
    ensure_document_text_role(
        project, source_column_id=source, text_column_id=text, commit=True
    )
    request = {
        **_request(sheet, row, key="designated-first-generation"),
        "output_names": {"text": "Text"},
        "replace_existing": True,
        "expected_output_columns": {"Text": text},
    }
    return sheet, source, row, text, request


def _seed_prepared_base_text(project, *, sheet, source, row, target):
    from frisket.engine.store.cell_writes import (
        BaseCellWrite,
        create_base_cell_producer,
        initialize_base_cells,
    )
    from frisket.engine.store.evidence import record_source_artifact
    from frisket.engine.store.prepared_content import (
        PreparedContentStore,
        PreparedPageDraft,
    )

    blob_hash = str(
        project.db.execute(
            "SELECT json_extract(value,'$.blob') FROM current_cell_values "
            "WHERE row_id=? AND column_id=?",
            (row, source),
        ).fetchone()[0]
    )
    project.db.execute("BEGIN IMMEDIATE")
    op_id = project.append_op("document_text.seed", commit=False)
    artifact = record_source_artifact(
        project,
        artifact_kind="document",
        media_type="application/pdf",
        blob_hash=blob_hash,
        page_count=1,
        source_sheet_id=sheet,
        source_row_id=row,
        source_column_id=source,
    )
    ref = PreparedContentStore(project).stage_reference(
        source_artifact_id=int(artifact["id"]),
        producing_op_id=op_id,
        pages=[PreparedPageDraft(1, "seeded exact text")],
    )
    producer_id = create_base_cell_producer(
        project.db, stage_id=f"op:{op_id}", op_id=op_id
    )
    initialize_base_cells(
        project.db,
        producer_id=producer_id,
        cells=[BaseCellWrite(row, target, ref)],
    )
    project.db.commit()
    return ref.ref_id


def test_generation_free_designated_text_publishes_ordinary_first_generation(
    tmp_path,
):
    from contextlib import closing

    from frisket.engine.store import Project
    from frisket.engine.store.prepared_content import PreparedContentStore

    with closing(Project.create(tmp_path / "first-text.frisket")) as project:
        sheet, _source, row, text, request = _document_text_admission(project, tmp_path)
        result = run_action_spec(project, request, project_id="first-text")

        assert result.status == "completed", result.errors
        assert project.get_values(sheet, text, [row]) == {row: "first designated text"}
        binding = ResultGenerationStore(project).get_binding(result.run_id, text)
        assert binding is not None and binding.state == "sealed"
        head = ResultGenerationStore(project).read_cell_heads(text, row_ids=[row])[row]
        assert head.prepared_ref_id is not None
        assert PreparedContentStore(project).resolve(head.prepared_ref_id).text == (
            "first designated text"
        )


def test_generation_free_designated_text_admits_ocr_without_adopting_blocks(tmp_path):
    from contextlib import closing

    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
    from frisket.engine.store import Project

    with closing(Project.create(tmp_path / "first-ocr.frisket")) as project:
        _sheet, _source, _row, text, request = _document_text_admission(
            project, tmp_path
        )
        request.update(
            action_id="media.ocr",
            params={"source": "source", "engine": "rapidocr"},
            output_names={"text": "Text", "blocks": "Positions"},
            expected_output_columns={"Text": text, "Positions": None},
        )
        before = _mutation_snapshot(project)
        plan = build_typed_map_rows_plan(project, typed_action_for_request(request))
        assert dict(plan.output_target_preconditions) == {
            "Text": text,
            "Positions": None,
        }
        assert _mutation_snapshot(project) == before


@pytest.mark.parametrize(
    "mismatch",
    [
        "unbound",
        "other-source",
        "unfenced",
        "different-target",
        "existing-value",
        "stale-prepared",
    ],
)
def test_first_document_text_admission_requires_exact_generation_free_role(
    tmp_path, mismatch
):
    from contextlib import closing

    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import (
        TypedMapRowsPlanError,
        build_typed_map_rows_plan,
    )
    from frisket.engine.store import Project

    with closing(Project.create(tmp_path / "refused-text.frisket")) as project:
        sheet, source, row, text, request = _document_text_admission(project, tmp_path)
        if mismatch == "unbound":
            project.db.execute(
                "DELETE FROM document_text_column_roles WHERE source_column_id=?",
                (source,),
            )
            project.db.commit()
        elif mismatch == "other-source":
            project.add_column(sheet, "another source", type="file")
            request["params"] = {"source": "another source"}
        elif mismatch == "unfenced":
            request.pop("expected_output_columns")
        elif mismatch == "different-target":
            request["expected_output_columns"] = {"Text": source}
        elif mismatch == "existing-value":
            project.apply_edits(
                [{"row_id": row, "column_id": text, "value": "keep me"}]
            )
        else:
            _seed_prepared_base_text(
                project, sheet=sheet, source=source, row=row, target=text
            )
            project.apply_edits(
                [
                    {
                        "row_id": row,
                        "column_id": source,
                        "value": {
                            "blob": "sha256:" + "9" * 64,
                            "mime": "application/pdf",
                        },
                    }
                ]
            )
        before = _mutation_snapshot(project)
        with pytest.raises(TypedMapRowsPlanError):
            build_typed_map_rows_plan(project, typed_action_for_request(request))
        assert _mutation_snapshot(project) == before


def test_seeded_exact_prepared_text_is_admitted_without_copying_body(tmp_path):
    from contextlib import closing

    from frisket.engine.store import Project
    from frisket.engine.store.prepared_content import PreparedContentStore

    with closing(Project.create(tmp_path / "seeded-text.frisket")) as project:
        sheet, source, row, text, request = _document_text_admission(project, tmp_path)
        seeded = _seed_prepared_base_text(
            project, sheet=sheet, source=source, row=row, target=text
        )

        result = run_action_spec(project, request, project_id="seeded-text")

        assert result.status == "completed", result.errors
        assert PreparedContentStore(project).resolve(seeded).text == "seeded exact text"
        assert project.get_values(sheet, text, [row]) == {row: "first designated text"}


def test_seeded_unrelated_ai_target_is_not_adopted(tmp_path):
    from contextlib import closing

    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import (
        TypedMapRowsPlanError,
        build_typed_map_rows_plan,
    )
    from frisket.engine.store import Project

    with closing(Project.create(tmp_path / "unrelated-seeded.frisket")) as project:
        sheet, source, row, _text, request = _document_text_admission(
            project, tmp_path
        )
        unrelated = project.add_column(
            sheet, "Unrelated Text", type="text", ai_generated=True
        )
        _seed_prepared_base_text(
            project, sheet=sheet, source=source, row=row, target=unrelated
        )
        request["output_names"] = {"text": "Unrelated Text"}
        request["expected_output_columns"] = {"Unrelated Text": unrelated}

        before = _mutation_snapshot(project)
        with pytest.raises(TypedMapRowsPlanError):
            build_typed_map_rows_plan(project, typed_action_for_request(request))
        assert _mutation_snapshot(project) == before


def test_runner_rechecks_document_text_role_before_adopting_generation_free_target(
    tmp_path,
):
    from contextlib import closing

    from frisket.actions.system import typed_action_for_request
    from frisket.ai.llm import ModelRouter
    from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
    from frisket.engine.runner import MapRunner
    from frisket.engine.store import Project
    from frisket.engine.store.result_generations import GenerationDeclarationConflict
    from frisket.execution.attempt_authority import UnroutedOnlyAuthority

    with closing(Project.create(tmp_path / "changed-role.frisket")) as project:
        _sheet, source, _row, _text, request = _document_text_admission(
            project, tmp_path
        )
        plan = build_typed_map_rows_plan(project, typed_action_for_request(request))
        project.db.execute(
            "DELETE FROM document_text_column_roles WHERE source_column_id=?", (source,)
        )
        project.db.commit()
        runner = MapRunner(
            project,
            ModelRouter(keys={}),
            authority=UnroutedOnlyAuthority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        before = _mutation_snapshot(project)
        with pytest.raises(
            GenerationDeclarationConflict, match="no generation history"
        ):
            runner.prepare_run(plan.spec_dict(), program=plan.program)
        assert _mutation_snapshot(project) == before


@pytest.mark.parametrize("default_hidden", [False, True])
def test_managed_resume_only_accepts_exact_open_recovery(
    tmp_path: Path, default_hidden: bool
) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(tmp_path)
    claimed_run = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=column_id,
        row_ids=row_ids,
        label="managed recovery",
    )
    generations = ResultGenerationStore(project)
    _declare(generations, claimed_run, column_id, write_mode="create")
    store = RunResultStore(project)
    fields = [
        {"name": "generated", "column_type": "text", "default_hidden": default_hidden}
    ]
    bindings = generations.bindings_for_run(claimed_run.run_id)
    try:
        _validate_managed_resume(
            project,
            store,
            run_id=claimed_run.run_id,
            sheet_id=sheet_id,
            fields=fields,
            bindings=bindings,
            explicit_resume_scope=row_ids,
        )
        with pytest.raises(GenerationStateError, match="change its row scope"):
            _validate_managed_resume(
                project,
                store,
                run_id=claimed_run.run_id,
                sheet_id=sheet_id,
                fields=fields,
                bindings=bindings,
                explicit_resume_scope=row_ids[:-1],
            )
        with pytest.raises(GenerationStateError, match="output descriptors"):
            _validate_managed_resume(
                project,
                store,
                run_id=claimed_run.run_id,
                sheet_id=sheet_id,
                fields=[{"name": "generated", "column_type": "number"}],
                bindings=bindings,
                explicit_resume_scope=None,
            )
    finally:
        _release(project, claimed_run)
        project.close()


def test_managed_sheet_refresh_refuses_before_rows_or_receipts_change(
    tmp_path: Path,
) -> None:
    project, _parent_id, _source_column_id, child_id, _parent_row_ids = _build(tmp_path)
    column_id = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? ORDER BY position LIMIT 1",
            (child_id,),
        ).fetchone()[0]
    )
    row_ids = [
        int(row[0])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (child_id,)
        ).fetchall()
    ]
    claimed_run = _start_claimed_run(
        project,
        sheet_id=child_id,
        output_column_id=column_id,
        row_ids=row_ids,
        label="refresh gate generation",
    )
    _declare(
        ResultGenerationStore(project),
        claimed_run,
        column_id,
        write_mode="create",
    )
    try:
        before = (
            _mutation_snapshot(project),
            tuple(
                tuple(row)
                for row in project.db.execute(
                    "SELECT id,position,hidden FROM rows WHERE sheet_id=? "
                    "ORDER BY position",
                    (child_id,),
                ).fetchall()
            ),
            project.op_cursor,
        )
        result = run_action_spec(
            project,
            _refresh(sheet_id=child_id, key="managed-refresh@sha256:test"),
            project_id="p-ref",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "refresh_unsupported"
        assert result.errors[0].details == {
            "sheet_id": child_id,
            "column_id": column_id,
            "reason": "generation_managed_rows",
        }
        after = (
            _mutation_snapshot(project),
            tuple(
                tuple(row)
                for row in project.db.execute(
                    "SELECT id,position,hidden FROM rows WHERE sheet_id=? "
                    "ORDER BY position",
                    (child_id,),
                ).fetchall()
            ),
            project.op_cursor,
        )
        assert after == before
    finally:
        _release(project, claimed_run)
        project.close()
