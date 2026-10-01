from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic_ai import Agent, CallDeferred, DeferredToolRequests
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness import Skills
from pydantic_ai_harness.compaction import SummarizingCompaction, TieredCompaction

from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.structured import FrisketRouterModel
from frisket.ai.llm.types import LLMResponse


MODEL_ID = "gemini/gemini-3.5-flash"


def _text_response(text: str) -> dict[str, Any]:
    return {
        "id": "response-id",
        "choices": [
            {
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 8},
    }


def _tool_response(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {
        "id": "response-id",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 8},
    }


def _router_with_responses(
    responses: list[dict[str, Any]],
) -> tuple[ModelRouter, list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    router = ModelRouter(
        keys={"gemini": "test-key"}, cache=None, cache_mode="off", max_retries=0
    )
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return router, requests


@pytest.mark.anyio
async def test_skills_load_over_gemini_and_messages_round_trip(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "source-check"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: source-check\n"
        "description: Check claims against cited source material.\n"
        "---\n\n"
        "Inspect the cited passage before accepting the claim.\n",
        encoding="utf-8",
    )
    router, wire_requests = _router_with_responses(
        [
            _tool_response("load_capability", {"id": "source-check"}, "skill-1"),
            _text_response("The source-check skill is loaded."),
        ]
    )
    model = FrisketRouterModel(router, MODEL_ID)
    agent = Agent(
        model,
        output_type=str,
        capabilities=[Skills("skills", workspace=LocalWorkspaceBackend(tmp_path))],
    )

    result = await agent.run("Load the source-check skill, then acknowledge it.")

    assert result.output == "The source-check skill is loaded."
    assert [tool["function"]["name"] for tool in wire_requests[0]["tools"]] == [
        "load_capability"
    ]
    assert "Inspect the cited passage" not in json.dumps(wire_requests[0]["messages"])
    assert "Inspect the cited passage" in json.dumps(wire_requests[1]["messages"])
    serialized = ModelMessagesTypeAdapter.dump_json(result.all_messages())
    restored = ModelMessagesTypeAdapter.validate_json(serialized)
    assert restored == result.all_messages()


@pytest.mark.anyio
async def test_tiered_compaction_uses_router_and_accounts_summary_call() -> None:
    router, wire_requests = _router_with_responses(
        [
            _text_response(
                "## Intent\nContinue the investigation with exact source IDs."
            ),
            _text_response("Investigation continued."),
        ]
    )
    accounted: list[LLMResponse] = []

    async def on_response(response: LLMResponse) -> None:
        accounted.append(response)

    model = FrisketRouterModel(router, MODEL_ID, on_response=on_response)
    assert model.model_id == "gemini:gemini-3.5-flash"
    assert model.context_window == 1_000_000
    agent = Agent(
        model,
        output_type=str,
        capabilities=[
            TieredCompaction(
                tiers=[
                    SummarizingCompaction(
                        max_messages=1, keep_messages=1, receipts=True
                    )
                ],
                target_fraction=0.00003,
            )
        ],
    )
    history = [
        ModelRequest(parts=[UserPromptPart("Investigate " + "records " * 80)]),
        ModelResponse(
            parts=[TextPart("Prior findings " + "evidence " * 80)],
            model_name=MODEL_ID,
            provider_name="gemini",
        ),
    ]

    result = await agent.run("Continue.", message_history=history)

    assert result.output == "Investigation continued."
    assert len(wire_requests) == 2
    assert "context summarization assistant" in json.dumps(wire_requests[0]["messages"])
    assert "Summary of previous conversation" in json.dumps(
        wire_requests[1]["messages"]
    )
    assert accounted == model.wire_calls
    assert len(accounted) == 2
    assert all(response.cost is not None for response in accounted)


@pytest.mark.anyio
async def test_budget_refusal_before_final_request_makes_no_extra_wire_call() -> None:
    router, wire_requests = _router_with_responses(
        [_text_response("## Intent\nKeep the investigation bounded.")]
    )
    admitted_requests = 0

    class BudgetPaused(RuntimeError):
        pass

    async def admit_request(_request: Any) -> None:
        nonlocal admitted_requests
        admitted_requests += 1
        if admitted_requests > 1:
            raise BudgetPaused

    model = FrisketRouterModel(router, MODEL_ID, before_request=admit_request)
    agent = Agent(
        model,
        output_type=str,
        capabilities=[
            TieredCompaction(
                tiers=[
                    SummarizingCompaction(
                        max_messages=1, keep_messages=1, receipts=True
                    )
                ],
                target_fraction=0.00003,
            )
        ],
    )
    history = [
        ModelRequest(parts=[UserPromptPart("Investigate " + "records " * 80)]),
        ModelResponse(
            parts=[TextPart("Prior findings " + "evidence " * 80)],
            model_name=MODEL_ID,
            provider_name="gemini",
        ),
    ]

    with pytest.raises(BudgetPaused):
        await agent.run("Continue.", message_history=history)

    assert admitted_requests == 2
    assert len(wire_requests) == 1
    assert model.attempts == 1
    assert len(model.wire_calls) == 1


@pytest.mark.anyio
async def test_deferred_action_messages_serialize_and_resume_with_same_call_id() -> (
    None
):
    router, wire_requests = _router_with_responses(
        [
            _tool_response("run_action", {"action": "extract entities"}, "job-1"),
            _text_response("The action completed with 12 entities."),
        ]
    )
    model = FrisketRouterModel(router, MODEL_ID)
    agent = Agent(model, output_type=[str, DeferredToolRequests])

    @agent.tool_plain
    async def run_action(action: str) -> str:
        raise CallDeferred(metadata={"action": action})

    paused = await agent.run("Extract entities with the available action.")

    assert isinstance(paused.output, DeferredToolRequests)
    [request] = paused.output.calls
    assert request.tool_call_id == "job-1"
    assert paused.output.metadata == {"job-1": {"action": "extract entities"}}
    serialized = ModelMessagesTypeAdapter.dump_json(paused.all_messages())
    restored = ModelMessagesTypeAdapter.validate_json(serialized)
    results = paused.output.build_results(
        calls={"job-1": {"status": "completed", "entity_count": 12}}
    )

    resumed = await agent.run(
        message_history=restored,
        deferred_tool_results=results,
    )

    assert resumed.output == "The action completed with 12 entities."
    followup = json.dumps(wire_requests[1]["messages"])
    assert "job-1" in followup
    assert "entity_count" in followup
