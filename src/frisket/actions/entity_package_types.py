"""FollowTheMoney dataset and package capabilities, independent of transport."""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class EntityRowset(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")
    kind: Literal["sheet", "materialized_sheet"] = "sheet"
    sheet_id: int = Field(strict=True, gt=0)


class ImportedEntitySheet(BaseModel):
    name: str
    schema_name: str
    kind: Literal["entity", "relationship", "unsupported"]
    sheet_id: int
    column_ids: dict[str, int]
    row_ids: tuple[int, ...]


class ImportedEntityDataset(BaseModel):
    dataset_name: str | None
    sheets: tuple[ImportedEntitySheet, ...]
    op_id: int
    diagnostics: tuple[dict[str, Any], ...]


class ExportedEntityPackage(BaseModel):
    kind: Literal["export_project_file"] = "export_project_file"
    format: Literal["zip"] = "zip"
    content_type: Literal["application/zip"] = "application/zip"
    export_kind: Literal["followthemoney"] = "followthemoney"
    filename: str
    blob_hash: str
    byte_count: int
    sha256: str
    entity_count: int
    skipped_count: int


class FollowTheMoneyImporter(Protocol):
    def import_entities(
        self, *, source_path: str, dataset_name: str | None = None
    ) -> ImportedEntityDataset: ...


class FollowTheMoneyExporter(Protocol):
    def export_entities(
        self,
        *,
        rowsets: list[EntityRowset],
        mappings: list[dict[str, Any]],
        filename: str = "entities.ftm.zip",
        validate: bool = True,
    ) -> ExportedEntityPackage: ...
