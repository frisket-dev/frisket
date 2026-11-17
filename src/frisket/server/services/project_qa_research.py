"""Research policy around the existing Ask agent and action hosts."""

from __future__ import annotations

import hashlib
import json
import uuid
import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any
from dataclasses import asdict

from frisket.ai.llm.pricing import cost_of, estimate_tokens
from frisket.ai.llm.types import LLMRequest, LLMResponse
from frisket.ai.research.search import SearchService, SearchResponse
from frisket.engine.store.project_qa_research import (
    ProjectQAResearchBudgetExceeded,
    ProjectQAResearchConflictError,
    ProjectQAResearchStore,
    ProjectQAResearchTurnLimitReached,
    ProjectQAResearchUnknownCost,
)
from frisket.execution.pricing_policy import usd_to_micros
from frisket.server.thread_worker import await_thread_worker


def model_actual_cost(response: LLMResponse) -> float | None:
    """Return the cost this research budget actually incurred."""

    if response.cached:
        return 0.0
    if response.actual_accounting is not None:
        return response.actual_accounting.cost
    return response.cost


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
        self._approval_gate = asyncio.Lock()
        self._resume_lock = asyncio.Lock()
        self._pending: tuple[dict[str, Any], asyncio.Future[str]] | None = None
        self._closed = False
        self._model_operation: ContextVar[tuple[str, str] | None] = ContextVar(
            "research_model_operation", default=None
        )

    def _require_open(self) -> None:
        if self._closed:
            raise ProjectQAResearchConflictError("This research run has stopped.")

    def close(self) -> None:
        """Revoke the live continuation without losing returned usage."""
        self._closed = True
        if self._pending is not None:
            self._pending[1].cancel()

    async def check_authority(self) -> None:
        # This loop calls the decision primitive, not pause(), so a skills
        # change never recursively acquires the approval gate.
        while True:
            self._require_open()
            await await_thread_worker(self.store.assert_running, self.id)
            try:
                await await_thread_worker(self.authorize)
                self._require_open()
                return
            except ResearchSkillsUnavailable as exc:
                await self._await_decision({"kind": "skills", "message": str(exc)})

    async def _still_required(self, approval: dict[str, Any]) -> bool:
        kind = approval.get("kind")
        if kind == "skills":
            try:
                await await_thread_worker(self.authorize)
            except ResearchSkillsUnavailable:
                return True
            return False
        if kind == "budget":
            budget = await await_thread_worker(self.store.budget_summary, self.id)
            return budget["remaining_micros"] < approval["next_estimate_micros"]
        if kind == "turn_limit":
            current = await await_thread_worker(self.store.get, self.id)
            return (
                current["max_turns"] is not None
                and current["turn_count"] >= current["max_turns"]
            )
        return True

    async def _await_decision(
        self,
        approval: dict[str, Any],
        *,
        recheck: Callable[[], Awaitable[bool]] | None = None,
    ) -> str:
        async with self._approval_gate:
            self._require_open()
            await await_thread_worker(self.store.assert_running, self.id)
            # Another decision may have changed the budget, skill availability,
            # or action policy while this caller waited. Never publish stale work.
            if recheck is not None and not await recheck():
                return "continue"
            if not await self._still_required(approval):
                return "continue"
            pending = {
                **approval,
                "operation_id": approval.get("operation_id")
                or f"approval_{uuid.uuid4().hex}",
                "id": uuid.uuid4().hex,
            }
            decision = asyncio.get_running_loop().create_future()
            # Install the waiter before SQLite publication can become visible.
            self._pending = (pending, decision)
            try:
                await await_thread_worker(self.store.publish_approval, self.id, pending)
                return await decision
            finally:
                decision.cancel()
                if self._pending is not None and self._pending[1] is decision:
                    self._pending = None

    async def pause(
        self,
        approval: dict[str, Any],
        *,
        recheck: Callable[[], Awaitable[bool]] | None = None,
    ) -> str:
        """One visible, operation-bound decision, independent of the browser.

        A recheck only assesses whether this exact action still needs approval;
        it must not request another pause. Authority checks run after releasing
        the gate because a disabled skill can require a separate decision.
        """
        decision = await self._await_decision(approval, recheck=recheck)
        await self.check_authority()
        return decision

    async def resume(
        self,
        *,
        approval_id: str,
        expected_revision: int,
        decision: str,
        actor: str | None,
        authorize: Callable[[], None],
        context: Any,
        **preferences: Any,
    ) -> dict[str, Any]:
        """Consume durable permission before waking exactly its live waiter."""
        async with self._resume_lock:
            self._require_open()
            pending = self._pending
            if pending is None or pending[0]["id"] != approval_id or pending[1].done():
                raise ProjectQAResearchConflictError(
                    "This approval is no longer pending."
                )

            def consume():
                authorize()
                return self.store.resume(
                    self.id,
                    approval_id=approval_id,
                    expected_revision=expected_revision,
                    decision=decision,
                    actor=actor,
                    **preferences,
                )

            # HTTP disconnect must not commit a consumption and abandon the
            # Future. Drain this small command and its wake before propagating it.
            async def commit_and_wake():
                current = await await_thread_worker(consume)
                self.authorize = authorize
                self.context = context
                if not pending[1].done():
                    pending[1].set_result(decision)
                return current

            command = asyncio.create_task(commit_and_wake())
            try:
                return await asyncio.shield(command)
            except asyncio.CancelledError:
                await command
                raise

    async def admit(
        self,
        operation_id: str,
        identity: str,
        kind: str,
        estimate_micros: int | None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            while True:
                await self.check_authority()
                try:
                    async with self._approval_gate:
                        self._require_open()
                        # Gate acquisition may have waited on a different
                        # operation. Revalidate current permission before spend,
                        # but release the gate before a skills pause.
                        await await_thread_worker(self.authorize)
                        return await await_thread_worker(
                            self.store.admit_operation,
                            self.id,
                            operation_id=operation_id,
                            payload_identity=identity,
                            operation_kind=kind,
                            estimate_micros=estimate_micros,
                            metadata=metadata,
                        )
                except ResearchSkillsUnavailable:
                    continue
        except ProjectQAResearchUnknownCost as exc:
            raise ResearchPaused(
                {
                    "kind": "unknown_cost",
                    "operation": kind,
                    "operation_id": operation_id,
                    "payload_identity": identity,
                }
            ) from exc
        except ProjectQAResearchBudgetExceeded as exc:
            budget = await await_thread_worker(self.store.budget_summary, self.id)
            raise ResearchPaused(
                {
                    "kind": "budget",
                    "operation_id": operation_id,
                    "payload_identity": identity,
                    **budget,
                    "next_estimate_micros": exc.needed_micros,
                }
            ) from exc
        except ProjectQAResearchTurnLimitReached as exc:
            current = await await_thread_worker(self.store.get, self.id)
            raise ResearchPaused(
                {
                    "kind": "turn_limit",
                    "operation_id": operation_id,
                    "payload_identity": identity,
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
        actual_cost = model_actual_cost(response)
        if actual_cost is None:
            # Preserve the commitment when actual cost is unavailable. Continuing
            # with invented zero-cost usage would silently replenish the budget.
            await self.pause(
                {
                    "kind": "unknown_cost",
                    "operation": "model",
                    "operation_id": operation[0],
                }
            )
            return
        await await_thread_worker(
            self.store.settle_operation,
            self.id,
            operation_id=operation[0],
            payload_identity=operation[1],
            actual_micros=usd_to_micros(actual_cost),
        )

    async def search(
        self,
        service: SearchService,
        query: str,
        *,
        search: Callable[..., Awaitable[SearchResponse]],
    ) -> SearchResponse:
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
        result = await search(query, quote=quote)
        if result.usage.provider_cost_usd is None:
            await self.pause(
                {
                    "kind": "unknown_cost",
                    "operation": "search",
                    "operation_id": operation_id,
                }
            )
        else:
            await await_thread_worker(
                self.store.settle_operation,
                self.id,
                operation_id=operation_id,
                payload_identity=identity,
                actual_micros=usd_to_micros(result.usage.provider_cost_usd),
            )
        return result

    async def checkpoint(self, messages: list[Any]) -> dict[str, Any]:
        from pydantic_ai.messages import ModelMessagesTypeAdapter

        serialized = json.loads(ModelMessagesTypeAdapter.dump_json(messages))
        return await await_thread_worker(self.store.checkpoint, self.id, serialized)
