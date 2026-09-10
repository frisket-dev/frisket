"""Exact row-bound visual analysis and cancellation-safe borrowed blob lifetimes."""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from frisket.actions.types import ColumnRef, Row, RowError
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import (
    TimelineError,
    canonical_json_hash,
    ensure_media_timeline,
    media_timeline_fingerprint,
    read_timeline_source,
)
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.execution.provider import ExecutionLimits, enforce_media_duration_limit
from frisket.features.temporal.visual_cuts import visual_cuts_value
from frisket.features.temporal_values import TimelinePointsValue


async def _settle(task: asyncio.Future[Any]) -> None:
    """Keep the borrowed path alive through repeated cancellation and failure."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    if not task.cancelled():
        task.exception()


class AdmittedVisualCutsReader:
    def __init__(
        self,
        project: Project,
        *,
        preview: bool = False,
        cancelled: Callable[[], bool] | None = None,
        execution_limits: ExecutionLimits | None = None,
    ) -> None:
        self._project = project
        self._preview = preview
        self._cancelled = cancelled
        self._limits = execution_limits
        self._closed = False
        self._reads: set[asyncio.Task[TimelinePointsValue]] = set()
        self._preview_anchors: dict[tuple[int, int, int, str, int], str] = {}

    @property
    def closed(self) -> bool:
        return self._closed

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("visual cuts reader is closed")
        if self._cancelled is not None and self._cancelled():
            raise asyncio.CancelledError

    def bind_row(
        self,
        row: Row,
        *,
        sheet_id: int,
        row_id: int,
        sources: Mapping[str, Mapping[str, Any]],
    ) -> _BoundVisualCutsReader:
        self._check_open()
        if (
            type(sheet_id) is not int
            or sheet_id <= 0
            or type(row_id) is not int
            or row_id <= 0
        ):
            raise TypeError("visual cuts requires an admitted source row")
        return _BoundVisualCutsReader(
            self, row, sheet_id, row_id, copy.deepcopy(dict(sources))
        )

    async def aclose(self) -> None:
        self._closed = True
        # A read owns its materialization context. Settle the whole read, not
        # only its detector, before returning control to invocation teardown.
        for task in tuple(self._reads):
            await _settle(task)

    async def _read(
        self, *, sheet_id: int, row_id: int, captured: Mapping[str, Any]
    ) -> TimelinePointsValue:
        self._check_open()
        source = read_timeline_source(
            self._project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=captured["column_id"],
            value=captured["value"],
            current_value_ref=captured["value_ref"],
        )
        column = self._project.db.execute(
            "SELECT type FROM columns WHERE id=?", (captured["column_id"],)
        ).fetchone()
        if column is None or column["type"] != "video" or source.media_kind != "video":
            raise TimelineError("timeline_not_found", "source column is not video")
        maximum = self._limits.max_media_seconds if self._limits is not None else None
        if maximum is not None:
            probe = MediaBlobStore(self._project).probe_metadata(source.blob_hash)
            enforce_media_duration_limit(maximum, probe.get("duration_seconds"))
        self._check_open()
        anchor = source.anchor
        if not self._preview:
            anchor = ensure_media_timeline(
                self._project,
                blob_hash=source.blob_hash,
                duration_ms=source.duration_ms,
                media_type=source.media_type,
                filename=source.filename,
                source_sheet_id=sheet_id,
                source_row_id=row_id,
                source_column_id=captured["column_id"],
            )
        if anchor is None:
            identity = (
                sheet_id,
                row_id,
                captured["column_id"],
                source.blob_hash,
                source.duration_ms,
            )
            stable_id = self._preview_anchors.setdefault(
                identity, f"source_artifact:preview-{uuid.uuid4().hex}"
            )
            wire_anchor = {
                "artifact_stable_id": stable_id,
                "fingerprint": media_timeline_fingerprint(
                    blob_hash=source.blob_hash, duration_ms=source.duration_ms
                ),
                "duration_ms": source.duration_ms,
            }
        else:
            wire_anchor = anchor.wire_value()
        with self._project.materialize_blob(source.blob_hash) as path:
            detector = asyncio.create_task(
                asyncio.to_thread(visual_cuts_value, path, timeline=wire_anchor)
            )
            try:
                while not detector.done():
                    self._check_open()
                    await asyncio.wait({detector}, timeout=0.05)
                value = await asyncio.shield(detector)
                self._check_open()
            except BaseException:
                await _settle(detector)
                raise
        return TimelinePointsValue.model_validate(value)


class _BoundVisualCutsReader:
    def __init__(
        self,
        owner: AdmittedVisualCutsReader,
        row: Row,
        sheet_id: int,
        row_id: int,
        sources: dict[str, Any],
    ) -> None:
        self._owner = owner
        self._row = row
        self._sheet_id = sheet_id
        self._row_id = row_id
        self._sources = sources

    async def read(self, row: Row, source: ColumnRef[Any]) -> TimelinePointsValue:
        self._owner._check_open()
        if row is not self._row or not isinstance(source, ColumnRef):
            raise RowError(
                "invalid_input_ref", "Visual cuts requires the admitted row and source."
            )
        captured = self._sources.get(source.name)
        if (
            not isinstance(captured, dict)
            or source.name not in row.values
            or canonical_json_hash(source.read(row))
            != canonical_json_hash(captured["value"])
        ):
            raise RowError(
                "stale_input", "Visual cuts source differs from its admitted cell."
            )
        task = asyncio.create_task(
            self._owner._read(
                sheet_id=self._sheet_id, row_id=self._row_id, captured=captured
            )
        )
        self._owner._reads.add(task)
        try:
            try:
                return await asyncio.shield(task)
            except BaseException:
                await _settle(task)
                raise
        except TimelineError as error:
            raise RowError(error.code, error.message) from error
        finally:
            self._owner._reads.discard(task)
