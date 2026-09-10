"""Invocation-owned exact media staging for the typed table host."""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from pathlib import Path

from pydantic import TypeAdapter

from frisket.actions.temporal_types import (
    TemporalMediaColumn,
    TemporalMediaValue,
    TranscriptSelection,
    validate_transcript_selection_scope,
)
from frisket.actions.types import (
    DynamicOutput,
    DynamicTableResult,
    RowSource,
    TableColumn,
    TableError,
    TableRow,
)
from frisket.contracts.action import ActionError
from frisket.engine.executor.embedding_read import TableReadRefused
from frisket.engine.executor.temporal_materialization import (
    CoreTemporalMediaMaterializer,
    revalidate_action_sources,
    revalidate_temporal_media_source,
    temporal_inherited_output_keys,
)
from frisket.engine.executor.temporal_split import (
    _all_warnings,
    _receipt_inputs,
    _source_range_for_staged,
    publish_split_clip,
    resolve_temporal_split_plan,
    stage_temporal_split_plan,
    project_split_transcripts,
)
from frisket.engine.executor.temporal_annotations import (
    annotation_intersects_source_range,
    project_resolved_temporal_annotation_items,
)
from frisket.engine.executor.temporal_preview import (
    PreviewTemporalSources,
    PreviewTemporalValue,
)
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.engine.store.cell_writes import create_base_cell_producer
from frisket.engine.store.artifact_timeline import (
    TimelineError,
    EphemeralMediaClock,
    media_timeline_fingerprint,
)
from frisket.engine.store.media_splice import MediaSpliceError


