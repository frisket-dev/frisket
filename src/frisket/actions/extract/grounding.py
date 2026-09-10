from __future__ import annotations

from pydantic import ValidationError

from frisket.actions.extraction_types import (
    ExtractField,
    ExtractGrounding,
    extraction_output_fields,
)
from frisket.actions.grounding_types import EvidenceBox, EvidenceClaim
from frisket.actions.types import DynamicOutput, Outcome, RowResult
from frisket.ops.extraction import normalize_extracted_value


def _claim_integer(value):
    try:
        return int(value) if not isinstance(value, bool) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _evidence_claim(raw, *, item_index=None):
    """Drop unknown/invalid hints independently, never accept caller source IDs."""
    if not isinstance(raw, dict):
        return None
    data = {key: raw[key] for key in EvidenceClaim.model_fields if key in raw}
    data["item_index"] = item_index
    indices = raw.get("segment_indices")
    data["segment_indices"] = [
        number
        for value in (indices if isinstance(indices, list) else [])
        if (number := _claim_integer(value)) is not None
    ]
    for key in ("page", "page_start", "page_end"):
        if key in data:
            data[key] = _claim_integer(data[key])
    bbox = raw.get("bbox")
    if isinstance(bbox, list):
        bbox = bbox[0] if bbox else None
    box = (
        {key: value for key, value in bbox.items() if key in EvidenceBox.model_fields}
        if isinstance(bbox, dict)
        else {}
    )
    if isinstance(bbox, dict):
        if all(key in bbox for key in ("x", "y", "w", "h")):
            try:
                box.update(
                    x0=float(bbox["x"]),
                    y0=float(bbox["y"]),
                    x1=float(bbox["x"]) + float(bbox["w"]),
                    y1=float(bbox["y"]) + float(bbox["h"]),
                    space="pixel",
                )
            except (TypeError, ValueError):
                pass
        for axis in ("width", "height"):
            box.setdefault(f"page_{axis}", bbox.get(axis, bbox.get(f"image_{axis}")))
    try:
        data["bbox"] = EvidenceBox.model_validate(box)
    except ValidationError:
        data["bbox"] = None
    for key in ("quote", "snippet", "grounding_method"):
        if data.get(key) is not None:
            data[key] = str(data[key]).strip() or None
    return EvidenceClaim.model_validate(data)


def complete_extraction(
    *,
    fields: list[ExtractField],
    include_confidence: bool,
    grounding: ExtractGrounding | None,
    response: DynamicOutput,
) -> RowResult[DynamicOutput]:
    output = {}
    output_fields = extraction_output_fields(
        fields, include_confidence=include_confidence
    )
    confidence = (
        response.root.get(f"{fields[0].name}_confidence")
        if include_confidence
        else None
    )
    grounded_names = (
        {field.name for field in fields} if grounding and grounding.enabled else set()
    )
    for name, field in output_fields.items():
        raw = response.root.get(name)
        grounded = name in grounded_names and isinstance(raw, dict)
        value = normalize_extracted_value(
            name, raw.get("value") if grounded else raw, dict(field.schema)
        )
        claims, warnings = [], []
        if grounded:
            raw_warnings = raw.get("warnings")
            warnings = [
                str(item)
                for item in (raw_warnings if isinstance(raw_warnings, list) else [])
                if item is not None
            ]
            raw_evidence = raw.get("evidence")
            evidence = raw_evidence if isinstance(raw_evidence, list) else []
            entries = (
                (
                    (index, entry)
                    for index, group in enumerate(evidence)
                    if isinstance(group, list)
                    for entry in group
                )
                if field.schema.get("type") == "array"
                else ((None, entry) for entry in evidence)
            )
            for index, entry in entries:
                claim = _evidence_claim(entry, item_index=index)
                if claim is not None:
                    claims.append(claim)
        output[name] = Outcome.ok(
            value,
            confidence=confidence,
            evidence=tuple(claims),
            warnings=tuple(warnings),
        )
    return RowResult(output=DynamicOutput(root=output))
