from contextlib import closing

from frisket.engine.store import Project
from frisket.engine.store.document_text import ensure_document_text_role
from frisket.upgrades.project.domain_document_text_migration import (
    adopt_legacy_document_text,
)
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.output_claims import OutputColumnClaimStore
from helpers import run_writer_authority_fixture


def _setup(tmp_path):
    project = Project.create(tmp_path / "legacy.frisket")
    sheet = project.add_sheet("Documents")
    source = project.add_column(sheet, "File", type="file")
    left = project.add_column(sheet, "OCR A", type="text")
    right = project.add_column(sheet, "OCR B", type="text")
    blob = project.add_blob(b"fixture", mime="application/pdf")
    rows = project.add_rows(
        sheet, [{"File": {"blob": blob}} for _ in range(3)], {"File": source}
    )
    return project, sheet, source, left, right, blob, rows


def _publish(project, sheet, source, column, row, blob, text):
    op = project.append_op("media.ocr")
    with project.db:
        project.db.execute("BEGIN IMMEDIATE")
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            blob_hash=blob,
            page_count=1,
            source_sheet_id=sheet,
            source_row_id=row,
            source_column_id=source,
        )
        ref = PreparedContentStore(project).stage_reference(
            source_artifact_id=artifact["id"],
            producing_op_id=op,
            pages=[PreparedPageDraft(1, text)],
        )
    runs = RunResultStore(project)
    run = runs.start_run(op, sheet, "media.ocr", row_ids=[row], total_rows=1)
    auth = run_writer_authority_fixture(project, run, output_column_ids={column})
    generations = ResultGenerationStore(project)
    generations.declare(
        run,
        column,
        output_role="text",
        compatibility_key="sha256:test-text",
        write_mode="create",
        claim_token=auth.claim_token,
    )
    runs.write_results(
        run,
        [
            {
                "row_id": row,
                "column_id": column,
                "value": ref,
                "publication_effect": "publish_value",
            }
        ],
        **auth.kwargs(),
    )
    generations.seal(
        run, [column], claim_token=auth.claim_token, terminal_disposition="completed"
    )
    runs.point_column_at_run(op, column, run)
    OutputColumnClaimStore(project).release(claim_token=auth.claim_token)
    return ref.ref_id


def _adopt(project):
    project.db.execute("BEGIN IMMEDIATE")
    try:
        result = adopt_legacy_document_text(project.db)
        project.db.commit()
        return result
    except BaseException:
        project.db.rollback()
        raise


def test_reuses_common_column_and_preserves_empty_and_missing_rows(tmp_path):
    project, sheet, source, left, right, blob, rows = _setup(tmp_path)
    with closing(project):
        ref = _publish(project, sheet, source, left, rows[0], blob, "")
        before = [
            tuple(row)
            for row in project.db.execute("SELECT * FROM prepared_page_versions")
        ]
        assert _adopt(project) == {source: left}
        assert (
            project.db.execute(
                "SELECT prepared_ref_id FROM current_cell_values WHERE row_id=? AND column_id=?",
                (rows[0], left),
            ).fetchone()[0]
            == ref
        )
        assert (
            project.db.execute(
                "SELECT 1 FROM current_cell_values WHERE row_id=? AND column_id=?",
                (rows[1], left),
            ).fetchone()
            is None
        )
        assert before == [
            tuple(row)
            for row in project.db.execute("SELECT * FROM prepared_page_versions")
        ]
        assert _adopt(project) == {}


def test_mixed_rows_freeze_compact_refs_and_manual_correction(tmp_path):
    project, sheet, source, left, right, blob, rows = _setup(tmp_path)
    with closing(project):
        ref1 = _publish(project, sheet, source, left, rows[0], blob, "original first")
        ref2 = _publish(project, sheet, source, right, rows[1], blob, "second")
        project.apply_edits(
            [{"row_id": rows[0], "column_id": left, "value": "corrected first"}]
        )
        project.add_column(sheet, "File text", type="text")
        before = [
            tuple(row)
            for row in project.db.execute("SELECT * FROM prepared_page_versions")
        ]
        adopted = _adopt(project)[source]
        assert adopted not in (left, right)
        assert (
            project.db.execute(
                "SELECT name FROM columns WHERE id=?", (adopted,)
            ).fetchone()[0]
            == "File text_2"
        )
        values = project.get_values(sheet, adopted, rows)
        assert values[rows[0]] == "corrected first"
        assert values[rows[1]] == "second"
        assert (
            project.db.execute(
                "SELECT prepared_ref_id FROM document_text_manual_pins WHERE row_id=? AND text_column_id=?",
                (rows[0], adopted),
            ).fetchone()[0]
            == ref1
        )
        cells = [
            tuple(row)
            for row in project.db.execute(
                "SELECT row_id,value_kind,value FROM cells WHERE column_id=? ORDER BY row_id",
                (adopted,),
            )
        ]
        assert cells == [
            (rows[0], "prepared_content_ref", ref1),
            (rows[1], "prepared_content_ref", ref2),
        ]
        assert before == [
            tuple(row)
            for row in project.db.execute("SELECT * FROM prepared_page_versions")
        ]
        assert _adopt(project) == {}


