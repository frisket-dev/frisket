"""Invocation-scoped local NER capability over the existing extraction adapters."""

from __future__ import annotations

import asyncio
from typing import Any

from frisket.actions.types import Row, RowError
from frisket.ops.ner_local import LocalNerExtractor


class AdmittedNerExtractor:
    def __init__(self, ctx, *, engine: str):
        if engine not in ("spacy", "gliner"):
            raise ValueError("unsupported local NER engine")
        self._ctx = ctx
        self._engine = engine
        self._closed = False
        self._tasks: set[asyncio.Task] = set()

    def _check_active(self, ctx) -> None:
        if self._closed:
            raise RuntimeError("NER invocation is closed")
        cancelled = ctx.extras.get("cancelled")
        if callable(cancelled) and cancelled():
            raise asyncio.CancelledError

    def bind_row(self, row: Row, *, sheet_id, row_id, sources, ctx):
        self._check_active(ctx)
        if type(sheet_id) is not int or type(row_id) is not int or row_id <= 0:
            raise RowError("invalid_input_ref", "NER requires its admitted row")
        return _BoundNerExtractor(self, ctx)

    async def aclose(self) -> None:
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class _BoundNerExtractor:
    def __init__(self, owner: AdmittedNerExtractor, ctx):
        self._owner = owner
        self._ctx = ctx

    async def extract(
        self, text: str, *, labels: list[str], threshold: float
    ) -> list[dict[str, Any]]:
        owner = self._owner
        owner._check_active(self._ctx)
        extractor = LocalNerExtractor(owner._engine, self._ctx)
        task = asyncio.create_task(
            extractor.extract(text, labels=labels, threshold=threshold)
        )
        owner._tasks.add(task)
        try:
            result = await task
            owner._check_active(self._ctx)
            return result
        finally:
            owner._tasks.discard(task)
