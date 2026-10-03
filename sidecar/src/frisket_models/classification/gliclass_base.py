from __future__ import annotations

from pathlib import Path
from typing import Any

from ._common import (
    MAX_LABELS,
    candidate_texts,
    checked_score,
    snapshot_directory,
    validate_request,
)

MAX_COMBINED_TOKENS = 512


def _load_runtime(snapshot: Path) -> tuple[Any, Any]:
    from gliclass import GLiClassModel, ZeroShotClassificationPipeline
    from transformers import AutoTokenizer

    model = GLiClassModel.from_pretrained(
        str(snapshot), local_files_only=True, trust_remote_code=False
    )
    tokenizer = AutoTokenizer.from_pretrained(
        str(snapshot),
        add_prefix_space=True,
        local_files_only=True,
        trust_remote_code=False,
    )
    pipeline = ZeroShotClassificationPipeline(
        model,
        tokenizer,
        classification_type="single-label",
        device="cpu",
        max_classes=MAX_LABELS,
        max_length=MAX_COMBINED_TOKENS,
        progress_bar=False,
    )
    return pipeline, tokenizer


class GLiClassBase:
    """Thin CPU adapter for a local GLiClass Base v3 snapshot."""

    def __init__(self, snapshot_path: str | Path) -> None:
        self.snapshot_path = snapshot_directory(snapshot_path)
        self.model_revision = self.snapshot_path.name
        self._pipeline, self._tokenizer = _load_runtime(self.snapshot_path)

    def classify(
        self,
        text: str,
        labels: list[str],
        *,
        descriptions: dict[str, str],
        instruction: str,
    ) -> dict[str, str | float]:
        validate_request(text, labels, descriptions, instruction)
        candidates = candidate_texts(labels, descriptions)
        prepared = self._pipeline.pipe.prepare_input(
            text, candidates, prompt=instruction
        )
        combined_tokens = len(self._tokenizer.encode(prepared))
        if combined_tokens > MAX_COMBINED_TOKENS:
            raise ValueError(
                "Combined GLiClass input exceeds the 512-token limit; "
                "no input was truncated."
            )

        batches = self._pipeline(
            text,
            candidates,
            prompt=instruction,
            return_hierarchical=False,
            batch_size=1,
        )
        if (
            not isinstance(batches, list)
            or len(batches) != 1
            or not isinstance(batches[0], list)
            or len(batches[0]) != 1
            or not isinstance(batches[0][0], dict)
        ):
            raise ValueError("GLiClass returned an invalid single-label result")
        prediction = batches[0][0]
        winner = prediction.get("label")
        try:
            winner_index = candidates.index(winner)
        except ValueError as exc:
            raise ValueError("GLiClass returned an unknown label") from exc
        return {
            "label": labels[winner_index],
            "score": checked_score(prediction.get("score")),
            "model_revision": self.model_revision,
        }
