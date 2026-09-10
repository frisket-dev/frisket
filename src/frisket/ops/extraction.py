"""Pure structured extraction prompts, schemas, and absence normalization."""

from __future__ import annotations

import logging
from typing import Any

from frisket.ai.message_content import render_input_block


def extract_response_schema(
    fields: list[dict[str, Any]],
    *,
    grounding_enabled: bool,
    grounding_fields: set[str] | None = None,
    nullable: bool = False,
) -> dict[str, Any]:
    def value_schema(field):
        schema = field["schema"]
        return {"anyOf": [schema, {"type": "null"}]} if nullable else schema

    if not grounding_enabled:
        props = {field["name"]: value_schema(field) for field in fields}
        return {"type": "object", "properties": props, "required": list(props)}
    props: dict[str, Any] = {}
    for field in fields:
        name = field["name"]
        if (
            name not in grounding_fields
            if grounding_fields is not None
            else name.endswith(("_confidence", "_justification"))
        ):
            props[name] = value_schema(field)
            continue
        # Declare the two anchors the resolution chain actually consumes so
        # the structured-output stack steers the model toward them:
        # `segment_indices` (the reliable transcript lookup) and
        # a verbatim `quote` (the align fallback). `additionalProperties`
        # stays open so page/bbox/snippet/grounding_method still ride along
        # for the OCR/PDF path.
        evidence_item_schema = {
            "type": "object",
            "properties": {
                "segment_indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "quote": {"type": "string"},
            },
            # Both anchors are optional per evidence item (a citation may
            # carry either or both); explicit so jsonschema's nothing-
            # required default is a decision, not an accident.
            "required": [],
            "additionalProperties": True,
        }
        # A LIST-typed field's evidence is a
        # list of the SAME length as `value`, index-aligned -- evidence[i]
        # supports value[i] (one evidence set per list item, mirroring
        # map.ner's per-entity write shape). Scalar fields keep the flat
        # evidence-list shape unchanged.
        is_list_field = field["schema"].get("type") == "array"
        evidence_schema = (
            {
                "type": "array",
                "items": {"type": "array", "items": evidence_item_schema},
            }
            if is_list_field
            else {"type": "array", "items": evidence_item_schema}
        )
        props[name] = {
            "type": "object",
            "properties": {
                "value": value_schema(field),
                "evidence": evidence_schema,
                "warnings": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["value"],
            "additionalProperties": True,
        }
    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
    }


def render_extract_messages(
    row_values: dict[str, Any],
    fields: list[dict[str, Any]],
    *,
    instruction: str = "Extract the requested fields.",
    context: str = "",
    grounding_enabled: bool = False,
) -> list[dict[str, Any]]:
    system = (
        "You extract structured data from documents for an investigative "
        "journalist. Extract only what is actually present; use empty "
        "lists/nulls when absent. Never fabricate."
    )
    if context:
        system += f"\n\nDataset context: {context}"
    segment_columns = _segment_stream_columns(row_values) if grounding_enabled else []
    rendered_row_values = dict(row_values)
    for name in segment_columns:
        rendered_row_values[name] = _numbered_segments_text(row_values[name])
    user_parts = render_input_block(rendered_row_values)
    user_parts.append({"type": "text", "text": f"\n{instruction}"})
    if grounding_enabled:
        evidence_copy = (
            "\nFor each extracted field, return an object with `value`, "
            "optional `evidence`, and optional `warnings`. Evidence items "
            "may cite a page, bbox in normalized page coordinates, quote, "
            "snippet, and grounding_method. Do not invent evidence."
        )
        list_fields_present = any(
            field["schema"].get("type") == "array"
            for field in fields
            if not field["name"].endswith(("_confidence", "_justification"))
        )
        if list_fields_present:
            evidence_copy += (
                " When a field's value is a list, `evidence` is a list of "
                "the SAME length as `value`: evidence[i] is the list of "
                "evidence items supporting value[i] (use [] for an item "
                "with none)."
            )
        if segment_columns:
            evidence_copy += (
                " Transcript segments above are shown as numbered units "
                "`[i] (start-end) text`. For an item supported by those "
                "segments, you MUST cite it by returning `segment_indices` "
                "(the exact numbers shown, e.g. [2, 3]) in that evidence "
                "item — list every segment the supporting passage spans. "
                "Keep a short verbatim `quote` alongside; prefer the "
                "numbers, they are the reliable anchor."
            )
        user_parts.append({"type": "text", "text": evidence_copy})
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_parts},
    ]


def _segment_stream_columns(row_values: dict[str, Any]) -> list[str]:
    """Column names whose value is a transcribe-shaped segment list (a JSON
    array of ``{text, start, end, ...}`` dicts) -- the transcript-sourced
    list-item grounding DEFAULT (W2.3) numbers these in the prompt so the
    model can return ``segment_indices`` (strategy `anchor_id`/B) instead of a
    free-text quote it would otherwise have to be aligned against post-hoc."""
    columns: list[str] = []
    for name, value in row_values.items():
        if not isinstance(value, list) or not value:
            continue
        if all(
            isinstance(item, dict) and "text" in item and "start" in item
            for item in value
        ):
            columns.append(name)
    return columns


def _numbered_segments_text(segments: list[dict[str, Any]]) -> str:
    lines = []
    for rank, segment in enumerate(segments):
        index = segment.get("segment_index", rank)
        start = segment.get("start")
        end = segment.get("end")
        span = f" ({start}s-{end}s)" if start is not None and end is not None else ""
        # Per-turn speaker rides the numbered text as a '[S1]'
        # prefix so the model can attribute a cited statement to a speaker;
        # absent on non-diarized transcripts.
        speaker = segment.get("speaker")
        speaker_tag = f" [{speaker}]" if speaker else ""
        lines.append(f"[{index}]{span}{speaker_tag} {segment.get('text', '')}")
    return "\n".join(lines)


def normalize_extracted_value(field: str, value: Any, schema: dict[str, Any]) -> Any:
    """Normalize whole null/none strings, except explicitly declared enum labels."""
    if (
        not isinstance(value, str)
        or value.strip().lower() not in {"null", "none"}
        or value in (schema.get("enum") or [])
    ):
        return value
    logging.getLogger("frisket.executor").info(
        "map_extract_null_string_coerced",
        extra={
            "event": "map_extract_null_string_coerced",
            "field": field,
            "token": value.strip(),
        },
    )
    return None
