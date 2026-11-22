"""Backend-authored descriptive metadata for the built-in walkthroughs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WalkthroughMetadata:
    id: str
    badges: tuple[str, ...]


WALKTHROUGHS: tuple[WalkthroughMetadata, ...] = (
    WalkthroughMetadata("project-ask", ("Project Ask", "Citations")),
    WalkthroughMetadata("lawsuit-documents", ("PDFs", "OCR", "Review")),
    WalkthroughMetadata("civic-ai-triage", ("AI triage", "Views", "Judge")),
    WalkthroughMetadata("combine-values", ("Data cleanup", "Clustering")),
    WalkthroughMetadata("council-audio", ("Audio", "Transcription")),
    WalkthroughMetadata("rss-import", ("RSS", "Views", "Alerts")),
    WalkthroughMetadata("multilingual-names", ("Translate", "Transliterate")),
    WalkthroughMetadata("regex-extract", ("Regex", "Joins")),
    WalkthroughMetadata("local-model-lab", ("Local AI",)),
)


def walkthrough_catalog() -> dict[str, list[dict[str, object]]]:
    return {
        "walkthroughs": [
            {"id": walkthrough.id, "badges": list(walkthrough.badges)}
            for walkthrough in WALKTHROUGHS
        ]
    }
