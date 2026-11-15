"""Local NER operations bound to the host-admitted engine and transport."""

from typing import Any, Literal

from frisket.execution.dispatch_control import dispatch_for_context
from frisket.execution.purchases import (
    PurchaseObservation,
    PurchaseRequest,
    execute_purchase_sync,
    policy_for_context,
)

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
        dispatch_for_context(self.context).check()
        if self.engine == "spacy":
            from frisket.ops.spacy_ner import run_spacy_ner_default, spacy_available

            available, error = spacy_available()
            if not available:
                raise RuntimeError(error or "The spacy NER engine is not available.")
            results = execute_purchase_sync(
                PurchaseRequest(
                    provider="local",
                    capability="ner",
                    engine="spacy",
                    credential_source="none",
                    prepared={"labels": labels},
                    context=self.context,
                    quantity={"input_count": 1},
                ),
                policy=policy_for_context(self.context),
                dispatch=dispatch_for_context(self.context),
                invoke=lambda: run_spacy_ner_default([text], labels),
                observe=lambda result: PurchaseObservation(
                    result, {"input_count": 1}, 0
                ),
            )
        else:
            response = await sidecar_post(
                self.context,
                "/ner",
                dispatch=dispatch_for_context(self.context),
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
