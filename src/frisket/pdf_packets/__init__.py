"""Pure matching primitives for splitting whole-page PDF packets."""

from frisket.pdf_packets.matching import (
    PacketMatchResult,
    PageMatch,
    PhraseRule,
    StartKind,
    match_packet_pages,
)
from frisket.pdf_packets.signatures import (
    PHASH_BITS,
    page_perceptual_signature,
    signature_similarity,
)

__all__ = [
    "PacketMatchResult",
    "PHASH_BITS",
    "PageMatch",
    "PhraseRule",
    "StartKind",
    "match_packet_pages",
    "page_perceptual_signature",
    "signature_similarity",
]
