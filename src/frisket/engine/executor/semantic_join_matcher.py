"""Admitted semantic matching; provider facts precede cached vector publication."""

from __future__ import annotations

from typing import Any

from frisket.actions.semantic_join_types import SemanticJoinMatch
from frisket.actions.types import Outcome, SheetColumnRef
from frisket.ops.base import OpContext
from frisket.engine.executor.embedding_batches import (
    record_embedding_batch,
    refuse_missing_paid_vectors,
)

_UNBOUND_SOURCE = object()


class AdmittedSemanticMatcher:
    """One frozen target corpus and embedding route for a serialized row run."""

    def __init__(
        self,
        *,
        target: SheetColumnRef,
        values: tuple[tuple[int, Any], ...],
        backend: tuple[Any, str],
        estimated_model: str,
    ) -> None:
        from frisket.semantic import embedder_is_remote

        if backend[1] != estimated_model:
            raise ValueError("semantic join embedding backend changed after admission")
        if embedder_is_remote(backend[1]) and not estimated_model:
            raise ValueError("remote embedding was not admitted")
        self.target = target.model_copy(deep=True)
        self.values = tuple(
            (row_id, str(value).strip())
            for row_id, value in values
            if value not in (None, "")
        )
        if not self.values:
            raise ValueError("target column has no values to match against")
        self.backend = backend
        self._candidates: list[tuple[int, str, Any]] | None = None

    def bind_row(
        self, ctx: OpContext, *, expected_source: Any = _UNBOUND_SOURCE
    ) -> BoundSemanticMatcher:
        return BoundSemanticMatcher(self, ctx, expected_source)

    async def candidates(self, ctx: OpContext) -> list[tuple[int, str, Any]]:
        from frisket.semantic import _doc_vectors_async

        if self._candidates is None:
            embed, model_id = self.backend
            vectors = await _doc_vectors_async(
                ctx.project,
                [{"content": text} for _, text in self.values],
                embed,
                model_id,
                before_fresh_batch=lambda texts: refuse_missing_paid_vectors(
                    ctx, model_id, texts
                ),
                on_fresh_batch=lambda batch, texts: record_embedding_batch(
                    ctx, model_id, batch, texts
                ),
            )
            self._candidates = [
                (row_id, text, vector)
                for (row_id, text), vector in zip(self.values, vectors, strict=True)
            ]
        return self._candidates


class BoundSemanticMatcher:
    def __init__(
        self, admitted: AdmittedSemanticMatcher, ctx: OpContext, expected_source: Any
    ) -> None:
        self._admitted = admitted
        self._ctx = ctx
        self._issued: SemanticJoinMatch | None = None
        self._expected_source = expected_source
        self._evidence = None

    async def match(
        self,
        value: Any,
        *,
        target: SheetColumnRef,
        match_threshold: float = 0.70,
        confident_threshold: float = 0.85,
    ) -> SemanticJoinMatch:
        from frisket.semantic import _cosine, _doc_vectors_async

        if target != self._admitted.target:
            raise ValueError("semantic match target differs from the admitted column")
        if (
            self._expected_source is not _UNBOUND_SOURCE
            and value != self._expected_source
        ):
            raise ValueError(
                "semantic match value differs from the admitted source cell"
            )
        if not 0 <= match_threshold < confident_threshold <= 1:
            raise ValueError("thresholds must satisfy 0 <= match < confident <= 1")
        if self._issued is not None:
            raise ValueError("one semantic match is permitted per source row")
        if value in (None, ""):
            result = SemanticJoinMatch(
                match_value=Outcome.ok(None),
                match_score=Outcome.ok(None),
                matched_row_id=Outcome.ok(None),
            )
        else:
            candidates = await self._admitted.candidates(self._ctx)
            embed, model_id = self._admitted.backend
            query = (
                await _doc_vectors_async(
                    self._ctx.project,
                    [{"content": str(value).strip()}],
                    embed,
                    model_id,
                    before_fresh_batch=lambda texts: refuse_missing_paid_vectors(
                        self._ctx, model_id, texts
                    ),
                    on_fresh_batch=lambda batch, texts: record_embedding_batch(
                        self._ctx, model_id, batch, texts
                    ),
                )
            )[0]
            best_row, best_value, vector = max(
                candidates, key=lambda candidate: _cosine(query, candidate[2])
            )
            score = round(max(0.0, _cosine(query, vector)), 4)
            justification = None
            if score < match_threshold:
                justification = (
                    f"no match >= {match_threshold}: best candidate {best_value!r} "
                    f"(row {best_row}) scored {score}"
                )
                best_row = best_value = None
            elif score < confident_threshold:
                justification = (
                    f"gray-band match ({match_threshold}-{confident_threshold}): "
                    f"scored {score} against {best_value!r} — review before trusting"
                )
            result = SemanticJoinMatch(
                match_value=Outcome.ok(
                    best_value, confidence=score, justification=justification
                ),
                match_score=Outcome.ok(score, confidence=score),
                matched_row_id=Outcome.ok(best_row, confidence=score),
            )
        self._issued = result.model_copy(deep=True)
        self._evidence = {
            "match_threshold": float(match_threshold),
            "confident_threshold": float(confident_threshold),
            "model": self._admitted.backend[1],
        }
        return result

    def validate_output(self, result: SemanticJoinMatch) -> None:
        if self._issued is None or result != self._issued:
            raise ValueError("semantic join output must be the admitted match result")

    def evidence(self):
        if self._evidence is None:
            raise ValueError("semantic match was not executed")
        return dict(self._evidence)
