"""Host-owned web search, including admission and observed provider use."""

from __future__ import annotations

import asyncio
import uuid

from frisket.actions.research_types import SearchResult
from frisket.actions.types import RowError
from frisket.ai.research.search import (
    SearchProviderError,
    SearchQuote,
    SearchResponse,
    SearchService,
)
from frisket.ops.base import RecipeInvocationHalt

MAX_ATTEMPTS = 4


class _SearchUnavailable(Exception):
    def __init__(self, error: SearchProviderError) -> None:
        super().__init__(str(error))
        self.error = error


class AdmittedWebSearcher:
    def __init__(self, ctx, *, search_service: SearchService | None = None):
        self._ctx = ctx
        self._closed = False
        self._owner = self
        self._calls = set()
        self._service = (
            search_service
            or ctx.extras.get("search_service")
            or SearchService(preference="ddgs", effective_keys={})
        )
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
        bound = AdmittedWebSearcher(ctx, search_service=self._service)
        bound._owner = self
        return bound

    async def _call(self, query, max_results, attempt):
        from frisket.engine.executor.visual_cuts_read import _settle

        quote = self._service.quote(max_results=max_results)
        task = asyncio.create_task(
            self._service.search(query, max_results=max_results, quote=quote)
        )
        self._owner._calls.add(task)
        try:
            try:
                return await asyncio.shield(task)
            except SearchProviderError as exc:
                raise _SearchUnavailable(exc) from None
        finally:
            await _settle(task)
            self._owner._calls.discard(task)
            response = None
            error = None
            if not task.cancelled():
                try:
                    response = task.result()
                except SearchProviderError as exc:
                    error = exc
                except Exception:  # receipt records failure; original propagates
                    pass
            self._record(attempt, response=response, error=error, quote=quote)

    def _record(
        self,
        attempt: int,
        *,
        response: SearchResponse | None,
        error: SearchProviderError | None,
        quote: SearchQuote,
    ) -> None:
        ctx = self._ctx
        if ctx.project is None:
            # Explicit standalone `op try` has no project receipt.
            return
        from frisket.engine.store.receipts import ReceiptStore
        from frisket.execution.attempt import attempt_in_scope

        writer = attempt_in_scope(ctx.extras)
        if writer is None:
            raise RuntimeError("Web search requires its admitted output writer")
        provider = response.provider if response is not None else self._service.provider
        usage = response.usage if response is not None else None
        max_attempts = MAX_ATTEMPTS if provider == "ddgs" else 1
        evidence = {
            "kind": "web_search_call",
            "call_id": uuid.uuid4().hex,
            "row_id": ctx.extras["row_id"],
            "provider": provider,
            "service": usage.service if usage is not None else f"{provider}.search",
            "external_api": True,
            "attempt": attempt,
            "max_attempts_per_row": max_attempts,
            "succeeded": response is not None,
            "cost_actual": (
                usage.provider_cost_usd
                if usage is not None
                else (0.0 if provider == "ddgs" else None)
            ),
            "cost_source": usage.cost_source if usage is not None else "unknown",
        }
        if usage is not None and usage.units:
            evidence["units"] = {unit.name: unit.quantity for unit in usage.units}
        if usage is not None:
            evidence["provider_reported_cost_usd"] = usage.provider_reported_cost_usd
        evidence.update(
            {
                "pricing_key": quote.pricing_key,
                "pricing_unit": quote.unit,
                "unit_price_usd": quote.unit_price_usd,
                "estimated_cost_usd": quote.estimated_cost_usd,
            }
        )
        if error is not None:
            evidence["error_code"] = error.code
        ReceiptStore(ctx.project)._record_writer_evidence(
            evidence,
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
        if self._service.provider != "ddgs" and max_results > 10:
            raise RowError(
                "invalid_params", "Paid search result limit must be between 1 and 10"
            )
        max_attempts = MAX_ATTEMPTS if self._service.provider == "ddgs" else 1
        last_error: SearchProviderError | None = None
        for attempt in range(1, max_attempts + 1):
            self._check_active()
            try:
                results = await self._call(query, max_results, attempt)
            except _SearchUnavailable as exc:
                last_error = exc.error
                if self._service.provider == "ddgs" or attempt < max_attempts:
                    await asyncio.sleep(1.5 * attempt)
            else:
                return [
                    SearchResult(
                        title=item.title,
                        url=item.url,
                        snippet=item.excerpt,
                    )
                    for item in results.results
                ]
        if last_error is not None and self._service.provider != "ddgs":
            raise RowError(f"search_{last_error.code}", str(last_error)) from None
        raise RowError("search_failed", "Search failed after bounded retries") from None


def search_provider_use(recorded, facts):
    """Project provider use only from calls recorded by the admitted adapter."""
    if not recorded:
        return []
    calls = [item.ref for item in recorded]
    costs = [call.get("cost_actual") for call in calls]
    cost_actual = None if any(cost is None for cost in costs) else sum(costs)
    units: dict[str, int | float] = {}
    for call in calls:
        for name, quantity in call.get("units", {}).items():
            units[name] = units.get(name, 0) + quantity
    provider_use = {
        "provider": calls[0]["provider"],
        "service": calls[0]["service"],
        "external_api": True,
        "selected_row_count": facts.total_rows,
        "successful_row_count": max(0, facts.completed_rows - facts.failed_rows),
        "failed_row_count": facts.failed_rows,
        "max_attempts_per_row": max(call["max_attempts_per_row"] for call in calls),
        "operation_call_count": len(calls),
        "cost_actual": cost_actual,
    }
    if units:
        provider_use["units"] = units
    if calls[0].get("pricing_key") is not None:
        provider_use.update(
            {
                "pricing_key": calls[0]["pricing_key"],
                "pricing_unit": calls[0]["pricing_unit"],
                "unit_price_usd": calls[0]["unit_price_usd"],
            }
        )
    return [provider_use]
