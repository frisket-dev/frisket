"""Small whole-page perceptual signatures for packet-family matching."""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image


PHASH_BITS = 256
_DCT_SIZE = 64
_LOW_FREQUENCY_SIZE = 16
_SIGNATURE_LIMIT = 1 << PHASH_BITS


def page_perceptual_signature(image: Image.Image) -> int:
    """Return a 256-bit DCT perceptual hash for one complete page image.

    The 16 x 16 low-frequency DCT block keeps coarse page structure while
    suppressing much of the character-level variation.  Returning an integer
    makes the native Hamming comparison a single xor plus ``bit_count``.
    """

    if not isinstance(image, Image.Image):
        raise TypeError("image must be a PIL Image")
    if image.width < 1 or image.height < 1:
        raise ValueError("image must have nonzero dimensions")

    pixels = np.asarray(
        image.convert("L").resize((_DCT_SIZE, _DCT_SIZE), Image.Resampling.LANCZOS),
        dtype=np.float32,
    )
    low_frequency = cv2.dct(pixels)[:_LOW_FREQUENCY_SIZE, :_LOW_FREQUENCY_SIZE]
    bits = (low_frequency > np.median(low_frequency)).flat
    signature = 0
    for bit in bits:
        signature = (signature << 1) | int(bit)
    return signature


def _validated_signature(value: int, *, label: str) -> int:
    if type(value) is not int or not 0 <= value < _SIGNATURE_LIMIT:
        raise ValueError(f"{label} must be a 256-bit nonnegative integer")
    return value


def signature_similarity(left: int, right: int) -> float:
    """Return normalized Hamming similarity for two 256-bit signatures."""

    left = _validated_signature(left, label="left signature")
    right = _validated_signature(right, label="right signature")
    return 1.0 - ((left ^ right).bit_count() / PHASH_BITS)


__all__ = ["PHASH_BITS", "page_perceptual_signature", "signature_similarity"]
