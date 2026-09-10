"""Bounded table samples from the normal producer, without publication."""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from frisket.actions.core import CreateSheet
from frisket.actions.temporal_types import TemporalMediaReader
from frisket.actions.transcript_types import TranscriptReader
from frisket.actions.types import (
    CollectionReader,
    ImportBlobStager,
    StagedFile,
    TableError,
)
from frisket.actions.url_import_types import UrlImporter
from frisket.contracts.action import ActionError
from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
from frisket.engine.executor.table_action import TableSource, builtin_table_source
from frisket.engine.runner.preview import PREVIEW_MAX_ROWS, PreviewColumn
from frisket.engine.store.import_blobs import ImportBlob


@dataclass(frozen=True)
class PreviewFile:
    """An exported scratch handle, not a URL or durable blob identity."""

    id: str


@dataclass
class TablePreviewResult:
    columns: list[PreviewColumn]
    rows: list[dict[str, dict[str, Any]]]
    total: int | None
    warnings: tuple[str, ...] = ()
    artifacts: dict[str, ImportBlob] = field(default_factory=dict, repr=False)
    stager: AdmittedImportBlobStager | None = field(default=None, repr=False)

    def close(self) -> None:
        if self.stager is not None:
            self.stager.close()


def table_preview_refusal(bound) -> ActionError | None:
    terminal = bound.action.definition.run
    if not isinstance(terminal, CreateSheet):
        raise TypeError("table preview requires create_sheet")
    if any(cap in terminal.capabilities for cap in (CollectionReader, UrlImporter)):
        return ActionError(
            code="preview_effect_requires_run",
            message="External acquisition needs a durable run, not a preview.",
            action_kind=bound.action.action_id,
        )
    return None


def preview_table(project, project_id, bound, *, deps, progress, cancelled):
    """Consume at most the host sample cap; never probe an extra expensive row."""
    refusal = table_preview_refusal(bound)
    if refusal is not None:
        raise TableError(refusal.code, refusal.message)
    return preview_table_source(
        builtin_table_source(project, project_id, bound, deps=deps),
        progress=progress,
        cancelled=cancelled,
    )


def preview_table_source(source: TableSource, *, progress, cancelled):
    """The same bounded collector consumes builtin and child-prepared tables."""
    stager = (
        AdmittedImportBlobStager(cancelled=cancelled)
        if any(
            cap in source.capabilities
            for cap in (ImportBlobStager, TemporalMediaReader)
        )
        else None
    )
    artifacts: dict[str, ImportBlob] = {}
    exported: dict[StagedFile, PreviewFile] = {}

    def file_value(handle):
        if handle not in exported:
            descriptor = stager.describe(handle)
            reference = PreviewFile(uuid.uuid4().hex)
            exported[handle] = reference
            artifacts[reference.id] = descriptor
        return exported[handle]

    try:
        with source.prepare(
            check_sheet_name=False,
            blob_stager=stager,
            row_limit=PREVIEW_MAX_ROWS,
            cancelled=cancelled,
        ) as prepared:
            columns = [
                PreviewColumn(
                    name=column.name,
                    column_type=column.type,
                    format=column.format,
                    hidden=column.hidden,
                )
                for column in prepared.table.columns
            ]
            rows = []
            total = None
            iterator = iter(prepared.rows)
            for _ in range(PREVIEW_MAX_ROWS):
                if cancelled():
                    raise TableError("action_cancelled", "Table preview was cancelled.")
                item = next(iterator, None)
                if item is None:
                    total = len(rows)
                    break
                prepared.row_count += 1
                values, _lineage, files = item
                values = dict(values)
                transcripts = prepared.readers.get(TranscriptReader)
                if transcripts is not None:
                    values = transcripts.preview_row(values, len(rows))
                grouped = defaultdict(list)
                for name, handle in files:
                    grouped[name].append(handle)
                for name, handles in grouped.items():
                    values[name] = (
                        [file_value(handle) for handle in handles]
                        if isinstance(values[name], list)
                        else file_value(handles[0])
                    )
                rows.append({name: {"value": value} for name, value in values.items()})
                progress(len(rows), None)
        if cancelled():
            raise TableError("action_cancelled", "Table preview was cancelled.")
        progress(len(rows), total)
        return TablePreviewResult(
            columns,
            rows,
            total,
            warnings=prepared.warnings,
            artifacts=artifacts,
            stager=stager,
        )
    except BaseException:
        if stager is not None:
            stager.close()
        raise
