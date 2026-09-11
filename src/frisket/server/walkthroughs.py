"""Backend-authored descriptive metadata for the built-in walkthroughs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WalkthroughMetadata:
    id: str
    badges: tuple[str, ...]


WALKTHROUGHS: tuple[WalkthroughMetadata, ...] = (
    WalkthroughMetadata("regex-extract", ("Regex", "Joins")),
    WalkthroughMetadata("lawsuit-documents", ("PDFs", "AI extraction")),
    WalkthroughMetadata("council-audio", ("Audio", "Transcription")),
    WalkthroughMetadata("civic-ai-triage", ("AI extraction",)),
    WalkthroughMetadata("rss-import", ("RSS", "AI classification")),
    WalkthroughMetadata("combine-values", ("Data cleanup",)),
    WalkthroughMetadata("multilingual-names", ("Multilingual text", "Data cleanup")),
    WalkthroughMetadata("local-model-lab", ("Local AI",)),
)


def walkthrough_catalog() -> dict[str, list[dict[str, object]]]:
    return {
        "walkthroughs": [
            {"id": walkthrough.id, "badges": list(walkthrough.badges)}
            for walkthrough in WALKTHROUGHS
        ]
    }
