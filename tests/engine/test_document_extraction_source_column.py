from contextlib import closing

from frisket.actions.document_extraction_types import ExtractionTemplate
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.document_extraction_read import load_positioned_document
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.store import Project
from frisket.engine.store.evidence import record_source_artifact, record_source_span


def test_extraction_adds_page_scoped_source_file_with_collision_free_name(
    tmp_path,
) -> None:
    with closing(Project.create(tmp_path / "source-column.frisket")) as project:
        sheet_id = project.add_sheet("PDF pages")
        document_column_id = project.add_column(sheet_id, "document", "file")
        blob_id = project.add_blob(
            b"page-scoped source",
            filename="report.pdf",
            mime="application/pdf",
        )
        artifact = record_source_artifact(
            project,
            artifact_kind="file",
            blob_hash=blob_id,
            media_type="application/pdf",
            page_count=3,
            metadata={
                "engine": "tesseract",
                "page_images": {"2": {"source_width": 100, "source_height": 100}},
            },
        )
        for text, x0, x1 in (("VALUE", 0.1, 0.3), ("SECOND PAGE", 0.4, 0.7)):
            record_source_span(
                project,
                artifact_id=artifact["id"],
                span_kind="region",
                page_start=2,
                page_end=2,
                bbox=[
                    {
                        "x0": x0,
                        "y0": 0.1,
                        "x1": x1,
                        "y1": 0.15,
                        "space": "page_normalized",
                    }
                ],
                quote=text,
            )
        [source_row_id] = project.add_rows(
            sheet_id,
            [
                {
                    "document": {
                        "blob": blob_id,
                        "filename": "report.pdf",
                        "mime": "application/pdf",
                        "page": 2,
                    }
                }
            ],
            {"document": document_column_id},
        )
        reference = load_positioned_document(project, blob_id, page=2)
        template = ExtractionTemplate(
            reference_blob_id=blob_id,
            reference_page=2,
            reference_fingerprint=reference.document.source_fingerprint,
            fields=[
                {
                    "id": "value",
                    "name": "Source document",
                    "key": {
                        "page": 2,
                        "box": {"x0": 0.09, "y0": 0.09, "x1": 0.31, "y1": 0.16},
                    },
                    "value": {
                        "page": 2,
                        "box": {"x0": 0.39, "y0": 0.09, "x1": 0.71, "y1": 0.16},
                    },
                }
            ],
        )
        result = run_typed_create_sheet_action(
            project,
            "project",
            typed_action_for_request(
                {
                    "action_id": "media.extract_document",
                    "scope": {
                        "kind": "sheet_rows",
                        "sheet_id": sheet_id,
                        "row_ids": [source_row_id],
                    },
                    "params": {
                        "source": "document",
                        "template": template.model_dump(mode="json"),
                    },
                    "output_names": {"Source document": "Extracted value"},
                    "sheet_name": "Extracted",
                    "idempotency_key": "source-column",
                }
            ),
        )

        assert result.status == "completed", result.errors
        output = result.outputs[0].ref
        assert list(output["columns"]) == ["Extracted value", "Source document 2"]
        [output_row_id] = project.visible_row_ids(output["sheet_id"])
        assert project.get_values(
            output["sheet_id"], output["columns"]["Extracted value"]
        ) == {output_row_id: "SECOND PAGE"}
        assert project.get_values(
            output["sheet_id"], output["columns"]["Source document 2"]
        ) == {
            output_row_id: {
                "blob": blob_id,
                "filename": "report.pdf",
                "mime": "application/pdf",
                "page": 2,
            }
        }
        assert {
            column["name"]: column["type"]
            for column in project.columns(output["sheet_id"])
        } == {"Extracted value": "text", "Source document 2": "file"}
        evidence_columns = {
            int(row["column_id"])
            for row in project.db.execute(
                "SELECT column_id FROM evidence_links WHERE sheet_id=?",
                (output["sheet_id"],),
            )
        }
        assert output["columns"]["Extracted value"] in evidence_columns
        assert output["columns"]["Source document 2"] not in evidence_columns
