"""Admitted local topic analysis and host-owned, output-bound provenance."""

from __future__ import annotations

import asyncio
import copy
from threading import Event
from typing import Any

from frisket.actions.temporal_types import topic_sidecar_names
from frisket.actions.types import ColumnRef, DynamicOutput, Outcome, Row, RowError
from frisket.contracts.action import ReceiptEvidence
from frisket.engine.executor.temporal_transcripts import resolve_timestamped_transcript
from frisket.engine.executor.visual_cuts_read import _settle
from frisket.engine.store.artifact_timeline import (
    canonical_json_hash,
    resolve_artifact_timeline,
)
from frisket.features.temporal.transcript_boundaries import lock_transcript_boundaries
from frisket.features.temporal_values import (
    TimelineRangesValue,
    canonical_temporal_hash,
)
from frisket.features.topic_segmentation.contracts import (
    BetweenUnits,
    DialogueUnit,
    ExactTime,
    SegmentationCancelled,
    SegmentationContext,
    SegmentationEngineUnavailable,
    SegmentationSnapshot,
    TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION,
    WithinUnit,
)
from frisket.features.topic_segmentation import engines


def _dialogue_snapshot(resolved: Any) -> SegmentationSnapshot:
    units = []
    for ordinal, span in enumerate(resolved.spans):
        selector = span.get("selector")
        speaker = selector.get("speaker") if isinstance(selector, dict) else None
        units.append(
            DialogueUnit(
                id=str(span["stable_id"]),
                ordinal=ordinal,
                text=str(span["quote"]),
                speaker=speaker
                if isinstance(speaker, str) and speaker.strip()
                else None,
                start_ms=int(span["start_ms"]),
                end_ms=int(span["end_ms"]),
            )
        )
    return SegmentationSnapshot(
        snapshot_hash=resolved.snapshot_hash,
        source_kind="timestamped_transcript",
        language=resolved.language,
        units=tuple(units),
    )


def _topic_analysis_receipt(result: Any, locked: Any) -> dict[str, Any]:
    candidates = []
    for candidate in result.boundaries:
        locator = candidate.locator
        if isinstance(locator, BetweenUnits):
            wire = {
                "kind": locator.kind,
                "left_unit_id": locator.left_unit_id,
                "right_unit_id": locator.right_unit_id,
            }
        elif isinstance(locator, WithinUnit):
            wire = {"kind": locator.kind, "unit_id": locator.unit_id}
        elif isinstance(locator, ExactTime):
            wire = {"kind": locator.kind, "at_ms": locator.at_ms}
        else:
            raise ValueError("topic engine returned an unsupported locator")
        candidates.append(
            {
                "id": candidate.id,
                "locator": wire,
                "strength": candidate.strength,
                "label": candidate.label,
                "diagnostics": dict(candidate.diagnostics),
            }
        )
    return {
        "engine_id": result.engine_id,
        "engine_version": result.engine_version,
        "resolved_settings": dict(result.resolved_settings),
        "engine_diagnostics": dict(result.diagnostics),
        "native_candidates": candidates,
        "locking": locked.receipt_metadata(),
    }


class AdmittedTopicSectionsReader:
    def __init__(self, project, *, cancelled=None, engine: str | None = None):
        self._project = project
        self._engine = engine
        self._cancelled = cancelled
        self._closed = False
        self._reads: set[asyncio.Task] = set()

    def _check_open(self):
        if self._closed:
            raise RuntimeError("topic sections reader is closed")
        if self._cancelled is not None and self._cancelled():
            raise asyncio.CancelledError

    def bind_row(self, row, *, sheet_id, row_id, sources):
        self._check_open()
        if (
            type(sheet_id) is not int
            or sheet_id <= 0
            or type(row_id) is not int
            or row_id <= 0
        ):
            raise TypeError("topic sections requires an admitted source row")
        return _BoundTopicSectionsReader(
            self, row, sheet_id, row_id, copy.deepcopy(dict(sources))
        )

    async def aclose(self):
        self._closed = True
        for task in tuple(self._reads):
            await _settle(task)


