"""Research policy around the existing Ask agent and action hosts."""

from __future__ import annotations

import hashlib
import json
import uuid
import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any
from dataclasses import asdict

from frisket.ai.llm.pricing import cost_of, estimate_tokens
from frisket.ai.llm.types import LLMRequest, LLMResponse
from frisket.ai.research.search import SearchService, SearchResponse
from frisket.engine.store.project_qa_research import (
    ProjectQAResearchBudgetExceeded,
    ProjectQAResearchStore,
    ProjectQAResearchTurnLimitReached,
    ProjectQAResearchUnknownCost,
)
from frisket.execution.pricing_policy import usd_to_micros
from frisket.server.thread_worker import await_thread_worker


class ResearchPaused(Exception):
    """An ordinary pause needing user input, not an agent/provider failure."""

    def __init__(self, approval: dict[str, Any]):
        super().__init__(str(approval["kind"]))
        self.approval = approval


class ResearchSkillsUnavailable(ValueError):
    """An admitted instruction package was disabled or removed by its manager."""


def payload_identity(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def model_estimate_micros(request: LLMRequest) -> int | None:
    """Use the existing price catalog, including tool-schema input tokens.

    This is an estimate, not a provider-enforced charge ceiling. Settlement
    records actual usage, and later admissions see any overrun.
    """
    inputs = json.dumps(
        {
            "messages": request.messages,
            "tools": request.tools,
            "schema": request.schema,
        },
        ensure_ascii=False,
    )
    price = cost_of(request.model, estimate_tokens(inputs), request.max_tokens)
    return None if price is None else usd_to_micros(price)


class ResearchSession:
    """One live owner; SQLite owns permission revisions and money admission."""

    def __init__(
        self,
        store: ProjectQAResearchStore,
        research_id: str,
        *,
        authorize: Callable[[], None],
        context: Any = None,
    ) -> None:
        self.store = store
        self.id = research_id
        self.authorize = authorize
        self.context = context
        self._wake = asyncio.Event()
        self._decision: str | None = None
        self._model_operation: ContextVar[tuple[str, str] | None] = ContextVar(
            "research_model_operation", default=None
        )

    async def check_authority(self) -> None:
        try:
            await await_thread_worker(self.authorize)
        except ResearchSkillsUnavailable as exc:
            await self.pause({"kind": "skills", "message": str(exc)})

    async def pause(self, approval: dict[str, Any]) -> str:
        """Wait in the live owner. A browser disconnect does not end this task."""
        self._wake.clear()
        self._decision = None
        pending = {**approval, "id": uuid.uuid4().hex}

        def publish():
            current = self.store.get(self.id)
            return self.store.update(
                self.id,
                expected_revision=current["revision"],
                state="paused",
                pending_approval=pending,
            )

        await await_thread_worker(publish)
        await self._wake.wait()
        await self.check_authority()
        return self._decision or "continue"

    def wake(self, decision: str) -> None:
        """Called only after the resume endpoint commits its conditional update."""
        self._decision = decision
        self._wake.set()

    async def admit(
        self,
        operation_id: str,
        identity: str,
        kind: str,
        estimate_micros: int | None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.check_authority()
        try:
            return await await_thread_worker(
                self.store.admit_operation,
                self.id,
                operation_id=operation_id,
                payload_identity=identity,
                operation_kind=kind,
                estimate_micros=estimate_micros,
                metadata=metadata,
            )
        except ProjectQAResearchUnknownCost as exc:
            raise ResearchPaused({"kind": "unknown_cost", "operation": kind}) from exc
        except ProjectQAResearchBudgetExceeded as exc:
            budget = await await_thread_worker(self.store.budget_summary, self.id)
            raise ResearchPaused(
                {
                    "kind": "budget",
                    **budget,
                    "next_estimate_micros": exc.needed_micros,
                }
            ) from exc
        except ProjectQAResearchTurnLimitReached as exc:
            current = await await_thread_worker(self.store.get, self.id)
            raise ResearchPaused(
                {
                    "kind": "turn_limit",
                    "turns_used": current["turn_count"],
                    "max_turns": current["max_turns"],
                }
            ) from exc

    async def before_model(self, request: LLMRequest) -> None:
        operation_id = f"model_{uuid.uuid4().hex}"
        identity = payload_identity(
            {
                "model": request.model,
                "messages": request.messages,
                "tools": request.tools,
                "schema": request.schema,
                "max_tokens": request.max_tokens,
            }
        )
        while True:
            try:
                await self.admit(
                    operation_id, identity, "model", model_estimate_micros(request)
                )
                break
            except ResearchPaused as pause:
                await self.pause(pause.approval)
        self._model_operation.set((operation_id, identity))

    async def after_model(self, response: LLMResponse) -> None:
        operation = self._model_operation.get()
        if operation is None:
            raise RuntimeError("Research model usage has no admitted request")
        if response.cost is None:
            # Preserve the commitment when actual cost is unavailable. Continuing
            # with invented zero-cost usage would silently replenish the budget.
            await self.pause({"kind": "unknown_cost", "operation": "model"})
            return
        await await_thread_worker(
            self.store.settle_operation,
            self.id,
            operation_id=operation[0],
            payload_identity=operation[1],
            actual_micros=usd_to_micros(response.cost),
        )

    async def search(self, service: SearchService, query: str) -> SearchResponse:
        if not query.strip() or len(query) > 500:
            raise ValueError("Enter a search query up to 500 characters.")
        quote = service.quote()
        identity = payload_identity({"query": query, "quote": asdict(quote)})
        operation_id = f"search_{uuid.uuid4().hex}"
        while True:
            try:
                await self.admit(
                    operation_id,
                    identity,
                    "search",
                    usd_to_micros(quote.estimated_cost_usd)
                    if quote.estimated_cost_usd is not None
                    else None,
                    metadata={"provider": quote.provider, "quote": asdict(quote)},
                )
                break
            except ResearchPaused as pause:
                await self.pause(pause.approval)
        result = await service.search(query, quote=quote)
        if result.usage.provider_cost_usd is None:
            await self.pause({"kind": "unknown_cost", "operation": "search"})
        else:
            await await_thread_worker(
                self.store.settle_operation,
                self.id,
                operation_id=operation_id,
                payload_identity=identity,
                actual_micros=usd_to_micros(result.usage.provider_cost_usd),
            )
        return result

    async def checkpoint(self, messages: list[Any], **changes: Any) -> dict[str, Any]:
        from pydantic_ai.messages import ModelMessagesTypeAdapter

        serialized = json.loads(ModelMessagesTypeAdapter.dump_json(messages))

        def save():
            current = self.store.get(self.id)
            return self.store.update(
                self.id,
                expected_revision=current["revision"],
                saved_messages=serialized,
                **changes,
            )

        return await await_thread_worker(save)
