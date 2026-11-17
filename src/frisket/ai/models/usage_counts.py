"""Pure aggregation of complete or unknown diagnostic usage counts."""

from collections.abc import Iterable, Mapping
from typing import Any


def sum_known_counts(values: Iterable[int | None]) -> int | None:
    """An incomplete count is unknown, not a confidently partial total."""
    total = 0
    for value in values:
        if value is None:
            return None
        total += int(value)
    return total


def model_call_token_counts(
    calls: Iterable[Mapping[str, Any]],
) -> dict[str, int | None]:
    """Count applicable meters, preserving unknowns and excluding non-token work.

    Completion facts use tokens_in/out; input-only classifier and embedding
    meters use input_tokens. These are physical meters, not provider names.
    Existing LLM facts without keys still mean unreported token usage.
    """
    calls = list(calls)
    totals = {}
    for name, meter in (("tokens_in", "input_tokens"), ("tokens_out", "output_tokens")):
        counts = []
        for call in calls:
            units = call.get("units", {})
            if name in units:
                counts.append(units[name])
            elif meter in units:
                counts.append(units[meter])
            elif call.get("capability") == "llm.complete":
                counts.append(None)
        totals[name] = sum_known_counts(counts) if counts else None
    return totals
