"""Source resolution and immutable snapshots for ``map.find``."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from frisket.ai.map.find_source import AddressedUnit
from frisket.ai.vision.region_locator import ImageAsset, prepare_image_asset
from frisket.contracts.action import ActionError
from frisket.engine.executor.find_plan import FindOperation
from frisket.actions.find_types import FindSourceColumn


_MAX_UNIT_CHARS = 1_200


@dataclass(frozen=True)
class ResolvedFindSource:
    sheet_id: int
    row_id: int
    column_id: int
    column_type: str
    source_snapshot: str
    value_ref: dict[str, Any]
    artifact_id: int | None
    units: tuple[AddressedUnit, ...]
    source_kind: str = "text"
    image: ImageAsset | None = None
    blob_hash: str | None = None
    filename: str | None = None
    media_type: str | None = None


def _snapshot_payload(sources: tuple[ResolvedFindSource, ...]) -> list[dict[str, Any]]:
    return [
        {
            "sheet_id": source.sheet_id,
            "row_id": source.row_id,
            "column_id": source.column_id,
            "source_snapshot": source.source_snapshot,
        }
        for source in sources
    ]


def _hash_json(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _find_target_snapshot(
    project: Any, params: FindOperation
) -> dict[str, Any] | ActionError:
    row = project.db.execute(
        "SELECT id, parent_sheet_id, parent_op_id FROM sheets "
        "WHERE name=? AND hidden=0",
        (params.target_sheet_name,),
    ).fetchone()
    if row is None:
        return {"kind": "absent", "name": params.target_sheet_name}
    sheet_id = int(row["id"])
    parent_sheet_id = row["parent_sheet_id"]
    parent_op_id = row["parent_op_id"]
    op = (
        project.db.execute(
            "SELECT kind FROM ops WHERE id=?", (parent_op_id,)
        ).fetchone()
        if isinstance(parent_op_id, int)
        else None
    )
    if (
        sheet_id == params.sheet_id
        or parent_sheet_id != params.sheet_id
        or op is None
        or str(op["kind"]) != params.action_kind
    ):
        return ActionError(
            code="duplicate_sheet_name",
            message=(
                "The findings target name belongs to a sheet not managed by "
                "map.find for this source sheet"
            ),
            action_kind=params.action_kind,
            field="sheet_name",
            details={"sheet_id": sheet_id, "name": params.target_sheet_name},
        )
    return {
        "kind": "managed",
        "name": params.target_sheet_name,
        "sheet_id": sheet_id,
        "parent_sheet_id": int(parent_sheet_id),
        "parent_op_id": int(parent_op_id),
    }


def _chunk_text_units(row_id: int, text: str) -> tuple[AddressedUnit, ...]:
    """Split plain text without losing deterministic character coordinates."""

    units: list[AddressedUnit] = []
    cursor = 0
    while cursor < len(text):
        limit = min(len(text), cursor + _MAX_UNIT_CHARS)
        end = limit
        if limit < len(text):
            candidates = [
                text.rfind("\n\n", cursor, limit),
                text.rfind("\n", cursor, limit),
                text.rfind(". ", cursor, limit),
                text.rfind(" ", cursor, limit),
            ]
            boundary = max(candidates)
            if boundary > cursor + (_MAX_UNIT_CHARS // 2):
                end = boundary + (2 if text[boundary : boundary + 2] == ". " else 1)
        chunk = text[cursor:end]
        if chunk.strip():
            index = len(units)
            units.append(
                AddressedUnit(
                    unit_id=f"r{row_id}:u{index}",
                    text=chunk,
                    # Negative virtual ids encode exact character coordinates;
                    # write-time evidence materializes them onto the source cell.
                    span_ids=(-(cursor + 1), -(end + 1)),
                )
            )
        cursor = max(end, cursor + 1)
    return tuple(units)


def _ocr_units(resolved: Any) -> tuple[AddressedUnit, ...]:
    return tuple(
        AddressedUnit(
            unit_id=str(span["stable_id"]),
            text=str(span["quote"]),
            span_ids=(int(span["id"]),),
        )
        for span in resolved.spans
    )


def resolve_find_sources(
    project: Any,
    params: FindOperation,
) -> tuple[ResolvedFindSource, ...] | ActionError:
    columns = {
        str(column["name"]): column for column in project.columns(params.sheet_id)
    }
    column = columns.get(params.source_column)
    if column is None:
        return ActionError(
            code="invalid_input_ref",
            message="map.find source column does not exist",
            action_kind=params.action_kind,
            field="params.source",
        )
    column_id = int(column["id"])
    column_type = str(column["type"])
    accepted_types = FindSourceColumn.accepted_column_types
    if accepted_types is not None and column_type not in accepted_types:
        return ActionError(
            code="invalid_input_ref",
            message="map.find source column type is not accepted",
            action_kind=params.action_kind,
            field="params.source",
            details={
                "source_column": params.source_column,
                "type": column_type,
                "accepted_column_types": list(accepted_types),
            },
        )
    row_ids = project.visible_row_ids(params.sheet_id, params.row_ids)
    if params.row_ids is not None and set(row_ids) != set(params.row_ids):
        missing = sorted(set(params.row_ids) - set(row_ids))
        return ActionError(
            code="invalid_input_ref",
            message="map.find row_ids must belong to the target sheet",
            action_kind=params.action_kind,
            field="scope.row_ids",
            details={"missing": missing},
        )
    values, refs = project.get_values_with_refs(
        params.sheet_id, column_id, row_ids=row_ids
    )
    sources: list[ResolvedFindSource] = []
    for row_id in row_ids:
        value = values.get(row_id)
        value_ref = dict(refs.get(row_id) or {})
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        artifact_id: int | None = None
        source_kind = "text"
        image: ImageAsset | None = None
        blob_hash: str | None = None
        filename: str | None = None
        media_type: str | None = None
        if column_type in {"audio", "video"}:
            from frisket.engine.executor.temporal_materialization import (
                resolve_source_timeline,
            )
            from frisket.engine.executor.temporal_transcripts import (
                resolve_compatible_transcripts_for_anchor,
            )
            from frisket.engine.store.artifact_timeline import TimelineError

            try:
                lease = resolve_source_timeline(
                    project,
                    sheet_id=params.sheet_id,
                    row_id=row_id,
                    column_id=column_id,
                )
            except (KeyError, TimelineError, TypeError, ValueError):
                return ActionError(
                    code="source_not_addressable",
                    message="The selected audio/video source has no stable timeline",
                    action_kind=params.action_kind,
                    field="params.source",
                    details={"sheet_id": params.sheet_id, "row_id": row_id},
                )
            transcripts, _warnings = resolve_compatible_transcripts_for_anchor(
                project,
                sheet_id=params.sheet_id,
                row_id=row_id,
                source_anchor=lease.anchor,
            )
            transcripts = tuple(
                transcript
                for transcript in transcripts
                if transcript.artifact_id == lease.anchor.artifact_id
                and transcript.source_to_transcript_offset_ms == 0
            )
            if not transcripts:
                return ActionError(
                    code="source_needs_transcript",
                    message=(
                        "The selected audio/video source needs a current timestamped "
                        "transcript. Run Transcribe, then retry Find."
                    ),
                    action_kind=params.action_kind,
                    field="params.source",
                    details={
                        "sheet_id": params.sheet_id,
                        "row_ids": [row_id],
                        "source_column": params.source_column,
                        "remediation_action_kind": "media.transcribe",
                    },
                )
            if len(transcripts) != 1:
                return ActionError(
                    code="source_transcript_ambiguous",
                    message=(
                        "Several current transcripts match the selected media. "
                        "Choose the exact timestamped transcript column to scan."
                    ),
                    action_kind=params.action_kind,
                    field="params.source",
                    details={
                        "sheet_id": params.sheet_id,
                        "row_id": row_id,
                        "transcript_columns": [
                            transcript.transcript_column_name
                            for transcript in transcripts
                        ],
                    },
                )
            transcript = transcripts[0]
            transcript_start_ms = transcript.source_to_transcript_offset_ms
            transcript_end_ms = transcript_start_ms + lease.anchor.duration_ms
            units = tuple(
                AddressedUnit(
                    unit_id=str(span["stable_id"]),
                    text=str(span["quote"]),
                    span_ids=(int(span["id"]),),
                )
                for span in transcript.spans
                if int(span["start_ms"]) < transcript_end_ms
                and int(span["end_ms"]) > transcript_start_ms
            )
            artifact_id = transcript.artifact_id
            snapshot = _hash_json(
                {
                    "media_value_ref": value_ref,
                    "media_source_hash": lease.source_value_hash,
                    "media_artifact": lease.anchor.artifact_stable_id,
                    "transcript_snapshot": transcript.snapshot_hash,
                    "source_to_transcript_offset_ms": (
                        transcript.source_to_transcript_offset_ms
                    ),
                }
            )
        elif column_type == "timestamped_transcript":
            from frisket.engine.executor.temporal_transcripts import (
                resolve_timestamped_transcript,
            )

            transcript = resolve_timestamped_transcript(
                project,
                sheet_id=params.sheet_id,
                row_id=row_id,
                column_id=column_id,
            )
            if transcript is None:
                return ActionError(
                    code="source_needs_transcript",
                    message=(
                        "The selected audio/video source needs a current timestamped "
                        "transcript. Run Transcribe, then retry Find."
                    ),
                    action_kind=params.action_kind,
                    field="params.source",
                    details={
                        "sheet_id": params.sheet_id,
                        "row_ids": [row_id],
                        "source_column": params.source_column,
                        "remediation_action_kind": "media.transcribe",
                    },
                )
            units = tuple(
                AddressedUnit(
                    unit_id=str(span["stable_id"]),
                    text=str(span["quote"]),
                    span_ids=(int(span["id"]),),
                )
                for span in transcript.spans
            )
            artifact_id = transcript.artifact_id
            snapshot = transcript.snapshot_hash
        elif column_type == "text" and isinstance(value, str) and value.strip():
            from frisket.engine.store.ocr_word_stream import (
                resolve_current_ocr_evidence,
            )

            ocr_sources = resolve_current_ocr_evidence(
                project,
                sheet_id=params.sheet_id,
                row_id=row_id,
                column_id=column_id,
            )
            if len(ocr_sources) > 1:
                return ActionError(
                    code="source_ocr_ambiguous",
                    message="Several current OCR evidence links ground this text cell",
                    action_kind=params.action_kind,
                    field="params.source",
                    details={"sheet_id": params.sheet_id, "row_id": row_id},
                )
            if ocr_sources:
                ocr_source = ocr_sources[0]
                units = _ocr_units(ocr_source)
                artifact_id = ocr_source.artifact_id
                snapshot = _hash_json(
                    {
                        "value_ref": ocr_source.value_ref,
                        "evidence_link_id": ocr_source.evidence_link_id,
                        "unit_ids": [unit.unit_id for unit in units],
                    }
                )
            else:
                units = _chunk_text_units(row_id, value)
                snapshot = _hash_json(
                    {
                        "value": value,
                        "value_ref": value_ref,
                        "column_type": column_type,
                    }
                )
        elif (
            column_type in {"image", "file"}
            and isinstance(value, dict)
            and value.get("blob")
        ):
            blob_hash = str(value["blob"])
            blob = project.db.execute(
                "SELECT filename, mime FROM blobs WHERE hash=?", (blob_hash,)
            ).fetchone()
            media_type = str(value.get("mime") or (blob["mime"] if blob else "") or "")
            filename = (
                str(value.get("filename") or (blob["filename"] if blob else "") or "")
                or None
            )
            if column_type == "image" or media_type.startswith("image/"):
                try:
                    image = prepare_image_asset(
                        project.read_blob(blob_hash),
                        source_media_type=media_type or "image/unknown",
                    )
                except Exception as exc:
                    return ActionError(
                        code="source_not_addressable",
                        message="The selected image could not be decoded for Find",
                        action_kind=params.action_kind,
                        field="params.source",
                        details={
                            "sheet_id": params.sheet_id,
                            "row_id": row_id,
                            "error": str(exc)[:500],
                        },
                    )
                source_kind = "image"
                units = ()
                snapshot = _hash_json(
                    {
                        "value_ref": value_ref,
                        "blob": blob_hash,
                        "media_type": media_type,
                        "inference_hash": image.sha256,
                        "inference_width": image.width,
                        "inference_height": image.height,
                        "display_width": image.display_width,
                        "display_height": image.display_height,
                    }
                )
            else:
                from frisket.engine.store.ocr_word_stream import (
                    resolve_current_ocr_evidence,
                )

                ocr_sources = resolve_current_ocr_evidence(
                    project,
                    sheet_id=params.sheet_id,
                    row_id=row_id,
                    blob_hash=blob_hash,
                )
                if not ocr_sources:
                    return ActionError(
                        code="source_not_addressable",
                        message=(
                            "The selected file has no addressable OCR text. "
                            "Run OCR, then retry Find."
                        ),
                        action_kind=params.action_kind,
                        field="params.source",
                        details={
                            "sheet_id": params.sheet_id,
                            "row_ids": [row_id],
                            "source_column": params.source_column,
                            "remediation_action_kind": "media.ocr",
                        },
                    )
                if len(ocr_sources) > 1:
                    return ActionError(
                        code="source_ocr_ambiguous",
                        message=(
                            "Several current OCR outputs match the selected file. "
                            "Choose the exact OCR text column to scan."
                        ),
                        action_kind=params.action_kind,
                        field="params.source",
                        details={
                            "sheet_id": params.sheet_id,
                            "row_id": row_id,
                            "ocr_columns": [
                                source.column_name for source in ocr_sources
                            ],
                        },
                    )
                ocr_source = ocr_sources[0]
                artifact_id = ocr_source.artifact_id
                units = _ocr_units(ocr_source)
                snapshot = _hash_json(
                    {
                        "blob": blob_hash,
                        "evidence_link_id": ocr_source.evidence_link_id,
                        "ocr_value_ref": ocr_source.value_ref,
                        "unit_ids": [unit.unit_id for unit in units],
                    }
                )
        else:
            return ActionError(
                code="source_not_addressable",
                message="The selected source has no addressable text representation",
                action_kind=params.action_kind,
                field="params.source",
                details={"sheet_id": params.sheet_id, "row_id": row_id},
            )
        if units or source_kind == "image":
            sources.append(
                ResolvedFindSource(
                    sheet_id=params.sheet_id,
                    row_id=row_id,
                    column_id=column_id,
                    column_type=column_type,
                    source_snapshot=snapshot,
                    value_ref=value_ref,
                    artifact_id=artifact_id,
                    units=units,
                    source_kind=source_kind,
                    image=image,
                    blob_hash=blob_hash,
                    filename=filename,
                    media_type=media_type,
                )
            )
    if not sources:
        return ActionError(
            code="source_not_addressable",
            message="The selected row scope contains no addressable source",
            action_kind=params.action_kind,
            field="scope.row_ids",
        )
    return tuple(sources)
