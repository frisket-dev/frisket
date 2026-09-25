"""Bounded Project Ask investigation over persisted, scope-safe read tools."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.usage import UsageLimitExceeded, UsageLimits

from frisket.ai.llm.structured import FrisketRouterModel
from frisket.ai.llm.types import LLMRequest, LLMResponse, provider_from_model_id
from frisket.ai.models.accounting import wire_accounting_meta
from frisket.authoring.copilot import default_copilot_model
from frisket.engine.runner.validation import assert_provider_spend_cap
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAConflictError, ProjectQAStore
from frisket.server.services.project_qa_tools import ProjectQATools


MAX_MODEL_REQUESTS = 8
MAX_TOOL_CALLS = 24
RECENT_HISTORY_EVENTS = 12
MAX_HISTORY_CHARS = 8_000


class ProjectQAAnswer(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    citation_ids: list[str] = Field(default_factory=list, max_length=100)


async def run_turn(
    project: Project, router: Any, turn: dict[str, Any], store: ProjectQAStore
) -> dict[str, Any]:
    """Investigate one admitted turn; the caller owns terminalization."""

    tools = ProjectQATools(project, turn, store)
    model_id = turn["model"] or default_copilot_model(router)
    store.append_event(
        turn["id"],
        kind="assistant",
        payload={"text": "Inspecting the selected project material."},
    )

    async def before_request(request: LLMRequest) -> None:
        provider = provider_from_model_id(request.model)
        if router.credential_source_for(provider) == "project_key":
            assert_provider_spend_cap(project, provider)

    async def on_response(response: LLMResponse) -> None:
        accounting = wire_accounting_meta(model_id, [response])
        [call] = accounting["model_calls"]
        call_id = str(call["id"])
        store.record_usage(
            turn["id"],
            call_id=call_id,
            calls=[call],
            payload={
                "call_id": call_id,
                "tokens_in": accounting["tokens_in"],
                "tokens_out": accounting["tokens_out"],
                "cost": accounting["cost"],
            },
        )

    model = FrisketRouterModel(
        router,
        model_id,
        recipe_version="project_qa.v1",
        before_request=before_request,
        on_response=on_response,
    )
    citation_repairs = 0

    def recent_history() -> str:
        events = store.recent_events(turn["thread_id"], limit=RECENT_HISTORY_EVENTS)[
            "events"
        ]
        summary = [
            {"kind": event["kind"], "payload": event["payload"]} for event in events
        ]
        return json.dumps(summary, ensure_ascii=False)[:MAX_HISTORY_CHARS]

    def progress(tool: str, state: str, **extra: Any) -> None:
        try:
            store.append_event(
                turn["id"],
                kind="tool_started" if state == "started" else "tool_completed",
                payload={"tool": tool, "state": state, **extra},
            )
        except ProjectQAConflictError:
            # A concurrent Stop has already frozen content. The outer owner
            # observes and terminalizes that state; this tool must not revive it.
            return

    async def inspect_sheets() -> dict[str, Any]:
        progress("inspect_sheets", "started")
        try:
            observed = tools.inspect_sheets()
        except ValueError as error:
            progress("inspect_sheets", "completed", error="unavailable")
            raise ModelRetry(
                "inspect_sheets was unavailable; choose a permitted read."
            ) from error
        progress("inspect_sheets", "completed", sheets=len(observed["sheets"]))
        return observed

    async def read_rows(
        sheet_id: int,
        row_ids: list[int] | None = None,
        column_ids: list[int] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        progress("read_rows", "started", sheet_id=sheet_id)
        try:
            observed = tools.read_rows(sheet_id, row_ids, column_ids, limit)
        except ValueError as error:
            progress("read_rows", "completed", error="unavailable")
            raise ModelRetry(
                "That read was unavailable; use only the selected scope."
            ) from error
        progress(
            "read_rows",
            "completed",
            rows=len(observed["rows"]),
            truncated=observed["observation_truncated"],
        )
        return observed

    agent = Agent(
        model,
        output_type=ProjectQAAnswer,
        instructions=(
            "Answer the user's question using only the Project Ask tools. "
            "Do not guess source identifiers. Cite only citation IDs returned by read_rows. "
            "Use inspect_sheets before reading unfamiliar sheets. Recent conversation history "
            f"(may be truncated): {recent_history()}"
        ),
        retries=1,
    )
    agent.tool_plain(inspect_sheets, name="inspect_sheets")
    agent.tool_plain(read_rows, name="read_rows")

    @agent.output_validator
    def known_citations(answer: ProjectQAAnswer) -> ProjectQAAnswer:
        nonlocal citation_repairs
        unknown = set(answer.citation_ids) - tools.citation_ids
        if unknown:
            citation_repairs += 1
            if citation_repairs == 1:
                raise ModelRetry("Use only citation IDs returned by read_rows.")
            return answer.model_copy(
                update={
                    "citation_ids": [
                        citation_id
                        for citation_id in answer.citation_ids
                        if citation_id in tools.citation_ids
                    ]
                }
            )
        return answer

    try:
        result = await agent.run(
            turn["question"],
            usage_limits=UsageLimits(
                request_limit=MAX_MODEL_REQUESTS, tool_calls_limit=MAX_TOOL_CALLS
            ),
        )
    except UsageLimitExceeded:
        partial = {
            "text": "I reached this investigation's limit before finishing. Please narrow the question or scope and try again.",
            "citation_ids": [],
            "limited": True,
        }
        store.append_event(turn["id"], kind="assistant", payload=partial)
        return partial
    except UnexpectedModelBehavior:
        raise

    answer = result.output
    payload = {"text": answer.text, "citation_ids": answer.citation_ids}
    store.append_event(turn["id"], kind="answer", payload=payload)
    return payload
