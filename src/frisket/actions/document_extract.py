"""Annotation-driven document extraction through the ordinary table host."""

from typing import Any, ClassVar, Protocol

from pydantic import model_validator

from frisket.actions.document_extraction_types import ExtractionTemplate
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

    @model_validator(mode="after")
    def selected_group(self):
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


def extraction_columns(params: DocumentExtractParams):
    return tuple(
        TableColumn(key=field.name, type="text") for field in extraction_fields(params)
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
    )
