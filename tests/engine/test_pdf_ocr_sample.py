from contextlib import closing

import pytest

from frisket.contracts.action import Receipt
from frisket.engine.store import Project
from frisket.engine.store.pdf_ocr_sample import (
    capture_pdf_ocr_sample,
    publish_pdf_ocr_sample,
    publish_cached_pdf_ocr_sample,
)
from frisket.engine.store.receipts import ReceiptStore


def test_sample_publishes_receipt_owned_ocr_and_undo_redo(tmp_path):
    with closing(Project.create(tmp_path / "sample.frisket")) as project:
        sheet = project.add_sheet("Originals")
        source_col = project.add_column(sheet, "File", type="file")
        digest = project.add_blob(
            b"%PDF-fake", filename="a.pdf", mime="application/pdf"
        )
        value = {"blob": digest}
        row = project.add_rows(sheet, [{"File": value}], {"File": source_col})[0]
        capture = capture_pdf_ocr_sample(
            project,
            sheet_id=sheet,
            row_id=row,
            source_column_id=source_col,
            blob_hash=digest,
            filename="a.pdf",
            page_count=2,
            page_number=2,
            expected_source_value=value,
            ocr_action_params={"engine": "rapidocr"},
        )
        receipt_id = "receipt-sample"
        ReceiptStore(project).insert(
            Receipt(
                receipt_id=receipt_id,
                project_id="project",
                action_id="sample",
                action_kind="media.ocr",
                params_hash="sha256:sample",
                status="running",
            )
        )
        result = publish_pdf_ocr_sample(
            project,
            capture,
            page={"page": 2, "text": "Page two", "blocks": []},
            receipt_id=receipt_id,
        )
        assert (
            project.get_values(sheet, result["text_column_id"], [row])[row]
            == "Page two"
        )
        assert ReceiptStore(project).parsed_by_id(receipt_id).run_id == result["run_id"]
        evidence = project.db.execute(
            "SELECT link_role,receipt_id FROM evidence_links WHERE run_id=?",
            (result["run_id"],),
        ).fetchone()
        assert tuple(evidence) == ("media_ocr_grounding", receipt_id)
        assert (
            project.db.execute(
                "SELECT action_kind FROM runs WHERE id=?", (result["run_id"],)
            ).fetchone()[0]
            == "media.ocr"
        )
        assert project.undo() == result["operation_id"]
        assert not project.get_column(result["text_column_id"])["active"]
        assert project.redo() == result["operation_id"]
        assert (
            project.get_values(sheet, result["text_column_id"], [row])[row]
            == "Page two"
        )


def test_sample_rejects_changed_file_before_creating_undo_step(tmp_path):
    with closing(Project.create(tmp_path / "sample.frisket")) as project:
        sheet = project.add_sheet("Originals")
        source_col = project.add_column(sheet, "File", type="file")
        digest = project.add_blob(
            b"%PDF-fake", filename="a.pdf", mime="application/pdf"
        )
        value = {"blob": digest}
        row = project.add_rows(sheet, [{"File": value}], {"File": source_col})[0]
        capture = capture_pdf_ocr_sample(
            project,
            sheet_id=sheet,
            row_id=row,
            source_column_id=source_col,
            blob_hash=digest,
            filename="a.pdf",
            page_count=1,
            page_number=1,
            expected_source_value=value,
            ocr_action_params={"engine": "rapidocr"},
        )
        ReceiptStore(project).insert(
            Receipt(
                receipt_id="r",
                project_id="project",
                action_id="sample",
                action_kind="media.ocr",
                params_hash="sha256:sample",
                status="running",
            )
        )
        project.apply_edits([{"row_id": row, "column_id": source_col, "value": None}])
        before = project.op_cursor
        with pytest.raises(ValueError):
            publish_pdf_ocr_sample(
                project, capture, page={"page": 1, "text": "stale"}, receipt_id="r"
            )
        assert project.op_cursor == before
        assert ReceiptStore(project).parsed_by_id("r").run_id is None


