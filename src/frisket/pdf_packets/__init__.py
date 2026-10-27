"""Pure matching primitives for splitting whole-page PDF packets."""

from frisket.pdf_packets.matching import (
    PacketMatchResult,
    PageMatch,
    PhraseRule,
    StartKind,
    match_packet_pages,
)
from frisket.pdf_packets.materialize import write_pdf_range
from frisket.pdf_packets.source_map import (
    PageAddress,
    SourceMap,
    SourceOccurrence,
    SourceRange,
)
from frisket.pdf_packets.signatures import (
    PHASH_BITS,
    page_perceptual_signature,
    signature_similarity,
)

__all__ = [
    "PacketMatchResult",
    "PHASH_BITS",
    "PageAddress",
    "PageMatch",
    "PhraseRule",
    "SourceMap",
    "SourceOccurrence",
    "SourceRange",
    "StartKind",
    "match_packet_pages",
    "page_perceptual_signature",
    "signature_similarity",
    "write_pdf_range",
]
