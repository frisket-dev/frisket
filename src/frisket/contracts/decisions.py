"""OpenAI Decisions wire projection, decoding and provider usage pricing."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from frisket.contracts.clef import clef_questions

DECISIONS_MODEL = "gpt-6-luna"
DECISIONS_URL = "https://api.openai.com/v1/decisions"
LONG_CONTEXT_TOKENS = 272_000


def decisions_questions(
    fields: Sequence[Mapping[str, Any]], context: str = ""
) -> list[dict[str, Any]]:
    questions = clef_questions(fields, context, engine="openai-decisions")
    return [
        {
            "name": name,
            "instructions": question["instructions"],
            **(
                {"type": "predicate"}
                if question["type"] == "noul"
                else {
                    "type": "choice",
                    "choices": [
                        {"value": value, "description": description}
                        for value, description in question["criteria"].items()
                    ],
                }
            ),
        }
        for name, question in questions.items()
    ]


def estimate_decisions_tokens(text: str, questions: Sequence[Mapping[str, Any]]) -> int:
    """Rough billable-token equivalent, allowing for an implicit cache write."""
    wire = json.dumps({"input": text, "questions": questions}, ensure_ascii=False)
    tokens = math.ceil(len(wire.encode("utf-8")) / 4) + 64
    return math.ceil(tokens * 1.25 * (2 if tokens > LONG_CONTEXT_TOKENS else 1))


def decisions_usage(
    usage: Any, input_rate: float | None
) -> tuple[dict[str, int], float | None]:
    """Cache read/write counts are subsets of input; output is free for Decisions."""
    if not isinstance(usage, dict):
        return {}, None
    total = usage.get("input_tokens")
    details = usage.get("input_tokens_details")
    if type(total) is not int or total < 0 or not isinstance(details, dict):
        return {}, None
    cached, written = details.get("cached_tokens"), details.get("cache_write_tokens")
    if (
        any(type(n) is not int or n < 0 for n in (cached, written))
        or cached + written > total
    ):
        return {}, None
    units = {
        "input_tokens": total,
        "cached_tokens": cached,
        "cache_write_tokens": written,
    }
    if input_rate is None:
        return units, None
    # Official pricing: cache reads 0.1x, 30m writes 1.25x; long context 2x.
    equivalent = (
        Decimal(total - cached - written)
        + Decimal(cached) / 10
        + Decimal(written) * Decimal("1.25")
    )
    return units, float(
        equivalent
        * Decimal(str(input_rate))
        * (2 if total > LONG_CONTEXT_TOKENS else 1)
    )


def _probability(value: Any) -> float:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ValueError("OpenAI Decisions returned an invalid probability")
    return float(value)


def decisions_values(
    answers: Any, fields: Sequence[Mapping[str, Any]]
) -> dict[str, tuple[Any, float] | None]:
    """None represents a per-field refusal, distinct from a false predicate."""
    if not isinstance(answers, list) or len(answers) != len(fields):
        raise ValueError("OpenAI Decisions answers do not match requested fields")
    indexed = {}
    for answer in answers:
        if not isinstance(answer, dict) or not isinstance(answer.get("name"), str):
            raise ValueError("OpenAI Decisions returned an invalid answer")
        indexed[answer["name"]] = answer
    if set(indexed) != {f"q{i}" for i in range(len(fields))}:
        raise ValueError("OpenAI Decisions answers do not match requested fields")
    values = {}
    for index, field in enumerate(fields):
        answer = indexed[f"q{index}"]
        if answer.get("type") == "refusal":
            values[field["name"]] = None
            continue
        kind = "predicate" if field["type"] == "boolean" else "choice"
        if answer.get("type") != kind:
            raise ValueError("OpenAI Decisions returned an unexpected answer type")
        if kind == "predicate":
            probability = _probability(answer.get("probability"))
            value = probability >= 0.5
            confidence = probability if value else 1 - probability
        else:
            labels = field["labels"] if field["type"] == "category" else list(range(11))
            options = {f"o{i}": label for i, label in enumerate(labels)}
            choice = answer.get("choice")
            if not isinstance(choice, str) or choice not in options:
                raise ValueError("OpenAI Decisions returned an unknown choice")
            value = options[choice]
            # Confidence is a separate calibrated value, not the winning probability.
            confidence = _probability(answer.get("confidence"))
        values[field["name"]] = (value, confidence)
    return values
