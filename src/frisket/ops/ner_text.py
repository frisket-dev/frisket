"""The exact text surface indexed by named entity extraction."""

from typing import Any, Mapping


def ner_text(values: Mapping[str, Any]) -> str:
    """Inputs have already been rendered by the host, including templates."""
    return "\n".join(
        str(value)
        for value in values.values()
        if value is not None and not isinstance(value, dict)
    )