class AdmittedTemporalMediaReader:
    def __init__(
        self,
        project,
        *,
        scope,
        action_kind,
        preview_stager=None,
        row_limit=None,
        cancelled=None,
    ):
        self.project, self.scope, self.action_kind = project, scope, action_kind
        self.parent_sheet_id = scope.sheet_id
        self.sources = set()
        self.facts = []
        self.plans = []
        self.values = {}
        self.occurrences = []
        self.committed = {}
        self.cancelled = cancelled
        self.row_limit = row_limit
        self.preview_stager = preview_stager
        self.preview_sources = (
            PreviewTemporalSources(project, cancelled=cancelled)
            if preview_stager is not None
            else None
        )
        self.preview_values = set()
        self.materializer = CoreTemporalMediaMaterializer(should_cancel=cancelled)
        self.scratch = tempfile.TemporaryDirectory(prefix="frisket-temporal-")
        self.closed = False

    def close(self):
        self.closed = True
        if self.preview_sources is not None:
            self.preview_sources.close()

    def cleanup(self):
        self.close()
        self.scratch.cleanup()

    def read(self, source, selection):
        if self.closed or not isinstance(source, TemporalMediaColumn):
            raise TableError(
                "invalid_input_ref", "expected an active temporal media source"
            )
        selection = TypeAdapter(TranscriptSelection).validate_python(
            selection.model_dump(mode="python")
            if hasattr(selection, "model_dump")
            else selection
        )
        validate_transcript_selection_scope(selection, self.scope)
        # Resolving an imported media cell may establish its immutable source
        # artifact or probe duration. All captured inputs are revalidated in the
        # publication transaction, after staging.
        plan = resolve_temporal_split_plan(
            self.project,
            scope=self.scope,
            source=source,
            selection=selection,
            source_resolver=self.preview_sources.resolve
            if self.preview_sources is not None
            else None,
        )
        if isinstance(plan, ActionError):
            raise TableReadRefused(
                plan.model_copy(update={"action_kind": self.action_kind})
            )
        self.plans.append(plan)
        self.facts.extend(entry.ref for entry in _receipt_inputs(plan))
        schema, transcripts, annotations = _planned_schema(
            plan, cancelled=self.cancelled
        )
        scratch = Path(self.scratch.name) / str(len(self.plans))
        scratch.mkdir()
        try:
            staged = stage_temporal_split_plan(
                self.project,
                plan,
                scratch,
                materializer=self.materializer,
                use_core_batch_renderer=True,
                row_limit=self.row_limit,
                cancelled=self.cancelled,
                materialize=self.preview_sources.materialize
                if self.preview_sources is not None
                else None,
            )
        except TimelineError as exc:
            raise TableError(exc.code, exc.message) from exc
        except MediaSpliceError as exc:
            if exc.code == "cancelled":
                raise TableError(
                    "action_cancelled", "Temporal media rendering was cancelled."
                ) from exc
            code = (
                "stale_input"
                if exc.code == "source_missing"
                else "temporal_materializer_unavailable"
            )
            raise TableError(
                code,
                "The exact temporal media renderer could not produce a valid clip.",
            ) from exc
        if self.row_limit is not None:
            self.row_limit -= len(staged)
        media_type = schema[0].type
        rows = []
        for item in staged:
            if any(
                projection.segments
                and transcript.transcript_column_id not in transcripts
                for transcript, projection in item.transcripts
            ) or any(
                annotation.column_id not in annotations
                and annotation_intersects_source_range(
                    annotation,
                    source_start_ms=item.clip.resolved_start_ms,
                    source_end_ms=item.clip.resolved_end_ms,
                )
                for annotation in item.annotations
            ):
                raise TableError(
                    "cut_alignment_failed",
                    "Resolved clip bounds require columns outside the planned schema.",
                )
            source_token = RowSource(
                sheet_id=item.clip.source.sheet_id, row_id=item.clip.source.row_id
            )
            self.sources.add(source_token)
            with Path(item.clip.payload.path).open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            media = MediaBlobStore.media_cell(
                digest, mime=item.clip.payload.mime, filename=item.clip.payload.filename
            )
            values = {column.key: None for column in schema}
            preview_clock = None
            if self.preview_stager is not None:
                with Path(item.clip.payload.path).open("rb") as stream:
                    media = self.preview_stager.stage(
                        stream,
                        mime=item.clip.payload.mime,
                        filename=item.clip.payload.filename,
                    )
                preview_clock = EphemeralMediaClock(
                    uuid.uuid4().hex,
                    media_timeline_fingerprint(
                        blob_hash=digest, duration_ms=item.clip.payload.duration_ms
                    ),
                    item.clip.payload.duration_ms,
                )

            def admit(role, column_id, value, column_type):
                handle = TemporalMediaValue()
                self.values[handle] = (
                    item,
                    source_token,
                    role,
                    column_id,
                    value,
                    column_type,
                )
                return handle

            values["clip"] = admit("clip", None, media, media_type)
            values["source_range"] = _source_range_for_staged(item)
            if self.preview_stager is not None:
                source_range = values["source_range"]
                values["source_range"] = admit(
                    "source_range",
                    None,
                    self._preview_value(
                        "timeline_range",
                        source_range["timeline"],
                        {"item": source_range["item"]},
                    ),
                    "timeline_range",
                )
            for transcript, projection in item.transcripts:
                if projection.segments:
                    values[transcripts[transcript.transcript_column_id]] = admit(
                        "transcript",
                        transcript.transcript_column_id,
                        projection.text,
                        "timestamped_transcript",
                    )
            for annotation in item.annotations:
                if annotation.column_id in annotations:
                    if preview_clock is not None:
                        projection = project_resolved_temporal_annotation_items(
                            annotation,
                            source_start_ms=item.clip.resolved_start_ms,
                            source_end_ms=item.clip.resolved_end_ms,
                        )
                        if projection is not None:
                            values[annotations[annotation.column_id]] = admit(
                                "annotation",
                                annotation.column_id,
                                self._preview_value(
                                    annotation.type_name,
                                    preview_clock.wire_value(),
                                    projection,
                                ),
                                annotation.type_name,
                            )
                        continue
                    values[annotations[annotation.column_id]] = admit(
                        "annotation", annotation.column_id, None, annotation.type_name
                    )
            rows.append(
                TableRow(
                    output=DynamicOutput(values),
                    sources=(source_token,),
                    parent=source_token,
                )
            )
        return DynamicTableResult(
            schema=schema, rows=tuple(rows), warnings=tuple(_all_warnings(plan, staged))
        )

    def _preview_value(self, type_name, timeline, items):
        value = PreviewTemporalValue(type_name, timeline, items)
        self.preview_values.add(value)
        return value

    def owns_preview_value(self, value, type_name):
        return (
            isinstance(value, PreviewTemporalValue)
            and value in self.preview_values
            and value.type_name == type_name
        )

    def lower_row(self, values, fields, sources, parent, output_names):
        lowered = dict(values)
        bindings = []
        for field in fields:
            value = values[field.key]
            if isinstance(value, PreviewTemporalValue):
                raise ValueError(
                    "temporal preview values require their admitted handles"
                )
            if not isinstance(value, TemporalMediaValue):
                continue
            admitted = self.values.get(value)
            if admitted is None:
                raise ValueError("foreign temporal media value")
            item, source, role, column_id, plain, column_type = admitted
            if sources != (source,) or parent is not source:
                raise ValueError("temporal media requires its exact admitted parent")
            if field.column_type != column_type:
                raise ValueError("temporal media output type changed")
            bindings.append(
                (item, role, column_id, output_names.get(field.key, field.key))
            )
            lowered[field.key] = plain
        clips = [binding for binding in bindings if binding[1] == "clip"]
        if len({(role, column) for _item, role, column, _name in bindings}) != len(
            bindings
        ):
            raise ValueError(
                "one temporal projection cannot define duplicate output roles"
            )
        if bindings and (
            len(clips) != 1
            or any(binding[0] is not clips[0][0] for binding in bindings)
        ):
            raise ValueError(
                "temporal projections require exactly one matching same-row clip"
            )
        self.occurrences.append(tuple(bindings))
        return lowered

    def revalidate(self):
        if self.preview_stager is not None:
            raise ValueError("preview temporal values cannot be published")
        for plan in self.plans:
            revalidate_action_sources(self.project, plan.source_snapshots)
            for source in plan.sources:
                revalidate_temporal_media_source(self.project, source)
        issued_cells = {
            id(item): value
            for item, _source, role, _column, value, _type in self.values.values()
            if role == "clip"
        }
        for bindings in self.occurrences:
            for item, role, _column, _name in bindings:
                if role == "clip" and id(item) not in self.committed:
                    committed = self.materializer.commit_blob(self.project, item.clip)
                    if committed.cell_value != issued_cells[id(item)]:
                        raise TableError(
                            "stale_input", "Staged media changed before publication"
                        )
                    self.committed[id(item)] = committed

    def generated_columns(self):
        # Exact media cuts and inherited values are materializations, not newly
        # generated model output.
        return set()

    def initial_cell_values(self, row, ordinal):
        deferred = {
            name
            for _item, role, _column, name in self.occurrences[ordinal]
            if role == "annotation"
        }
        return {name: value for name, value in row.items() if name not in deferred}

    def publication_evidence(self, write, rows, *, receipt_id):
        evidence = []
        producer_id = create_base_cell_producer(
            self.project.db, stage_id=f"op:{write.op_id}", op_id=write.op_id
        )
        for bindings, row_id, row in zip(
            self.occurrences, write.row_ids, rows, strict=True
        ):
            clip = next((binding for binding in bindings if binding[1] == "clip"), None)
            if clip is None:
                continue
            item, _role, _column, name = clip
            evidence.extend(
                publish_split_clip(
                    self.project,
                    item,
                    self.committed[id(item)],
                    write,
                    row_id,
                    clip_name=name,
                    transcript_names={
                        column: name
                        for _item, role, column, name in bindings
                        if role == "transcript"
                    },
                    annotation_names={
                        column: name
                        for _item, role, column, name in bindings
                        if role == "annotation"
                    },
                    receipt_id=receipt_id,
                    materializer=self.materializer,
                    row=row,
                    producer_id=producer_id,
                )
            )
        return evidence


