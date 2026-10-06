"""Annotation-driven document extraction through the ordinary table host."""

from typing import Any, ClassVar, Protocol

from pydantic import Field, model_validator

from frisket.actions.document_extraction_types import (
    Box,
    ExtractionField,
    ExtractionTemplate,
    ExtractionScope,
    PageRegion,
)
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    DynamicOutput,
    TableColumn,
    TableResult,
)


class DocumentColumn(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = ("file", "image")


class DocumentExtractParams(ActionParams):
    source: DocumentColumn
    template: ExtractionTemplate
    repeat_group_id: str | None = None
    layout_id: int | None = Field(default=None, gt=0)
    extraction_scope: ExtractionScope | None = None

    @model_validator(mode="after")
    def selected_group(self):
        if self.extraction_scope is not None:
            selected_layout = self.extraction_scope.layout_id
            if selected_layout is not None and selected_layout != self.layout_id:
                raise ValueError("Scope must use the selected layout")
            if self.extraction_scope.kind == "layout" and self.layout_id is None:
                raise ValueError("Choose a saved layout for its document scope")
        groups = {group.id for group in self.template.sections}
        if self.repeat_group_id is not None and self.repeat_group_id not in groups:
            raise ValueError("Unknown repeated section")
        if groups and self.repeat_group_id is None:
            raise ValueError("Choose the repeated section to extract")
        return self


def extraction_fields(params: DocumentExtractParams):
    return tuple(
        field
        for field in params.template.fields
        if field.section_id is None or field.section_id == params.repeat_group_id
    )


def source_document_column_name(params: DocumentExtractParams) -> str:
    base = "Source document"
    used = {field.name for field in extraction_fields(params)}
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base} {suffix}"
        suffix += 1
    return candidate


def extraction_columns(params: DocumentExtractParams):
    return (
        *(
            TableColumn(key=field.name, type="text")
            for field in extraction_fields(params)
        ),
        TableColumn(key=source_document_column_name(params), type="file"),
    )


class PositionedDocumentReader(Protocol):
    def read(self, params: DocumentExtractParams) -> TableResult[DynamicOutput]: ...


def extract_documents(
    params: DocumentExtractParams, documents: PositionedDocumentReader
) -> TableResult[DynamicOutput]:
    return documents.read(params)


def definition():
    # Imported by the explicit registry after core has loaded capability types.
    from frisket.actions.core import ActionCategory, action, create_sheet

    return action(
        name="extract_document",
        title="Extract in sheet",
        description="Extract fields and repeated records from similarly formatted documents using drawn regions.",
        category=ActionCategory.EXTRACT,
        run=create_sheet(extract_documents, columns_from=extraction_columns),
        examples=(
            DocumentExtractParams(
                source="document",
                template=ExtractionTemplate(
                    reference_blob_id="a" * 64,
                    reference_fingerprint="example-positioned-document",
                    fields=[
                        ExtractionField(
                            id="name",
                            name="Name",
                            key=PageRegion(
                                page=1, box=Box(x0=0.1, y0=0.1, x1=0.25, y1=0.15)
                            ),
                            value=PageRegion(
                                page=1, box=Box(x0=0.3, y0=0.1, x1=0.8, y1=0.15)
                            ),
                        )
                    ],
                ),
            ),
        ),
    )
