"""Small editor adapters over admitted document reads and the shared matcher."""

import logging

from frisket.actions.document_extract import DocumentExtractParams
from frisket.actions.types import SheetRows, TableError
from frisket.contracts.http.document_extraction import (
    ExtractionDocumentResponse,
    ExtractionPreviewResponse,
    ExtractionPreviewDocument,
    ExtractionSavedLayout,
    ExtractionScopeCountsResponse,
    ExtractionTemplatesResponse,
    ExtractionTemplateSave,
)
from frisket.engine.executor.document_extraction_read import (
    AdmittedPositionedDocumentReader,
    DocumentCell,
    document_cell,
    load_positioned_document,
)
from frisket.server.route_errors import RouteError
from frisket.engine.store import extraction_layouts

_log = logging.getLogger(__name__)


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
        scope = body.scope
        if scope.kind == "layout" and scope.layout_id is None:
            scope = scope.model_copy(update={"layout_id": body.layout_id})
        if body.layout_id is not None:
            extraction_layouts.validate_layout_scope(
                project, body.layout_id, body.sheet_id, body.source
            )
        row_ids = extraction_layouts.resolve_document_scope(
            project, sheet_id=body.sheet_id, source=body.source, scope=scope
        )
        if not row_ids:
            return ExtractionPreviewResponse(documents=[])
        scope = SheetRows(sheet_id=body.sheet_id, row_ids=row_ids)
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

    def _import_saved_templates(self, project, pid, sheet_id, source):
        """Copy old editor recipes once without modifying workspace recipes."""
        for entry in self.workspace.saved_recipes():
            if not isinstance(entry, dict):
                continue
            spec = entry.get("spec", {})
            if not isinstance(spec, dict):
                continue
            params = spec.get("params", {})
            if (
                spec.get("action_kind") != "media.extract_document"
                or spec.get("project_id") != pid
                or spec.get("sheet_id") != sheet_id
            ):
                continue
            try:
                if not isinstance(params, dict):
                    raise ValueError("Invalid saved parameters")
                if params.get("source") != source:
                    continue
                recipe_id = entry["id"]
                if type(recipe_id) is not int or recipe_id <= 0:
                    raise ValueError("Invalid saved recipe identity")
                body = ExtractionTemplateSave(
                    sheet_id=sheet_id,
                    source=source,
                    reference_row_id=spec.get("reference_row_id"),
                    draft=params["template"],
                    repeat_group_id=params.get("repeat_group_id"),
                )
            except (KeyError, TypeError, ValueError):
                _log.warning("Skipped an invalid saved extraction template")
                continue
            if project.db.execute(
                "SELECT 1 FROM extraction_layouts WHERE imported_recipe_id=?",
                (recipe_id,),
            ).fetchone():
                continue
            # The original artifact remains available even if its source row
            # has since been deleted. A stale reference does not lose boxes.
            reference_row_id = body.reference_row_id
            if reference_row_id is not None and not project.visible_row_ids(
                sheet_id, [reference_row_id]
            ):
                reference_row_id = None
            try:
                extraction_layouts.save_layout(
                    project,
                    sheet_id=sheet_id,
                    source=source,
                    draft=body.draft.model_dump(mode="json"),
                    reference_row_id=reference_row_id,
                    repeat_group_id=body.repeat_group_id,
                    imported_recipe_id=recipe_id,
                )
            except ValueError:
                # Source/reference state may change during an import. Database
                # failures are deliberately not swallowed by this best effort.
                _log.warning("Skipped an invalid saved extraction template")

    @staticmethod
    def _response(layout, source):
        return ExtractionSavedLayout(
            **{
                key: layout[key]
                for key in (
                    "id",
                    "name",
                    "sheet_id",
                    "source_column_id",
                    "reference_row_id",
                    "draft",
                    "repeat_group_id",
                    "has_applied",
                )
            },
            source=source,
        )

    def templates(self, pid, sheet_id, source):
        project = self.workspace.get(pid)
        column = extraction_layouts.source_column(project, sheet_id, source)
        self._import_saved_templates(project, pid, sheet_id, source)
        return ExtractionTemplatesResponse(
            templates=[
                self._response(layout, source)
                for layout in extraction_layouts.list_layouts(
                    project, sheet_id, column["id"]
                )
            ],
            selected_layout_id=extraction_layouts.selected_layout_id(
                project, sheet_id, column["id"]
            ),
        )

    def save(self, pid, body, *, cancelled=None):
        project = self.workspace.get(pid)
        if body.id is not None:
            existing = extraction_layouts.get_layout(project, body.id)
            if existing is None:
                raise RouteError(404, "Extraction layout not found")
        if cancelled is not None and cancelled():
            raise TableError("action_cancelled", "Layout saving was cancelled")
        if body.reference_row_id is not None and body.draft.reference_blob_id:
            column = extraction_layouts.source_column(
                project, body.sheet_id, body.source
            )
            reference = document_cell(
                project,
                sheet_id=body.sheet_id,
                column_id=column["id"],
                row_id=body.reference_row_id,
            )
            if reference != DocumentCell(
                body.draft.reference_blob_id, body.draft.reference_page
            ):
                raise TableError(
                    "invalid_input_ref",
                    "Reference document no longer matches the selected cell",
                )
        entry = extraction_layouts.save_layout(
            project,
            sheet_id=body.sheet_id,
            source=body.source,
            draft=body.draft.model_dump(mode="json"),
            reference_row_id=body.reference_row_id,
            repeat_group_id=body.repeat_group_id,
            layout_id=body.id,
        )
        return self._response(entry, body.source)

    def select(self, pid, body):
        project = self.workspace.get(pid)
        extraction_layouts.select_layout(
            project,
            sheet_id=body.sheet_id,
            source=body.source,
            layout_id=body.layout_id,
        )
        return self._response(
            extraction_layouts.get_layout(project, body.layout_id), body.source
        )

    def counts(self, pid, body):
        project = self.workspace.get(pid)
        common = {"sheet_id": body.sheet_id, "source": body.source}
        # One snapshot makes the displayed scope alternatives consistent.
        with project.read_snapshot() as snapshot:
            counts = {
                "all": extraction_layouts.count_document_scope(
                    snapshot, **common, scope={"kind": "all"}
                ),
                "filter": extraction_layouts.count_document_scope(
                    snapshot,
                    **common,
                    scope={
                        "kind": "filter",
                        "filter": body.filter,
                        "parent_row_id": body.parent_row_id,
                        "scope_row_ids": body.scope_row_ids,
                    },
                ),
                "layout": extraction_layouts.count_document_scope(
                    snapshot,
                    **common,
                    scope={
                        "kind": "layout",
                        "layout_id": body.layout_id,
                    },
                )
                if body.layout_id is not None
                else 0,
                "this": extraction_layouts.count_document_scope(
                    snapshot,
                    **common,
                    scope={
                        "kind": "this",
                        "row_id": body.row_id,
                    },
                )
                if body.row_id is not None
                else 0,
            }
            return ExtractionScopeCountsResponse(**counts)