class _BoundTopicSectionsReader:
    def __init__(self, owner, row, sheet_id, row_id, sources):
        self._owner = owner
        self._row = row
        self._sheet_id = sheet_id
        self._row_id = row_id
        self._sources = sources
        self._results: list[tuple[TimelineRangesValue, dict[str, Any]]] = []

    def _resolve(self, captured):
        resolved = resolve_timestamped_transcript(
            self._owner._project,
            sheet_id=self._sheet_id,
            row_id=self._row_id,
            column_id=captured["column_id"],
        )
        if resolved is None:
            raise RowError(
                "stale_input",
                "This row has no current timestamped-transcript evidence; transcribe it again before finding topic sections.",
            )
        if (
            canonical_json_hash(resolved.transcript_value)
            != canonical_json_hash(captured["value"])
            or resolved.transcript_value_ref != captured["value_ref"]
        ):
            raise RowError(
                "stale_input", "Topic source differs from its admitted cell."
            )
        return resolved

    async def read(
        self, row: Row, source: ColumnRef[Any], *, engine: str, settings: dict[str, Any]
    ) -> TimelineRangesValue:
        self._owner._check_open()
        if self._owner._engine is not None and engine != self._owner._engine:
            raise RowError(
                "invalid_engine", "Topic engine differs from its admitted selection."
            )
        if row is not self._row or not isinstance(source, ColumnRef):
            raise RowError(
                "invalid_input_ref",
                "Topic analysis requires its admitted row and source.",
            )
        captured = self._sources.get(source.name)
        if (
            not isinstance(captured, dict)
            or source.name not in row.values
            or canonical_json_hash(source.read(row))
            != canonical_json_hash(captured["value"])
        ):
            raise RowError(
                "stale_input", "Topic source differs from its admitted cell."
            )
        resolved = self._resolve(captured)
        segmenter = engines.get_segmenter(engine)
        settings = dict(segmenter.validate_settings(settings))
        definition = segmenter.definition
        if not definition.available:
            raise RowError(
                "engine_unavailable",
                definition.error or "The selected topic engine is unavailable.",
            )
        snapshot = _dialogue_snapshot(resolved)
        segmenter.preflight(snapshot, settings).require()
        interrupted = Event()
        task = asyncio.create_task(
            asyncio.to_thread(
                segmenter.segment,
                snapshot,
                settings,
                SegmentationContext(
                    cancelled=lambda: (
                        interrupted.is_set()
                        or self._owner._closed
                        or (
                            self._owner._cancelled is not None
                            and self._owner._cancelled()
                        )
                    )
                ),
            )
        )
        self._owner._reads.add(task)
        try:
            try:
                while not task.done():
                    self._owner._check_open()
                    await asyncio.wait({task}, timeout=0.05)
                result = await asyncio.shield(task)
                self._owner._check_open()
            except BaseException:
                interrupted.set()
                await _settle(task)
                raise
        except SegmentationCancelled as exc:
            raise asyncio.CancelledError from exc
        except SegmentationEngineUnavailable as exc:
            raise RowError("engine_unavailable", str(exc)) from exc
        finally:
            self._owner._reads.discard(task)
        if result.engine_id != definition.id:
            raise RowError(
                "invalid_engine", "Topic engine returned a mismatched engine id."
            )
        if self._resolve(captured).snapshot_hash != resolved.snapshot_hash:
            raise RowError(
                "stale_input", "Transcript evidence changed during topic analysis."
            )
        timeline = resolve_artifact_timeline(self._owner._project, resolved.artifact_id)
        locked = lock_transcript_boundaries(
            snapshot, result.boundaries, timeline=timeline.wire_value()
        )
        value = locked.timeline_ranges
        payload = {
            "schema_version": TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION,
            "transcript_evidence_id": resolved.evidence_link_stable_id,
            "transcript_snapshot_hash": resolved.snapshot_hash,
            "transcript_run_id": resolved.transcript_run_id,
            "transcript_column_id": resolved.transcript_column_id,
            "transcript_value_ref": resolved.transcript_value_ref,
            "transcript_value_hash": canonical_json_hash(resolved.transcript_value),
            "artifact_stable_id": resolved.artifact_stable_id,
            "timeline": timeline.wire_value(),
            "language": resolved.language,
            **_topic_analysis_receipt(result, locked),
        }
        self._results.append((value, copy.deepcopy(payload)))
        return value

    def publication_cells(
        self, output, fields, names, output_columns, *, authored_output=None
    ):
        """Only exact invocation-owned results may acquire analysis evidence."""
        self._owner._check_open()
        authored_output = output if authored_output is None else authored_output
        sidecars = topic_sidecar_names(
            field.key
            for field in fields
            if field.column_type == "timeline_ranges" and not field.hidden
        )
        cells = {}
        for key, sidecar in sidecars.items():
            value = (
                output.root[key]
                if isinstance(output, DynamicOutput)
                else getattr(output, key)
            )
            authored_value = (
                authored_output.root[key]
                if isinstance(authored_output, DynamicOutput)
                else getattr(authored_output, key)
            )
            if isinstance(value, Outcome):
                if value.status == "failed":
                    cells[sidecar] = {
                        "value": None,
                        "error": value.message,
                        "error_code": value.code,
                        "outcome": "model_error",
                    }
                    continue
                value = value.value
            if isinstance(authored_value, Outcome):
                authored_value = authored_value.value
            if value is None:
                cells[sidecar] = {"value": None}
                continue
            payload = next(
                (
                    payload
                    for returned, payload in self._results
                    if returned is authored_value
                ),
                None,
            )
            if (
                payload is None
                or canonical_temporal_hash("timeline_ranges", value)
                != payload["locking"]["timeline_ranges_hash"]
            ):
                raise RowError(
                    "invalid_output",
                    "Topic ranges must retain their admitted analysis result.",
                )
            captured = next(
                cell
                for cell in self._sources.values()
                if cell["column_id"] == payload["transcript_column_id"]
            )
            current = self._resolve(captured)
            if (
                current.snapshot_hash != payload["transcript_snapshot_hash"]
                or current.evidence_link_stable_id != payload["transcript_evidence_id"]
            ):
                raise RowError(
                    "stale_input", "Transcript evidence changed before publication."
                )
            cells[sidecar] = {
                "value": {
                    **copy.deepcopy(payload),
                    # Preview has no columns and never persists this sidecar.
                    "range_column_id": output_columns.get(names[key]),
                }
            }
        return cells


