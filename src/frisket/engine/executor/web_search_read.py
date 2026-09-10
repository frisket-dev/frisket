"""Host-owned DDGS search, including bounded retries and observed provider use."""

from __future__ import annotations

import asyncio
import uuid

from frisket.actions.research_types import SearchResult
from frisket.actions.types import RowError
from frisket.ops.base import RecipeInvocationHalt

MAX_ATTEMPTS = 4


class _SearchUnavailable(Exception):
    pass


class AdmittedWebSearcher:
    def __init__(self, ctx):
        self._ctx = ctx
        self._closed = False
        self._owner = self
        self._calls = set()
        self._check_active()

    def _check_active(self):
        if self._closed or self._owner._closed:
            raise RuntimeError("Web search invocation is closed")
        if self._ctx.extras.get("preview"):
            raise RecipeInvocationHalt(
                "preview_unsupported", "Web search requires a run"
            )
        project = self._ctx.project
        if project is not None and project.effective_network_policy() == "off":
            raise RecipeInvocationHalt(
                "network_disabled", "Web search requires network access"
            )
        cancelled = self._ctx.extras.get("cancelled")
        if callable(cancelled) and cancelled():
            raise asyncio.CancelledError

    async def aclose(self):
        self._closed = True
        from frisket.engine.executor.visual_cuts_read import _settle

        for task in tuple(self._calls):
            await _settle(task)

    def bind_row(self, row, *, sheet_id, row_id, sources, ctx):
        self._check_active()
        bound = AdmittedWebSearcher(ctx)
        bound._owner = self
        return bound

    async def _call(self, query, max_results, attempt):
        from ddgs import DDGS
        from frisket.engine.executor.visual_cuts_read import _settle

        task = asyncio.create_task(
            asyncio.to_thread(lambda: DDGS().text(query, max_results=max_results))
        )
        self._owner._calls.add(task)
        try:
            try:
                return await asyncio.shield(task)
            except Exception:  # noqa: BLE001 — DDGS raises plain exceptions
                raise _SearchUnavailable from None
        finally:
            await _settle(task)
            self._owner._calls.discard(task)
            self._record(attempt, task.exception() is None)

    def _record(self, attempt, succeeded):
        ctx = self._ctx
        if ctx.project is None:
            # Explicit standalone `op try` has no project receipt.
            return
        from frisket.engine.store.receipts import ReceiptStore
        from frisket.execution.attempt import attempt_in_scope

        writer = attempt_in_scope(ctx.extras)
        if writer is None:
            raise RuntimeError("Web search requires its admitted output writer")
        ReceiptStore(ctx.project)._record_writer_evidence(
            {
                "kind": "web_search_call",
                "call_id": uuid.uuid4().hex,
                "row_id": ctx.extras["row_id"],
                "provider": "ddgs",
                "service": "ddgs.text",
                "external_api": True,
                "attempt": attempt,
                "max_attempts_per_row": MAX_ATTEMPTS,
                "succeeded": succeeded,
                "cost_actual": 0.0,
            },
            run_id=ctx.extras["run_id"],
            writer_attempt_id=writer.attempt_id,
            claim_token=ctx.extras["claim_token"],
        )

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        self._check_active()
        if self._ctx.project is not None:
            from frisket.execution.attempt import attempt_in_scope

            if attempt_in_scope(self._ctx.extras) is None:
                raise RuntimeError("Web search requires its admitted output writer")
        if not isinstance(query, str) or not query.strip():
            raise RowError("invalid_params", "Search query must be non-empty")
        if type(max_results) is not int or not 1 <= max_results <= 20:
            raise RowError(
                "invalid_params", "Search result limit must be between 1 and 20"
            )
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._check_active()
            try:
                results = await self._call(query, max_results, attempt)
            except _SearchUnavailable:
                await asyncio.sleep(1.5 * attempt)
            else:
                return [
                    SearchResult(
                        title=item.get("title"),
                        url=item.get("href"),
                        snippet=item.get("body"),
                    )
                    for item in (results or [])
                ]
        raise RowError("search_failed", "Search failed after bounded retries") from None


def search_provider_use(recorded, facts):
    """Project provider use only from calls recorded by the admitted adapter."""
    if not recorded:
        return []
    calls = [item.ref for item in recorded]
    return [
        {
            "provider": calls[0]["provider"],
            "service": calls[0]["service"],
            "external_api": True,
            "selected_row_count": facts.total_rows,
            "successful_row_count": max(0, facts.completed_rows - facts.failed_rows),
            "failed_row_count": facts.failed_rows,
            "max_attempts_per_row": max(call["max_attempts_per_row"] for call in calls),
            "operation_call_count": len(calls),
            "cost_actual": sum(call["cost_actual"] for call in calls),
        }
    ]
