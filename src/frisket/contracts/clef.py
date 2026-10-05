"""Text classification wire shared by Clef execution and cost estimation."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


def classification_text(values: Mapping[str, Any]) -> str:
    return "\n".join(
        str(value)
        for value in values.values()
        if value is not None and not isinstance(value, dict)
    )


def clef_questions(
    fields: Sequence[Mapping[str, Any]], context: str = ""
) -> dict[str, dict[str, Any]]:
    if not 1 <= len(fields) <= 64:
        raise ValueError("Clef requires between 1 and 64 fields")
    questions = {}
    for index, field in enumerate(fields):
        kind = field["type"]
        instruction = field.get("description") or str(field["name"])
        if context:
            instruction = f"Dataset context: {context}\n\n{instruction}"
        question: dict[str, Any] = {"instructions": instruction}
        if kind == "boolean":
            question["type"] = "noul"
        elif kind in {"category", "score"}:
            labels = (
                field["labels"] if kind == "category" else list(map(str, range(11)))
            )
            if not 2 <= len(labels) <= 254:
                raise ValueError("Clef category fields require 2 to 254 labels")
            descriptions = field.get("label_descriptions", {})
            question.update(
                type="choice",
                criteria={
                    f"o{i}": f"{label}: {descriptions[label]}"
                    if label in descriptions
                    else str(label)
                    for i, label in enumerate(labels)
                },
            )
            if kind == "score":
                question["instructions"] += "\nChoose an integer score from 0 to 10."
        else:
            raise ValueError("Clef supports category, boolean, and score fields only")
        # User column names and labels may contain spaces, Unicode or punctuation.
        # They are descriptions, never provider-constrained question/option IDs.
        questions[f"q{index}"] = question
    return questions


def estimate_clef_input_tokens(text: str, questions: Mapping[str, Any]) -> int:
    """Rough UTF-8-based estimate including question text, not a tokenizer."""
    wire = json.dumps({"state": text, "questions": questions}, ensure_ascii=False)
    return math.ceil(len(wire.encode("utf-8")) / 4) + 64


def _probability(value: Any) -> float:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ValueError("Clef returned an invalid probability")
    return float(value)


def clef_values(
    answers: Any, fields: Sequence[Mapping[str, Any]]
) -> dict[str, tuple[Any, float]]:
    """Decode decisions without trusting provider-chosen labels or score types."""
    if not isinstance(answers, dict) or set(answers) != {
        f"q{i}" for i in range(len(fields))
    }:
        raise ValueError("Clef answers do not match the requested fields")
    values = {}
    for index, field in enumerate(fields):
        answer = answers[f"q{index}"]
        if not isinstance(answer, dict):
            raise ValueError("Clef returned an invalid answer")
        if answer.get("type") != ("noul" if field["type"] == "boolean" else "choice"):
            raise ValueError("Clef returned an unexpected answer type")
        if field["type"] == "boolean":
            probability = _probability(answer.get("noul"))
            value = probability >= 0.5
            confidence = probability if value else 1 - probability
        else:
            labels = field["labels"] if field["type"] == "category" else list(range(11))
            options = {f"o{i}": label for i, label in enumerate(labels)}
            probabilities = answer.get("probabilities")
            choice = answer.get("choice")
            if not isinstance(probabilities, dict) or set(probabilities) != set(
                options
            ):
                raise ValueError("Clef probabilities do not match the requested labels")
            scores = {key: _probability(p) for key, p in probabilities.items()}
            # SystemOne rounds each option probability to four decimal places.
            if not math.isclose(
                sum(scores.values()), 1, abs_tol=len(scores) * 0.00005 + 1e-8
            ):
                raise ValueError("Clef probabilities must sum to one")
            if not isinstance(choice, str) or choice not in options:
                raise ValueError("Clef returned an unknown label")
            if scores[choice] < max(scores.values()):
                raise ValueError("Clef choice disagrees with its probabilities")
            value, confidence = options[choice], scores[choice]
        values[field["name"]] = (value, confidence)
    return values
