"""Approximate direct-PDF input tokens without treating encoded bytes as text."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from frisket.ai.llm.pricing import estimate_tokens


def estimate_input_tokens(value: Any, **json_options: Any) -> int | None:
    """Preserve ordinary estimates; PDF page/native-text metadata owns its estimate.

    The per-page allowance accounts conservatively for page images charged by
    PDF-capable providers. It is an estimate, not a maximum spend guarantee.
    Missing page counts remain unknown rather than pricing base64; native text
    is optional when the parser cannot extract it.
    """
    pdf_tokens = 0
    known = True

    def without_bytes(item: Any) -> Any:
        nonlocal pdf_tokens, known
        if isinstance(item, Mapping):
            if (
                item.get("type") == "document"
                and item.get("media_type") == "application/pdf"
            ):
                pages, chars = item.get("page_count"), item.get("text_char_count")
                if (
                    type(pages) is not int
                    or pages < 1
                    or (chars is not None and (type(chars) is not int or chars < 0))
                ):
                    known = False
                else:
                    pdf_tokens += 3000 * pages + ((chars or 0) + 3) // 4
                return {"type": "document", "media_type": "application/pdf"}
            return {key: without_bytes(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [without_bytes(child) for child in item]
        return item

    text = json.dumps(without_bytes(value), **json_options)
    return estimate_tokens(text) + pdf_tokens if known else None
