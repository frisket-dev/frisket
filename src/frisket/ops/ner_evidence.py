"""NER captured-input evidence, committed by the host with row results."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Mapping

from frisket.ops.ner_text import ner_text

if TYPE_CHECKING:
    from frisket.actions.ner import NerParams


_NER_CAPTURE_PREFIX = "__frisket_ner_capture_v1__:"
_NER_CAPTURE_SCHEMA = "frisket.ner_input_capture.v1"
_NER_EVIDENCE_KEY = "_frisket_ner_evidence"
_SQLITE_ID_CHUNK_SIZE = 900


def typed_ner_evidence_spec(
    params: NerParams, output_names: Mapping[str, str]
) -> dict[str, Any]:
    """Project only the domain facts consumed by the evidence writer."""
    from frisket.actions.types import Template

    return {
        "engine": params.engine.root,
        "output_name": output_names["entities"],
        "input_template": params.source.text
        if isinstance(params.source, Template)
        else None,
    }


def capture_result_evidence(
    row_values: dict[str, Any],
    input_capture: dict[str, dict[str, Any]],
    results: dict[str, dict[str, Any]],
    spec: dict[str, Any],
    *,
    run_id: int,
    op_id: int,
    row_id: int,
    output_columns: dict[str, int],
) -> None:
    """Finalize NER values and retain private evidence input until commit.

    Inline coordinates are available only when execution indexed one exact
    string cell. The private envelope carries value lineage and validated
    positions, not a second copy of the input text. The result sink ignores
    it while the row-effect checkpoint preserves it for crash replay.
    """

    out_name = str(spec.get("output_name") or "entities")
    result = results.get(out_name)
    output_column_id = output_columns.get(out_name)
    if not isinstance(result, dict) or output_column_id is None:
        return
    result_ref = {
        "run_id": run_id,
        "op_id": op_id,
        "row_id": row_id,
        "column_id": int(output_column_id),
    }
    existing = result.get(_NER_EVIDENCE_KEY)
    if existing is not None:
        if (
            _validated_ner_capture(
                existing,
                result.get("value"),
                expected_result_ref=result_ref,
            )
            is None
        ):
            raise ValueError("invalid NER evidence capture envelope")
        return
    legacy_capture = _parse_ner_capture(
        result.get("justification"), result.get("value")
    )
    if legacy_capture is not None:
        legacy_capture = {**legacy_capture, "result_ref": result_ref}
    justification = result.get("justification")
    if isinstance(justification, str) and justification.startswith(_NER_CAPTURE_PREFIX):
        result.pop("justification", None)
    entities = result.get("value")
    if not isinstance(entities, list):
        return
    capture = legacy_capture or {
        "schema_version": _NER_CAPTURE_SCHEMA,
        "result_ref": result_ref,
        "result_hash": _value_hash(entities),
    }
    if spec.get("input_template") or len(input_capture) != 1:
        result[_NER_EVIDENCE_KEY] = capture
        return
    captured = next(iter(input_capture.values()))
    source_text = captured.get("value")
    if not isinstance(source_text, str) or ner_text(row_values) != source_text:
        result[_NER_EVIDENCE_KEY] = capture
        return

    positions: list[list[list[int]]] = []
    is_llm = spec.get("engine") == "llm"
    if is_llm:
        expanded: list[dict[str, Any]] = []
        expanded_positions: list[list[list[int]]] = []
        seen_mentions: set[tuple[str, Any, int, int]] = set()
        for entity in entities:
            if not isinstance(entity, dict) or not isinstance(entity.get("text"), str):
                continue
            quote = entity["text"]
            matches: list[list[int]] = []
            search_from = 0
            while quote:
                start = source_text.find(quote, search_from)
                if start < 0:
                    break
                matches.append([start, start + len(quote)])
                search_from = start + len(quote)
            if matches:
                for start, end in matches:
                    mention = {**entity, "start": start, "end": end}
                    key = (quote, mention.get("type"), start, end)
                    if key in seen_mentions:
                        continue
                    seen_mentions.add(key)
                    expanded.append(mention)
                    expanded_positions.append([[start, end]])
            else:
                start = entity.get("start")
                end = entity.get("end")
                if type(start) is not int or type(end) is not int:
                    continue
                key = (quote, entity.get("type"), start, end)
                if key in seen_mentions:
                    continue
                seen_mentions.add(key)
                expanded.append(entity)
                expanded_positions.append([])
        entities = expanded
        positions = expanded_positions
        results[out_name]["value"] = entities

    if not is_llm:
        for entity in entities:
            if not isinstance(entity, dict) or not isinstance(entity.get("text"), str):
                positions.append([])
                continue
            quote = entity["text"]
            start = entity.get("start")
            end = entity.get("end")
            positions.append(
                [[start, end]]
                if type(start) is int
                and type(end) is int
                and 0 <= start < end <= len(source_text)
                and source_text[start:end] == quote
                else []
            )

    result[_NER_EVIDENCE_KEY] = (
        capture
        if legacy_capture is not None
        else {
            **capture,
            "result_hash": _value_hash(entities),
            "surface": {
                "column_id": int(captured["column_id"]),
                "value_ref": captured.get("value_ref"),
                "content_hash": _text_hash(source_text),
                "offset_unit": "unicode_codepoint",
            },
            "positions": positions,
        }
    )


def _text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _value_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return _text_hash(encoded)


def _write_entity_spans(
    project: Any,
    runner_spec: dict[str, Any],
    *,
    sheet_id: int,
    output_column_id: int,
    run_id: int,
    op_id: int,
    receipt_id: str,
    input_column_ids: dict[str, int],
    row_ids: list[int],
    captures_by_row: dict[int, dict[str, Any]] | None = None,
) -> None:
    """Write exact run results as quote evidence, positioning only captures.

    The runner sidecar is the authority for cell lineage and coordinates. A
    missing/malformed sidecar degrades to quote-only evidence; the writer never
    reconstructs execution input from the current sheet overlay.
    """
    if not row_ids:
        return
    from frisket.engine.store.evidence import (
        record_evidence_link,
        record_source_artifact,
        record_source_span,
        record_text_surface,
    )
    from frisket.engine.store.result_generations import ResultGenerationStore

    result_rows = []
    for start in range(0, len(row_ids), _SQLITE_ID_CHUNK_SIZE):
        chunk = row_ids[start : start + _SQLITE_ID_CHUNK_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        result_rows.extend(
            project.db.execute(
                "SELECT row_id, value, justification FROM results "
                f"WHERE run_id=? AND column_id=? AND row_id IN ({placeholders}) "
                "AND error IS NULL ORDER BY row_id",
                (run_id, output_column_id, *chunk),
            ).fetchall()
        )
    result_rows.sort(key=lambda row: int(row["row_id"]))
    first_input_column_id = next(iter(input_column_ids.values()), None)
    engine = runner_spec.get("engine")
    input_column_id_set = {int(value) for value in input_column_ids.values()}
    for result_row in result_rows:
        row_id = int(result_row["row_id"])
        if (
            project.db.execute(
                "SELECT 1 FROM evidence_links "
                "WHERE receipt_id=? AND row_id=? AND column_id=? LIMIT 1",
                (receipt_id, row_id, output_column_id),
            ).fetchone()
            is not None
        ):
            continue
        try:
            raw_value = json.loads(result_row["value"] or "null")
        except (TypeError, ValueError):
            raw_value = None
        entities = [
            entity
            for entity in (raw_value if isinstance(raw_value, list) else [])
            if isinstance(entity, dict) and isinstance(entity.get("text"), str)
        ]
        capture = _validated_ner_capture(
            (captures_by_row or {}).get(row_id),
            raw_value,
            expected_result_ref={
                "run_id": run_id,
                "op_id": op_id,
                "row_id": row_id,
                "column_id": output_column_id,
            },
        ) or _parse_ner_capture(result_row["justification"], raw_value)
        surface_spec = capture.get("surface") if capture is not None else None
        positions = capture.get("positions") if capture is not None else None
        if (
            not isinstance(surface_spec, dict)
            or type(surface_spec.get("column_id")) is not int
            or surface_spec["column_id"] not in input_column_id_set
            or not isinstance(surface_spec.get("value_ref"), dict)
            or surface_spec.get("offset_unit") != "unicode_codepoint"
            or not isinstance(positions, list)
            or len(positions) != len(entities)
        ):
            surface_spec = None
            positions = [None] * len(entities)

        validated_positions: list[list[tuple[int, int]]] = []
        for entity, entity_positions in zip(entities, positions, strict=True):
            valid: list[tuple[int, int]] = []
            seen: set[tuple[int, int]] = set()
            for position in (
                entity_positions if isinstance(entity_positions, list) else []
            ):
                if (
                    isinstance(position, list)
                    and len(position) == 2
                    and type(position[0]) is int
                    and type(position[1]) is int
                    and 0 <= position[0] < position[1]
                    and position[1] - position[0] == len(entity["text"])
                ):
                    pair = (position[0], position[1])
                    if pair not in seen:
                        valid.append(pair)
                        seen.add(pair)
            validated_positions.append(valid)

        surface_id = None
        if surface_spec is not None and any(validated_positions):
            surface = record_text_surface(
                project,
                surface_kind="cell",
                content_hash=str(surface_spec.get("content_hash") or ""),
                offset_unit="unicode_codepoint",
                text_sheet_id=sheet_id,
                text_row_id=row_id,
                text_column_id=int(surface_spec["column_id"]),
                value_ref=surface_spec["value_ref"],
            )
            surface_id = int(surface["id"])

        if not entities:
            continue
        artifact = record_source_artifact(
            project,
            artifact_kind="text",
            media_type="text/plain",
            source_sheet_id=sheet_id,
            source_row_id=row_id,
            source_column_id=(
                int(surface_spec["column_id"])
                if surface_spec is not None
                else first_input_column_id
            ),
            metadata={"engine": engine, "op": "map.ner"},
        )
        span_entries: list[tuple[dict[str, Any], bool]] = []
        for entity, entity_positions in zip(entities, validated_positions, strict=True):
            emitted_positions: list[tuple[int, int] | None] = (
                list(entity_positions) if entity_positions else [None]
            )
            seen_occurrences: set[tuple[int, int] | None] = set()
            for position in emitted_positions:
                if position in seen_occurrences:
                    continue
                seen_occurrences.add(position)
                span_entries.append(
                    (
                        record_source_span(
                            project,
                            artifact_id=artifact["id"],
                            span_kind="text",
                            char_start=position[0] if position is not None else None,
                            char_end=position[1] if position is not None else None,
                            quote=entity.get("text"),
                            text_layer_hash=(
                                str(surface_spec["content_hash"])
                                if position is not None and surface_spec is not None
                                else None
                            ),
                            text_surface_id=surface_id
                            if position is not None
                            else None,
                            metadata={"entity_type": entity.get("type")},
                        ),
                        position is not None,
                    )
                )
        managed = (
            ResultGenerationStore(project).get_binding(run_id, output_column_id)
            is not None
        )
        subject_ref = {
            "kind": "run_result",
            "op_id": op_id if managed else None,
            "row_id": row_id,
            "column_id": output_column_id,
            "run_id": run_id,
        }
        record_evidence_link(
            project,
            subject_kind="cell",
            subject_ref=subject_ref,
            spans=[
                {
                    "span_id": span["id"],
                    "rank": i,
                    "span_role": "annotation" if positioned else "support",
                }
                for i, (span, positioned) in enumerate(span_entries)
            ],
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=output_column_id,
            run_id=run_id,
            op_id=op_id,
            receipt_id=receipt_id,
            layer_family="entities" if any(validated_positions) else None,
            producer={"kind": "map.ner", "engine": engine},
        )


def _parse_ner_capture(justification: Any, value: Any) -> dict[str, Any] | None:
    if not isinstance(justification, str) or not justification.startswith(
        _NER_CAPTURE_PREFIX
    ):
        return None
    try:
        payload = json.loads(justification[len(_NER_CAPTURE_PREFIX) :])
    except (TypeError, ValueError):
        return None
    return _validated_ner_capture(payload, value)


def _validated_ner_capture(
    payload: Any,
    value: Any,
    *,
    expected_result_ref: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _NER_CAPTURE_SCHEMA
        or payload.get("result_hash") != _value_hash(value)
        or (
            expected_result_ref is not None
            and payload.get("result_ref") != expected_result_ref
        )
    ):
        return None
    return payload


def _write_ner_result_evidence(
    project: Any,
    runner_spec: dict[str, Any],
    *,
    batch: list[dict[str, Any]],
    sheet_id: int,
    run_id: int,
    op_id: int,
    output_columns: dict[str, int],
    input_column_ids: dict[str, int],
    claim_token: str | None,
) -> None:
    out_name = str(runner_spec.get("output_name") or "entities")
    output_column_id = output_columns.get(out_name)
    if output_column_id is None:
        return
    captures: dict[int, dict[str, Any]] = {}
    for item in batch:
        if int(item["column_id"]) != int(output_column_id):
            continue
        capture = _validated_ner_capture(
            item.get(_NER_EVIDENCE_KEY),
            item.get("value"),
            expected_result_ref={
                "run_id": run_id,
                "op_id": op_id,
                "row_id": int(item["row_id"]),
                "column_id": int(output_column_id),
            },
        )
        if item.get(_NER_EVIDENCE_KEY) is not None and capture is None:
            raise ValueError("invalid NER evidence capture envelope")
        if capture is not None:
            captures[int(item["row_id"])] = capture
    if not captures or not isinstance(claim_token, str) or not claim_token:
        return
    claim = project.db.execute(
        "SELECT receipt_id FROM output_column_claims "
        "WHERE claim_token=? AND run_id=? AND op_id=? AND column_id=? "
        "AND status='active'",
        (claim_token, run_id, op_id, int(output_column_id)),
    ).fetchone()
    if claim is None or not isinstance(claim["receipt_id"], str):
        return
    _write_entity_spans(
        project,
        runner_spec,
        sheet_id=sheet_id,
        output_column_id=int(output_column_id),
        run_id=run_id,
        op_id=op_id,
        receipt_id=str(claim["receipt_id"]),
        input_column_ids=input_column_ids,
        row_ids=sorted(captures),
        captures_by_row=captures,
    )
