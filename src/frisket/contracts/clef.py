"""Text classification wire shared by Clef execution and cost estimation."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from frisket.contracts.classification import validate_classification


CLEF_FLASH_MAX_TEXT_CHARS = 65536
CLEF_FLASH_MAX_SCHEMA_CHARS = 65536
CLEF_FLASH_MAX_TOTAL_OPTIONS = 512
CLEF_FLASH_MAX_DESCRIPTION_CHARS = 4096


def validate_clef_request(
    engine: str,
    questions: Mapping[str, Mapping[str, Any]],
    text: str | None = None,
) -> None:
    """Check the Flash worker's bounds on the rendered request before admission."""
    if engine != "clef-flash":
        return
    if text is not None and len(text) > CLEF_FLASH_MAX_TEXT_CHARS:
        raise ValueError(
            f"Clef Flash input exceeds {CLEF_FLASH_MAX_TEXT_CHARS} characters; "
            "shorten the input."
        )
    chars = options = 0
    for name, question in questions.items():
        instruction = question.get("instructions", "")
        criteria = question.get("criteria") or {}
        descriptions = criteria.values() if isinstance(criteria, Mapping) else criteria
        if any(
            len(description) > CLEF_FLASH_MAX_DESCRIPTION_CHARS
            for description in (instruction, *descriptions)
        ):
            raise ValueError(
                "Clef Flash question instructions and label descriptions must each "
                f"fit {CLEF_FLASH_MAX_DESCRIPTION_CHARS} characters; "
                "shorten the context, instructions, or labels."
            )
        chars += len(name) + len(instruction)
        options += 2 if question["type"] == "noul" else len(criteria)
        if isinstance(criteria, Mapping):
            chars += sum(len(key) + len(value) for key, value in criteria.items())
        else:
            chars += sum(map(len, criteria))
    if options > CLEF_FLASH_MAX_TOTAL_OPTIONS:
        raise ValueError(
            f"Clef Flash supports at most {CLEF_FLASH_MAX_TOTAL_OPTIONS} total "
            "options; reduce the fields or labels."
        )
    if chars > CLEF_FLASH_MAX_SCHEMA_CHARS:
        raise ValueError(
            f"Clef Flash question schema exceeds {CLEF_FLASH_MAX_SCHEMA_CHARS} "
            "characters; shorten the context, instructions, or labels."
        )


def classification_context(params: Mapping[str, Any]) -> str:
    """One effective context for classifier execution and question token estimates."""
    context = params.get("context") or ""
    instruction = params.get("instruction") or ""
    if not instruction:
        return context
    return "\n\n".join(
        part for part in (context, f"Extraction instructions: {instruction}") if part
    )


def classification_text(values: Mapping[str, Any]) -> str:
    return "\n".join(
        str(value)
        for value in values.values()
        if value is not None and not isinstance(value, dict)
    )


def clef_questions(
    fields: Sequence[Mapping[str, Any]], context: str = ""
) -> dict[str, dict[str, Any]]:
    validate_classification("clef", fields)
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
