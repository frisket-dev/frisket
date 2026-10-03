from __future__ import annotations

from pathlib import Path
from typing import Any

from ._common import checked_score, snapshot_directory, validate_request

MAX_LABELS = 254


def _load_runtime(snapshot: Path, *, cpu_threads: int) -> Any:
    from ._jeff_inference import DecisionModel

    return DecisionModel(
        snapshot,
        cpu_threads=cpu_threads,
        local_files_only=True,
        trust_remote_code=False,
    )


class Jeff:
    """Thin CPU adapter for a local Jeff 0.8B decision-model snapshot."""

    def __init__(self, snapshot_path: str | Path, *, cpu_threads: int = 4) -> None:
        if not isinstance(cpu_threads, int) or cpu_threads < 1:
            raise ValueError("cpu_threads must be a positive integer")
        self.snapshot_path = snapshot_directory(snapshot_path)
        self.model_revision = self.snapshot_path.name
        self._model = _load_runtime(self.snapshot_path, cpu_threads=cpu_threads)

    def classify(
        self,
        text: str,
        labels: list[str],
        *,
        descriptions: dict[str, str],
        instruction: str,
    ) -> dict[str, str | float]:
        validate_request(text, labels, descriptions, instruction, max_labels=MAX_LABELS)
        criteria = {}
        for index, label in enumerate(labels):
            value = label
            description = descriptions.get(label, "").strip()
            if description:
                value += f": {description}"
            criteria[f"option_{index:03d}"] = value
        row = {
            "state": text,
            "question": {
                "type": "choice",
                "instructions": instruction,
                "criteria": criteria,
            },
        }
        batches = self._model.predict([row], batch_size=1)
        if not isinstance(batches, list) or len(batches) != 1:
            raise ValueError("Jeff returned an invalid probability batch")
        probabilities = batches[0]
        if not isinstance(probabilities, (list, tuple)) or len(probabilities) != len(
            labels
        ):
            raise ValueError("Jeff probabilities do not match the supplied labels")
        try:
            scores = [checked_score(value) for value in probabilities]
        except ValueError as exc:
            raise ValueError(
                "Jeff probabilities must be finite and between 0 and 1"
            ) from exc
        if sum(scores) <= 0:
            raise ValueError("Jeff probabilities must have positive mass")
        winner = max(range(len(scores)), key=scores.__getitem__)
        return {
            "label": labels[winner],
            "score": scores[winner],
            "model_revision": self.model_revision,
        }
