"""Invocation-bound access to the existing bounded web-research loop."""

from __future__ import annotations

import asyncio
import copy
import json

from frisket.actions.extract.grounding import complete_extraction
from frisket.actions.extraction_types import extraction_output_fields
from frisket.actions.research_types import QuickSearchAnswer, ResearchAnswer
from frisket.actions.types import DynamicOutput, Outcome, RowError
from frisket.ai.llm import LLMError
from frisket.ai.llm.structured import StructuredCompleter, StructuredRequest
from frisket.ai.research.quick_search import run_quick_search
from frisket.ai.research.recorder import ResearchRecorder, research_secret_values
from frisket.ai.research.row_answer import (
    fetch_page_result,
    run_research,
    search_observation,
)
from frisket.engine.executor.agent_model_calls import (
    AccountedAgentRouter,
    require_agent_attempt,
)
from frisket.engine.executor.visual_cuts_read import _settle
from frisket.ops.base import RecipeInvocationHalt
from frisket.ops.extraction import extract_response_schema, render_extract_messages
from frisket.ops.integrations.hosted_error import HostedEngineError


class AdmittedResearcher:
    def __init__(self, ctx, *, model: str):
        from frisket.engine.executor.web_search_read import AdmittedWebSearcher

        self._ctx, self._model = ctx, model
        self._searcher = AdmittedWebSearcher(ctx)
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
                "preview_unsupported", "Agent Research requires an admitted run"
            )
        if self._ctx.project.effective_network_policy() == "off":
            raise RecipeInvocationHalt(
                "network_disabled", "Agent Research requires network access"
            )
        require_agent_attempt(self._ctx)
        self._started = True

    async def aclose(self):
        self._closed = True
        for task in tuple(self._tasks):
            task.cancel()
        for task in tuple(self._tasks):
            await _settle(task)
        await self._searcher.aclose()

    def bind_row(self, row, *, sheet_id, row_id, sources, ctx):
        if not self._started or self._closed:
            raise RuntimeError("Research requires an active invocation")
        return _BoundResearcher(
            self,
            row,
            row_id,
            ctx,
            self._searcher.bind_row(
                row,
                sheet_id=sheet_id,
                row_id=row_id,
                sources=sources,
                ctx=ctx,
            ),
        )


class _BoundResearcher:
    def __init__(self, owner, row, row_id, ctx, searcher):
        self._owner, self._row, self._row_id, self._ctx = owner, row, row_id, ctx
        self._searcher = searcher

    async def answer(self, row, *, goal, context):
        return await self._run(row, goal=goal, context=context)

    async def extract(
        self,
        row,
        *,
        goal,
        context,
        fields,
        include_confidence=False,
        include_sources=True,
    ):
        return await self._run(
            row,
            goal=goal,
            context=context,
            fields=copy.deepcopy(fields),
            include_confidence=include_confidence,
            include_sources=include_sources,
        )

    async def quick_search(self, row, *, query, max_results):
        return await self._run(
            row,
            goal=query,
            context={},
            quick_max_results=max_results,
        )

    async def _run(self, row, *, goal, context, **options):
        owner = self._owner
        if owner._closed or row is not self._row or self._row_id in owner._rows:
            raise RowError(
                "invalid_input_ref", "Research requires its admitted row, once"
            )
        owner._rows.add(self._row_id)
        task = asyncio.create_task(
            self._answer(goal, copy.deepcopy(context), **options)
        )
        owner._tasks.add(task)
        try:
            return await task
        finally:
            await _settle(task)
            owner._tasks.discard(task)

    async def _answer(
        self,
        goal,
        context,
        *,
        fields=None,
        include_confidence=False,
        include_sources=True,
        quick_max_results=None,
    ):
        owner, ctx = self._owner, self._ctx
        router = AccountedAgentRouter(
            ctx.extras["router"], ctx, owner._model, owner.accounting_by_row
        )
        search_secrets = model_secrets = ()
        try:
            search_secrets = research_secret_values(self._searcher.secret_values())
        except Exception:
            pass
        try:
            model_secrets = research_secret_values(
                router.secret_values_for_model(owner._model)
            )
        except Exception:
            pass
        recorder = ResearchRecorder(secret_values=search_secrets + model_secrets)
        try:
            router.add_secret_values(search_secrets)
        except Exception:
            pass  # Diagnostic handoff must never change execution.
        try:
            if quick_max_results is not None:
                data = await run_quick_search(
                    query=goal,
                    model_id=owner._model,
                    router=router,
                    search=lambda query: self._searcher.search_response(
                        query, max_results=quick_max_results
                    ),
                    recorder=recorder,
                    secret_values=search_secrets + model_secrets,
                )
                return QuickSearchAnswer.model_validate(data)
            research_goal = goal
            if fields is not None:
                research_goal += (
                    "\n\nCollect evidence for every requested field, including its "
                    "constraints. Explicitly identify fields you cannot establish. "
                    "Requested fields:\n"
                    + json.dumps(
                        [field.model_dump(exclude_none=True) for field in fields],
                        ensure_ascii=False,
                    )
                )
            data, _ = await run_research(
                context,
                goal=research_goal,
                model_id=owner._model,
                router=router,
                recipe_version="1",
                search=self._search,
                fetch=lambda url: fetch_page_result(url, ctx.http),
                recorder=recorder,
                secret_values=search_secrets,
            )
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
            if fields is None:
                return result
            output_fields = [
                {"name": key, "schema": dict(field.schema)}
                for key, field in extraction_output_fields(
                    fields, include_confidence=include_confidence
                ).items()
            ]
            messages = render_extract_messages(
                context,
                output_fields,
                instruction=goal,
                grounding_enabled=False,
            )
            messages[-1]["content"].append(
                {
                    "type": "text",
                    "text": "\nResearch findings:\n"
                    + result.answer
                    + "\nSources consulted:\n"
                    + json.dumps(result.sources),
                }
            )
            strict = await StructuredCompleter(router).complete(
                StructuredRequest(
                    model=owner._model,
                    messages=messages,
                    schema=extract_response_schema(
                        output_fields,
                        grounding_enabled=False,
                        nullable=True,
                    ),
                    repair_attempts=1,
                ),
                recipe_version="1",
            )
            output = complete_extraction(
                fields=fields,
                include_confidence=include_confidence,
                grounding=None,
                response=DynamicOutput(strict.data),
            ).output
            if include_sources:
                output.root["sources"] = Outcome.ok(result.sources)
            return output
        except LLMError as exc:
            router.retain_unrecorded_error_calls(exc)
            raise
        except (
            asyncio.CancelledError,
            RecipeInvocationHalt,
            RowError,
            HostedEngineError,
        ):
            raise
        except Exception as exc:
            raise RowError(
                "research_failed", str(exc)[:500] or "Research failed"
            ) from exc
        finally:
            try:
                router.set_research(recorder.snapshot())
            except Exception:
                pass  # Trace attachment is best-effort, including on failure.

    async def _search(self, query):
        response = await self._searcher.search_response(query, max_results=6)
        return search_observation(query, response)
