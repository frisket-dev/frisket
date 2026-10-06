"""Small editor adapters over admitted document reads and the shared matcher."""

from frisket.actions.document_extract import DocumentExtractParams
from frisket.actions.types import SheetRows, TableError
from frisket.contracts.http.document_extraction import (
    ExtractionDocumentResponse,
    ExtractionPreviewResponse,
    ExtractionPreviewDocument,
    ExtractionSavedTemplate,
    ExtractionTemplatesResponse,
)
from frisket.engine.executor.document_extraction_read import (
    AdmittedPositionedDocumentReader,
    DocumentCell,
    document_cell,
    load_positioned_document,
)
from frisket.server.route_errors import RouteError


class DocumentExtractionService:
    def __init__(self, workspace):
        self.workspace = workspace

    def document(self, pid, *, sheet_id, column_id, row_id, cancelled=None):
        project = self.workspace.get(pid)
        source = document_cell(
            project, sheet_id=sheet_id, column_id=column_id, row_id=row_id
        )
        loaded = load_positioned_document(
            project, source.blob_id, page=source.page, cancelled=cancelled
        )
        return ExtractionDocumentResponse(
            row_id=row_id,
            blob_id=source.blob_id,
            reference_page=source.page,
            filename=loaded.filename,
            mime=loaded.mime,
            document=loaded.document,
        )

    def preview(self, pid, body, *, cancelled=None):
        project = self.workspace.get(pid)
        params = DocumentExtractParams(
            source=body.source,
            template=body.template,
            repeat_group_id=body.repeat_group_id,
        )
        if body.row_ids == []:
            return ExtractionPreviewResponse(documents=[])
        scope = SheetRows(sheet_id=body.sheet_id, row_ids=body.row_ids)
        reader = AdmittedPositionedDocumentReader(
            project, scope=scope, params=params, row_limit=12, cancelled=cancelled
        )
        documents = []
        remaining = 200
        truncated = False
        try:
            for _source, _loaded, result in reader.document_results(params):
                # Limit browser payload, not the matcher or its record boundaries.
                limited = result.model_copy(
                    update={"records": result.records[:remaining]}
                )
                truncated |= len(limited.records) != len(result.records)
                remaining -= len(limited.records)
                documents.append(
                    ExtractionPreviewDocument(
                        **{**reader.documents[-1], "result": limited}
                    )
                )
        finally:
            reader.close()
        source_count = next(
            (
                len(fact["row_ids"])
                for fact in reader.facts
                if fact.get("kind") == "sheet_rows_read"
            ),
            0,
        )
        truncated |= source_count > len(documents)
        return ExtractionPreviewResponse(documents=documents, truncated=truncated)

    def templates(self, pid, sheet_id):
        self.workspace.get(pid)
        return ExtractionTemplatesResponse(
            templates=[
                ExtractionSavedTemplate(
                    id=entry["id"],
                    name=entry["name"],
                    sheet_id=sheet_id,
                    reference_row_id=entry["spec"]["reference_row_id"],
                    spec=entry["spec"],
                )
                for entry in self.workspace.saved_recipes()
                if entry.get("spec", {}).get("action_kind") == "media.extract_document"
                and entry["spec"].get("project_id") == pid
                and entry["spec"].get("sheet_id") == sheet_id
            ]
        )

    def save(self, pid, body, *, cancelled=None):
        project = self.workspace.get(pid)
        params = DocumentExtractParams(
            source=body.source,
            template=body.template,
            repeat_group_id=body.repeat_group_id,
        )
        column = project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name=? AND active=1",
            (body.sheet_id, body.source),
        ).fetchone()
        reference = (
            None
            if column is None
            else document_cell(
                project,
                sheet_id=body.sheet_id,
                column_id=column["id"],
                row_id=body.reference_row_id,
            )
        )
        if reference is None or reference != DocumentCell(
            body.template.reference_blob_id, body.template.reference_page
        ):
            raise TableError(
                "invalid_input_ref",
                "Reference document no longer matches the selected cell",
            )
        from frisket.engine.document_extraction import compile_template

        compile_template(
            body.template,
            load_positioned_document(
                project,
                body.template.reference_blob_id,
                page=body.template.reference_page,
                cancelled=cancelled,
            ).document,
        )
        if body.id is not None:
            existing = self.workspace.saved_recipe_by_id(body.id)
            if (
                existing is None
                or existing["spec"].get("project_id") != pid
                or existing["spec"].get("sheet_id") != body.sheet_id
                or existing["spec"].get("action_kind") != "media.extract_document"
            ):
                raise RouteError(404, "Extraction template not found")
        spec = {
            "action_kind": "media.extract_document",
            "project_id": pid,
            "sheet_id": body.sheet_id,
            "reference_row_id": body.reference_row_id,
            "params": params.model_dump(mode="json"),
        }
        if cancelled is not None and cancelled():
            raise TableError("action_cancelled", "Template saving was cancelled")
        entry = self.workspace.save_recipe(body.name, spec, recipe_id=body.id)
        return ExtractionSavedTemplate(
            id=entry["id"],
            name=entry["name"],
            sheet_id=body.sheet_id,
            reference_row_id=body.reference_row_id,
            spec=spec,
        )