def test_explicit_role_is_never_replaced_by_newer_alternative(tmp_path):
    project, sheet, source, left, right, blob, rows = _setup(tmp_path)
    with closing(project):
        _publish(project, sheet, source, left, rows[0], blob, "chosen")
        _publish(project, sheet, source, right, rows[0], blob, "newest")
        ensure_document_text_role(
            project, source_column_id=source, text_column_id=left, commit=True
        )
        assert _adopt(project) == {}
        assert (
            project.db.execute(
                "SELECT text_column_id FROM document_text_column_roles WHERE source_column_id=?",
                (source,),
            ).fetchone()[0]
            == left
        )


def test_prefers_older_complete_nonblank_over_newer_empty(tmp_path):
    project, sheet, source, left, right, blob, rows = _setup(tmp_path)
    with closing(project):
        _publish(project, sheet, source, left, rows[0], blob, "existing text")
        _publish(project, sheet, source, right, rows[0], blob, "")
        assert _adopt(project) == {source: left}


def test_common_manual_column_gains_pin_without_copy(tmp_path):
    project, sheet, source, left, right, blob, rows = _setup(tmp_path)
    with closing(project):
        ref = _publish(project, sheet, source, left, rows[0], blob, "original")
        project.apply_edits(
            [{"row_id": rows[0], "column_id": left, "value": "correction"}]
        )
        before = project.db.execute("SELECT count(*) FROM edits").fetchone()[0]
        assert _adopt(project) == {source: left}
        assert (
            project.db.execute(
                "SELECT prepared_ref_id FROM document_text_manual_pins WHERE row_id=? AND text_column_id=?",
                (rows[0], left),
            ).fetchone()[0]
            == ref
        )
        assert project.db.execute("SELECT count(*) FROM edits").fetchone()[0] == before


def test_adoption_rolls_back_then_can_resume(tmp_path):
    project, sheet, source, left, right, blob, rows = _setup(tmp_path)
    with closing(project):
        _publish(project, sheet, source, left, rows[0], blob, "first")
        _publish(project, sheet, source, right, rows[1], blob, "second")
        before = project.db.execute("SELECT count(*) FROM columns").fetchone()[0]
        project.db.execute("BEGIN IMMEDIATE")
        adopt_legacy_document_text(project.db)
        project.db.rollback()
        assert (
            project.db.execute("SELECT count(*) FROM columns").fetchone()[0] == before
        )
        assert (
            project.db.execute(
                "SELECT count(*) FROM document_text_column_roles"
            ).fetchone()[0]
            == 0
        )
        assert source in _adopt(project)


def test_failed_result_facts_survive_adoption(tmp_path):
    project, sheet, source, left, right, blob, rows = _setup(tmp_path)
    with closing(project):
        _publish(project, sheet, source, left, rows[0], blob, "successful")
        op = project.append_op("media.ocr")
        runs = RunResultStore(project)
        run = runs.start_run(op, sheet, "media.ocr", row_ids=[rows[1]], total_rows=1)
        auth = run_writer_authority_fixture(project, run, output_column_ids={right})
        generations = ResultGenerationStore(project)
        generations.declare(
            run,
            right,
            output_role="text",
            compatibility_key="sha256:test-text",
            write_mode="create",
            claim_token=auth.claim_token,
        )
        runs.write_results(
            run,
            [
                {
                    "row_id": rows[1],
                    "column_id": right,
                    "error": "OCR failed",
                    "error_code": "model_error",
                    "outcome": "model_error",
                    "publication_effect": "publish_error",
                }
            ],
            **auth.kwargs(),
        )
        generations.seal(
            run, [right], claim_token=auth.claim_token, terminal_disposition="failed"
        )
        OutputColumnClaimStore(project).release(claim_token=auth.claim_token)
        before = [
            tuple(row)
            for row in project.db.execute(
                "SELECT * FROM results ORDER BY run_id,row_id,column_id"
            )
        ]
        assert _adopt(project) == {source: left}
        assert before == [
            tuple(row)
            for row in project.db.execute(
                "SELECT * FROM results ORDER BY run_id,row_id,column_id"
            )
        ]
        assert (
            project.db.execute(
                "SELECT 1 FROM current_cell_values WHERE column_id=? AND row_id=?",
                (left, rows[1]),
            ).fetchone()
            is None
        )
