"""Admitted URL inputs and invocation-owned table staging."""

from __future__ import annotations

import io
from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from pydantic import StrictStr, TypeAdapter

from frisket.actions.types import (
    DynamicOutput,
    DynamicTableResult,
    TableColumn,
    TableRow,
    TableError,
)
from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager

from frisket.ops.url_import import acquire_url_records


class AdmittedUrlImporter:
    """Acquire actual call arguments; only the table host can publish staged files."""

    def __init__(
        self,
        stager: AdmittedImportBlobStager,
        *,
        max_urls: int | None = None,
        enabled_plugin_ids: set[str] | frozenset[str] = frozenset(),
    ):
        self._stager = stager
        self._max_urls = max_urls
        self._admitted_url_count = 0
        self._enabled_plugin_ids = enabled_plugin_ids
        self._facts: list[dict[str, Any]] = []
        self._closed = False

    def close(self) -> None:
        self._closed = True

    @property
    def facts(self) -> list[dict[str, Any]]:
        return deepcopy(self._facts)

    def read(self, urls: Sequence[str]) -> DynamicTableResult:
        if self._closed:
            raise ValueError("URL importer is closed")
        if isinstance(urls, (str, bytes)):
            raise ValueError("URLs must be a sequence of strings")
        actual = [
            url.strip()
            for url in TypeAdapter(list[StrictStr]).validate_python(list(urls))
        ]
        if not actual:
            raise ValueError("URL import requires at least one URL")
        next_count = self._admitted_url_count + len(actual)
        if self._max_urls is not None and next_count > self._max_urls:
            raise TableError(
                "url_limit_exceeded",
                f"URL count exceeds the deployment limit of {self._max_urls}",
            )
        # Every admitted input consumes the invocation budget, including URLs
        # that fail validation or acquisition. Producers may call read repeatedly.
        self._admitted_url_count = next_count
        records, kinds, blobs, refs, sources, errors = acquire_url_records(
            actual, enabled_plugin_ids=self._enabled_plugin_ids
        )
        media_type = kinds[0] if kinds and len(set(kinds)) == 1 else "file"
        columns = [
            TableColumn(key="url", type="text"),
            TableColumn(key="media", type=media_type),
            TableColumn(key="size", type="integer", format="filesize"),
        ]
        if errors:
            columns.append(TableColumn(key="error", type="text"))
        for blob, ref in zip(blobs, refs, strict=True):
            with io.BytesIO(blob["data"]) as stream:
                handle = self._stager.stage_acquired_url(
                    stream,
                    filename=blob["filename"],
                    mime=blob["mime"],
                    source_url=ref["source_url"],
                    provider=ref["provider"],
                    acquisition=blob["metadata"].get("_media_acquisition_v1", {}),
                )
            records[ref["row_index"] - 1]["media"] = handle
        self._facts.extend(
            [
                {
                    "kind": "url_list",
                    "url_count": len(actual),
                    "downloaded": len(blobs),
                    "failed": len(errors),
                },
                *sources,
                *errors,
            ]
        )
        return DynamicTableResult(
            schema=tuple(columns),
            rows=[
                TableRow(
                    output=DynamicOutput(
                        {column.key: record.get(column.key) for column in columns}
                    )
                )
                for record in records
            ],
        )
