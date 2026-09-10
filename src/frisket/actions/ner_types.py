"""The admitted local named-entity extraction capability."""

from typing import Any, Protocol


DEFAULT_NER_ENGINE = "spacy"
RECOMMENDED_NER_LABELS = ("person", "organization", "location", "date", "money")


class NerExtractor(Protocol):
    async def extract(
        self, text: str, *, labels: list[str], threshold: float
    ) -> list[dict[str, Any]]: ...
