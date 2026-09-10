"""Engine-neutral helpers for structured model message content."""

from __future__ import annotations

from typing import Any


def render_input_block(row_values: dict[str, Any]) -> list[dict[str, Any]]:
    """Render resolved row values as labeled text plus image content parts."""

    parts: list[dict[str, Any]] = []
    text_bits: list[str] = []
    for name, value in row_values.items():
        if isinstance(value, dict) and value.get("__image_b64__"):
            parts.append(
                {
                    "type": "image",
                    "media_type": value.get("mime", "image/jpeg"),
                    "data": value["__image_b64__"],
                }
            )
        elif value is not None:
            text_bits.append(f"{name}: {value}")
    if text_bits:
        parts.insert(0, {"type": "text", "text": "\n".join(text_bits)})
    return parts
