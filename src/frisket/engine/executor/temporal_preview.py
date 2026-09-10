"""Scratch-only temporal clocks and read-only media admission for table preview."""

from __future__ import annotations

import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.actions.types import TableError
from frisket.engine.store.artifact_timeline import (
    EphemeralMediaClock,
    TimelineError,
    TimelineLease,
    media_timeline_fingerprint,
    read_timeline_source,
)
from frisket.engine.store.media_splice import MediaSpliceError, probe_staged_media
from frisket.engine.sandbox.media_sync import run_media_sync


@dataclass(frozen=True, eq=False)
class PreviewTemporalValue:
    """Display-only items; this is deliberately not a durable temporal value."""

    type_name: str
    timeline: dict[str, Any]
    items: dict[str, Any]

    def wire_value(self) -> dict[str, Any]:
        return {
            "schema_version": "frisket.preview_temporal.v1",
            "column_type": self.type_name,
            "timeline": self.timeline,
            **self.items,
        }


class PreviewTemporalSources:
    def __init__(self, project, *, cancelled):
        self.project = project
        self.cancelled = cancelled
        self.resources = ExitStack()
        self.paths: dict[str, Path] = {}

    def close(self):
        self.resources.close()

    def check_cancelled(self):
        if self.cancelled is not None and self.cancelled():
            raise TableError("action_cancelled", "Temporal preview was cancelled.")

    @contextmanager
    def materialize(self, blob):
        self.check_cancelled()
        if blob not in self.paths:
            self.paths[blob] = Path(
                self.resources.enter_context(self.project.materialize_blob(blob))
            )
        yield self.paths[blob]

    def resolve(
        self, project, *, sheet_id, row_id, column_id, value, current_value_ref
    ):
        self.check_cancelled()
        arguments = dict(
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            value=value,
            current_value_ref=current_value_ref,
        )
        try:
            source = read_timeline_source(project, **arguments)
        except TimelineError as exc:
            if exc.code != "timeline_duration_required":
                raise
            with self.materialize(value["blob"]) as path:
                try:
                    probe = run_media_sync(
                        lambda should_cancel: probe_staged_media(
                            path, should_cancel=should_cancel
                        ),
                        cancelled=self.cancelled,
                    )
                except MediaSpliceError as probe_error:
                    if probe_error.code == "cancelled":
                        raise TableError(
                            "action_cancelled", "Temporal preview was cancelled."
                        ) from probe_error
                    raise TimelineError(
                        "timeline_duration_required",
                        "Source duration could not be probed.",
                    ) from probe_error
            source = read_timeline_source(
                project, **arguments, duration_ms=probe.duration_ms
            )
        self.check_cancelled()
        clock = source.anchor or EphemeralMediaClock(
            uuid.uuid4().hex,
            media_timeline_fingerprint(
                blob_hash=source.blob_hash, duration_ms=source.duration_ms
            ),
            source.duration_ms,
        )
        return TimelineLease(
            anchor=clock,
            blob_hash=source.blob_hash,
            media_type=source.media_type,
            media_kind=source.media_kind,
            filename=source.filename,
            source_cell_ref=source.source_cell_ref,
            current_value_ref=source.current_value_ref,
            source_value_hash=source.source_value_hash,
        )
