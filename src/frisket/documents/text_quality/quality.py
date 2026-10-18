"""Public OCR/text-quality boundary."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray

from frisket.actions.document_extraction_types import PositionedToken

from ._page import page_quality_signals
from ._text import has_encoding_damage, has_garbled_text


class TextQualityFlag(StrEnum):
    """Specific observations that can support a manual-review hint."""

    EMPTY = "empty"
    ENCODING_DAMAGE = "encoding_damage"
    GARBLED_TEXT = "garbled_text"
    PREDOMINANTLY_VERTICAL_WORDS = "predominantly_vertical_words"
    UNEXPLAINED_TEXTLIKE_INK = "unexplained_textlike_ink"


def assess_text_quality(
    text: str,
    *,
    grayscale_thumbnail: NDArray[np.uint8] | None = None,
    positioned_tokens: Sequence[PositionedToken] | None = None,
) -> tuple[TextQualityFlag, ...]:
    """Return reason flags for text that may need manual review.

    Text checks inspect at most 12,000 characters and run independently. Page
    flags require an aligned, already-rendered grayscale thumbnail and a
    complete sequence of normalized word tokens. This function never renders
    a page or invokes OCR.

    No flags means that these narrow checks found no issue; it is not evidence
    that extraction is accurate or complete.
    """

    if not isinstance(text, str):
        raise TypeError("text must be str")

    flags: list[TextQualityFlag] = []
    if not text.strip():
        flags.append(TextQualityFlag.EMPTY)
    else:
        if has_encoding_damage(text):
            flags.append(TextQualityFlag.ENCODING_DAMAGE)
        if has_garbled_text(text):
            flags.append(TextQualityFlag.GARBLED_TEXT)

    vertical_words, unexplained_ink = page_quality_signals(
        grayscale_thumbnail=grayscale_thumbnail,
        positioned_tokens=positioned_tokens,
    )
    if vertical_words:
        flags.append(TextQualityFlag.PREDOMINANTLY_VERTICAL_WORDS)
    if unexplained_ink:
        flags.append(TextQualityFlag.UNEXPLAINED_TEXTLIKE_INK)
    return tuple(flags)
