"""Local NER operations bound to the host-admitted engine and transport."""

from typing import Any, Literal

from frisket.ops._sidecar import sidecar_post
from frisket.ops.base import OpContext
from frisket.ops.entities import canonicalize_entities


class LocalNerExtractor:
    def __init__(self, engine: Literal["spacy", "gliner"], context: OpContext):
        if engine not in ("spacy", "gliner"):
            raise ValueError("unsupported local NER engine")
        self.engine = engine
        self.context = context

    async def extract(
        self, text: str, *, labels: list[str], threshold: float
    ) -> list[dict[str, Any]]:
        if not labels or any(not label.strip() for label in labels):
            raise ValueError("NER requires nonblank entity labels")
        if not 0 <= threshold <= 1:
            raise ValueError("NER threshold must be between zero and one")
        if not text.strip():
            return []
        if self.engine == "spacy":
            from frisket.ops.spacy_ner import run_spacy_ner_default, spacy_available

            available, error = spacy_available()
            if not available:
                raise RuntimeError(error or "The spacy NER engine is not available.")
            results = run_spacy_ner_default([text], labels)
        else:
            response = await sidecar_post(
                self.context,
                "/ner",
                json={
                    "texts": [text],
                    "labels": labels,
                    "threshold": threshold,
                    "engine": self.engine,
                },
                op="ner",
            )
            results = response.get("results") or [[]]
        return canonicalize_entities(results[0] if results else [])
