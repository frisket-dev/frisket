"""Pure matching primitives for splitting whole-page PDF packets."""

from frisket.pdf_packets.matching import (
    PacketMatchResult,
    PageMatch,
    PhraseRule,
    StartKind,
    match_packet_pages,
)

__all__ = [
    "PacketMatchResult",
    "PageMatch",
    "PhraseRule",
    "StartKind",
    "match_packet_pages",
]
