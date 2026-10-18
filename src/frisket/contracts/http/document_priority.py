"""HTTP contract for prioritizing explicitly opened documents."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from frisket.contracts.http.models import WireModel


class DocumentPriorityRequest(WireModel):
    sheet_id: int = Field(gt=0)
    row_id: int = Field(gt=0)
    column_id: int = Field(gt=0)


class DocumentPriorityResponse(WireModel):
    schema_version: Literal["frisket.document_priority.v1"] = Field(
        default="frisket.document_priority.v1", alias="schemaVersion"
    )
    promoted: bool
    run_id: int | None = Field(gt=0)


__all__ = ["DocumentPriorityRequest", "DocumentPriorityResponse"]