def test_successive_samples_keep_other_pages_and_manual_edit(tmp_path):
    with closing(Project.create(tmp_path / "sample.frisket")) as project:
        sheet = project.add_sheet("Originals")
        source_col = project.add_column(sheet, "File", type="file")
        digest = project.add_blob(
            b"%PDF-fake", filename="a.pdf", mime="application/pdf"
        )
        value = {"blob": digest}
        row = project.add_rows(sheet, [{"File": value}], {"File": source_col})[0]

        def publish(number, text, receipt_id):
            capture = capture_pdf_ocr_sample(
                project,
                sheet_id=sheet,
                row_id=row,
                source_column_id=source_col,
                blob_hash=digest,
                filename="a.pdf",
                page_count=2,
                page_number=number,
                expected_source_value=value,
                ocr_action_params={"engine": "rapidocr"},
            )
            ReceiptStore(project).insert(
                Receipt(
                    receipt_id=receipt_id,
                    project_id="project",
                    action_id=receipt_id,
                    action_kind="media.ocr",
                    params_hash="sha256:sample",
                    status="running",
                )
            )
            return publish_pdf_ocr_sample(
                project,
                capture,
                page={"page": number, "text": text, "blocks": []},
                receipt_id=receipt_id,
            )

        first = publish(1, "Page one", "r1")
        second = publish(2, "Page two", "r2")
        assert second["text_column_id"] == first["text_column_id"]
        assert (
            project.get_values(sheet, first["text_column_id"], [row])[row]
            == "Page one\n\nPage two"
        )
        from frisket.engine.store.ocr_word_stream import resolve_current_ocr_evidence

        evidence = resolve_current_ocr_evidence(
            project,
            sheet_id=sheet,
            row_id=row,
            column_id=first["text_column_id"],
            blob_hash=digest,
        )
        assert len(evidence) == 1
        assert {span["quote"] for span in evidence[0].spans} == {"Page one", "Page two"}

        project.apply_edits(
            [
                {
                    "row_id": row,
                    "column_id": first["text_column_id"],
                    "value": "Manual correction",
                }
            ]
        )
        third = publish(1, "New OCR", "r3")
        assert third["text_column_id"] != first["text_column_id"]
        assert (
            project.get_values(sheet, first["text_column_id"], [row])[row]
            == "Manual correction"
        )
        assert project.get_column(third["text_column_id"])["hidden"]


def test_cached_page_reuses_provider_result_for_separate_occurrence(tmp_path):
    with closing(Project.create(tmp_path / "sample.frisket")) as project:
        sheet = project.add_sheet("Originals")
        source_col = project.add_column(sheet, "File", type="file")
        digest = project.add_blob(
            b"%PDF-fake", filename="a.pdf", mime="application/pdf"
        )
        value = {"blob": digest}
        first, second = project.add_rows(
            sheet, [{"File": value}, {"File": value}], {"File": source_col}
        )

        def capture(row):
            return capture_pdf_ocr_sample(
                project,
                sheet_id=sheet,
                row_id=row,
                source_column_id=source_col,
                blob_hash=digest,
                filename="a.pdf",
                page_count=1,
                page_number=1,
                expected_source_value=value,
                ocr_action_params={"engine": "rapidocr"},
            )

        ReceiptStore(project).insert(
            Receipt(
                receipt_id="first",
                project_id="project",
                action_id="sample",
                action_kind="media.ocr",
                params_hash="sha256:sample",
                status="running",
            )
        )
        original = publish_pdf_ocr_sample(
            project,
            capture(first),
            page={"page": 1, "text": "Cached OCR", "blocks": []},
            receipt_id="first",
        )
        reused = publish_cached_pdf_ocr_sample(
            project,
            capture(second),
            prepared_ref_id=original["prepared_ref_id"],
            project_id="project",
        )
        assert reused["source_artifact_id"] != original["source_artifact_id"]
        assert (
            project.get_values(sheet, reused["text_column_id"], [second])[second]
            == "Cached OCR"
        )
        receipt = ReceiptStore(project).parsed_by_id(reused["receipt_id"])
        assert receipt.status == "completed"
        assert receipt.run_id == reused["run_id"]
        assert project.undo() == reused["operation_id"]
        assert (
            project.get_values(sheet, reused["text_column_id"], [second])[second]
            is None
        )
        assert (
            project.get_values(sheet, original["text_column_id"], [first])[first]
            == "Cached OCR"
        )
