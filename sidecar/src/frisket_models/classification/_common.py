from __future__ import annotations

import math
from pathlib import Path

MAX_LABELS = 255


def snapshot_directory(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("model snapshot path must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"model snapshot does not exist: {path}") from exc
    if not resolved.is_dir():
        raise ValueError(f"model snapshot is not a directory: {path}")
    return resolved


def validate_request(
    text: str,
    labels: list[str],
    descriptions: dict[str, str],
    instruction: str,
    *,
    max_labels: int = MAX_LABELS,
) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-blank string")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("instruction must be a non-blank string")
    if not isinstance(labels, list) or not 2 <= len(labels) <= max_labels:
        raise ValueError(f"labels must contain between 2 and {max_labels} items")
    if any(not isinstance(label, str) or not label.strip() for label in labels):
        raise ValueError("labels must be non-blank strings")
    if len(set(labels)) != len(labels):
        raise ValueError("labels must be unique")
    if not isinstance(descriptions, dict):
        raise ValueError("descriptions must be a dictionary")
    unknown = set(descriptions).difference(labels)
    if unknown:
        raise ValueError("descriptions contains an unknown label")
    if any(not isinstance(value, str) for value in descriptions.values()):
        raise ValueError("description values must be strings")


def candidate_texts(labels: list[str], descriptions: dict[str, str]) -> list[str]:
    candidates = []
    for label in labels:
        candidate = label
        description = descriptions.get(label, "").strip()
        if description:
            candidate += f": {description}"
        candidates.append(candidate)
    if len(set(candidates)) != len(candidates):
        raise ValueError("label descriptions produce duplicate model candidates")
    return candidates


def checked_score(value: object) -> float:
    try:
        score = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("classifier score must be a number") from exc
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ValueError("classifier score must be finite and between 0 and 1")
    return score
