"""Invocation-bound access to the existing bounded web-research loop."""

from __future__ import annotations

import asyncio
import copy

from frisket.actions.research_types import ResearchAnswer
from frisket.actions.types import RowError
from frisket.ai.llm import LLMError
from frisket.ai.research.row_answer import fetch_page, run_research, search_web
from frisket.engine.executor.agent_model_calls import (
    AccountedAgentRouter,
    require_agent_attempt,
)
from frisket.engine.executor.visual_cuts_read import _settle
from frisket.ops.base import RecipeInvocationHalt


class AdmittedResearcher:
    def __init__(self, ctx, *, model: str):
        self._ctx, self._model = ctx, model
        self._started = self._closed = False
        self._tasks = set()
        self._rows = set()
        self.calls_by_row = {}
        self.accounting_by_row = {}

    async def start(self, *, expected_rows):
        if self._started or self._closed:
            raise RuntimeError("Research invocation is already started or closed")
        if self._ctx.extras.get("preview"):
            raise RecipeInvocationHalt(
                "preview_unsupported", "Web research requires an admitted run"
            )
        if self._ctx.project.effective_network_policy() == "off":
            raise RecipeInvocationHalt(
                "network_disabled", "Web research requires network access"
            )
        require_agent_attempt(self._ctx)
        self._started = True

    async def aclose(self):
        self._closed = True
        for task in tuple(self._tasks):
            task.cancel()
        for task in tuple(self._tasks):
            await _settle(task)

    def bind_row(self, row, *, sheet_id, row_id, sources, ctx):
        if not self._started or self._closed:
            raise RuntimeError("Research requires an active invocation")
        return _BoundResearcher(self, row, row_id, ctx)


class _BoundResearcher:
    def __init__(self, owner, row, row_id, ctx):
        self._owner, self._row, self._row_id, self._ctx = owner, row, row_id, ctx

    async def answer(self, row, *, goal, context):
        owner = self._owner
        if owner._closed or row is not self._row or self._row_id in owner._rows:
            raise RowError(
                "invalid_input_ref", "Research requires its admitted row, once"
            )
        owner._rows.add(self._row_id)
        task = asyncio.create_task(self._answer(goal, copy.deepcopy(context)))
        owner._tasks.add(task)
        try:
            return await task
        finally:
            await _settle(task)
            owner._tasks.discard(task)

    async def _answer(self, goal, context):
        owner, ctx = self._owner, self._ctx
        router = AccountedAgentRouter(
            ctx.extras["router"], ctx, owner._model, owner.accounting_by_row
        )
        try:
            data, _ = await run_research(
                context,
                goal=goal,
                model_id=owner._model,
                router=router,
                recipe_version="1",
                search=search_web,
                fetch=lambda url: fetch_page(url, ctx.http),
            )
        except LLMError as exc:
            router.retain_unrecorded_error_calls(exc)
            raise
        except (asyncio.CancelledError, RecipeInvocationHalt):
            raise
        except Exception as exc:
            raise RowError(
                "research_failed", str(exc)[:500] or "Research failed"
            ) from exc
        if data.get("error"):
            raise RowError(
                data.get("error_code", "research_failed"), data["error"][:500]
            )
        result = ResearchAnswer(
            answer=data["answer"],
            sources=data.get("sources", []),
            unverified_memory=data.get("outcome") == "unverified_memory",
        )
        owner.calls_by_row[self._row_id] = [
            {
                "kind": "research_answer",
                "answer": result.answer,
                "sources": copy.deepcopy(result.sources),
                "unverified_memory": result.unverified_memory,
            }
        ]
        return result