def topic_receipt_evidence(project, facts, output_refs, hidden_refs):
    """Read back committed values: Params and author returns supply no facts."""
    evidence = []
    for hidden in hidden_refs:
        values = project.get_values(
            facts.sheet_id,
            hidden["column_id"],
            row_ids=facts.row_ids,
            apply_edits=False,
        )
        rows = []
        for row_id, payload in values.items():
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version")
                != TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION
            ):
                continue
            output = next(
                (
                    ref
                    for ref in output_refs
                    if ref["column_id"] == payload.get("range_column_id")
                ),
                None,
            )
            if output is None:
                continue
            value = project.get_values(
                facts.sheet_id, output["column_id"], row_ids=[row_id], apply_edits=False
            ).get(row_id)
            if value is None:
                continue
            compact = {
                key: copy.deepcopy(payload[key])
                for key in (
                    "schema_version",
                    "transcript_evidence_id",
                    "transcript_snapshot_hash",
                    "transcript_run_id",
                    "transcript_column_id",
                    "transcript_value_ref",
                    "transcript_value_hash",
                    "artifact_stable_id",
                    "timeline",
                    "language",
                    "engine_id",
                    "engine_version",
                    "resolved_settings",
                    "engine_diagnostics",
                    "native_candidates",
                    "locking",
                )
                if key in payload
            }
            compact["locking"] = {
                key: value
                for key, value in compact.get("locking", {}).items()
                if key != "timeline_ranges_hash"
            }
            rows.append(
                {
                    "row_id": row_id,
                    **compact,
                    "timeline_ranges_hash": canonical_temporal_hash(
                        "timeline_ranges", value
                    ),
                }
            )
        evidence.append(
            ReceiptEvidence(
                ref={
                    "kind": "topic_segmentation_analysis",
                    "run_id": facts.run_id,
                    "sidecar_column_id": hidden["column_id"],
                    "rows": rows,
                }
            )
        )
    return evidence
