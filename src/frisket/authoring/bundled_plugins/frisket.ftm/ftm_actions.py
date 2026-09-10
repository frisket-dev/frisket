"""FtM domain handlers; the host owns admitted reads, publication, and delivery."""

from typing import Any

from pydantic import Field

from frisket.sdk import (
    ActionCategory,
    ActionParams,
    EntityRowset,
    ExportedEntityPackage,
    FollowTheMoneyExporter,
    FollowTheMoneyImporter,
    ImportedEntityDataset,
    action,
)


class FtmImportParams(ActionParams):
    source_path: str = Field(
        min_length=1,
        title="FollowTheMoney file",
        description="Path to the admitted local FtM JSON or JSONL file.",
    )
    dataset_name: str | None = Field(
        default=None,
        min_length=1,
        description="Dataset label retained in the imported technical metadata.",
    )


class FtmExportParams(ActionParams):
    rowsets: list[EntityRowset] = Field(
        min_length=1,
        description="Investigative sheets or materialized sheets to export.",
    )
    mappings: list[dict[str, Any]] = Field(
        min_length=1,
        description="FtM schema, identity policy, and property mappings for each rowset.",
    )
    filename: str = Field(default="entities.ftm.zip", min_length=1)
    validate_entities: bool = Field(default=True, title="Validate entities")


def ftm_import(
    params: FtmImportParams, importer: FollowTheMoneyImporter
) -> ImportedEntityDataset:
    return importer.import_entities(
        source_path=params.source_path, dataset_name=params.dataset_name
    )


def ftm_export(
    params: FtmExportParams, exporter: FollowTheMoneyExporter
) -> ExportedEntityPackage:
    return exporter.export_entities(
        rowsets=params.rowsets,
        mappings=params.mappings,
        filename=params.filename,
        validate=params.validate_entities,
    )


FTM_IMPORT = action(
    name="ftm_import",
    title="Import FollowTheMoney",
    description=(
        "Import entity, relationship, and unsupported-schema sheets atomically, "
        "retaining original entities and dataset metadata. Existing sheet names "
        "are refused; this does not merge or deduplicate existing data."
    ),
    category=ActionCategory.CONVERT,
    form="generated",
    examples=(FtmImportParams(source_path="examples/entities.ftm.jsonl"),),
    run=ftm_import,
)

FTM_EXPORT = action(
    name="ftm_export",
    title="Export FollowTheMoney",
    description=(
        "Export selected investigative rowsets using their FtM mappings to a "
        "downloadable ZIP, including entity JSONL, validation, mappings, and "
        "source-reference evidence."
    ),
    category=ActionCategory.CONVERT,
    form="generated",
    examples=(
        FtmExportParams(
            rowsets=[EntityRowset(sheet_id=1)],
            mappings=[
                {
                    "rowset": {"kind": "sheet", "sheet_id": 1},
                    "schema": "Person",
                    "id_policy": {"kind": "row_ref"},
                    "properties": {"name": {"column": "name"}},
                }
            ],
        ),
    ),
    run=ftm_export,
)