def _planned_schema(plan, *, cancelled=None):
    """Freeze all selected source roles before any clip is rendered or sampled."""
    selected_transcripts = {}
    selected_annotations = {}
    media_kinds = {source.source.lease.media_kind for source in plan.sources}
    for source in plan.sources:
        for requested in source.source.ranges:
            if cancelled is not None and cancelled():
                raise TableError(
                    "action_cancelled", "Temporal media preparation was cancelled."
                )
            for transcript, projection in project_split_transcripts(
                source, requested, start_ms=requested.start_ms, end_ms=requested.end_ms
            ):
                if projection.segments:
                    selected_transcripts.setdefault(
                        transcript.transcript_column_id, transcript
                    )
            for annotation in source.annotations:
                if annotation_intersects_source_range(
                    annotation,
                    source_start_ms=requested.start_ms,
                    source_end_ms=requested.end_ms,
                ):
                    selected_annotations.setdefault(annotation.column_id, annotation)
    transcripts, annotations = temporal_inherited_output_keys(
        selected_transcripts.values(), selected_annotations.values()
    )
    media_type = next(iter(media_kinds)) if len(media_kinds) == 1 else "file"
    schema = (
        TableColumn("clip", media_type),
        TableColumn("source_range", "timeline_range"),
        *(TableColumn(name, "timestamped_transcript") for name in transcripts.values()),
        *(
            TableColumn(name, selected_annotations[column].type_name)
            for column, name in annotations.items()
        ),
    )
    return schema, transcripts, annotations
