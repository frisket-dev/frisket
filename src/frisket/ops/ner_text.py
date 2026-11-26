"""The exact text surface indexed by named entity extraction."""

from typing import Any, Mapping


def ner_text(values: Mapping[str, Any]) -> str:
    """Inputs have already been rendered by the host, including templates."""
    return "\n".join(
        str(value)
        for value in values.values()
        if value is not None and not isinstance(value, dict)
    )


def text_occurrences(text: str, passage: str) -> list[tuple[int, int]]:
    """Every non-overlapping exact occurrence of ``passage``, in order."""
    found: list[tuple[int, int]] = []
    start = text.find(passage) if passage else -1
    while start >= 0:
        found.append((start, start + len(passage)))
        start = text.find(passage, start + len(passage))
    return found
