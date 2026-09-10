from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.types import LLMResponse, SchemaViolation
from frisket.ops.base import OpContext


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# copilot.py: repairs via the completer, fails once repair_attempts (1) is
# exhausted.
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedAdapter:
    outcomes: list[Any]
    base_url: str = "http://mock"
    seen: list[Any] = field(default_factory=list)
    _i: int = 0

    async def complete(self, req: Any, client: Any) -> LLMResponse:
        self.seen.append(req)
        outcome = self.outcomes[min(self._i, len(self.outcomes) - 1)]
        self._i += 1
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(req)
        return outcome


def _resp(data: dict | None, *, tokens_in=50, tokens_out=10, cost=0.001):
    return lambda req: LLMResponse(
        content=json.dumps(data),
        data=data,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost,
        model=req.model,
    )


def _scripted_router(outcomes: list[Any], *, max_retries=3, provider="mock"):
    router = ModelRouter(keys={}, max_retries=max_retries)
    adapter = _ScriptedAdapter(outcomes=list(outcomes))
    router._adapters[provider] = adapter  # noqa: SLF001
    return router, adapter


def test_copilot_chat_repairs_invalid_then_valid_via_completer(tmp_path):
    # Program G: the copilot reply schema now also requires needs_import.
    valid = {"reply": "here is your action", "needs_import": False, "proposals": []}
    router, adapter = _scripted_router(
        [_resp({"reply": "oops"}), _resp(valid)]  # 1st: missing needs_import/proposals
    )
    from frisket.authoring.copilot import copilot_chat
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "copilot-repair.frisket")
    try:
        result = run(
            copilot_chat(project, router, [{"role": "user", "content": "hi"}], "mock/m")
        )
        assert result["reply"] == "here is your action"
        assert result["needs_import"] is False
        assert result["schema_version"] == "frisket.copilot_reply.v1"
        assert len(adapter.seen) == 2  # 1 bad + 1 corrective, per repair_attempts=1
    finally:
        project.close()


def test_copilot_chat_exhausts_repair_and_raises(tmp_path):
    router, _ = _scripted_router(
        [_resp({"reply": "bad"}), _resp({"reply": "still bad"})]
    )
    from frisket.authoring.copilot import copilot_chat
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "copilot-exhaustion.frisket")
    try:
        with pytest.raises(SchemaViolation):
            run(
                copilot_chat(
                    project,
                    router,
                    [{"role": "user", "content": "hi"}],
                    "mock/m",
                )
            )
    finally:
        project.close()


# ---------------------------------------------------------------------------
# ops/ocr.py's `_ocr_vlm`: repairs via the completer.
# ---------------------------------------------------------------------------


def test_ocr_vlm_repairs_invalid_then_valid_via_completer(tmp_path):
    from frisket.ops.ocr_engines import OcrEngines

    page = tmp_path / "scan.png"
    page.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-image-but-bytes-are-enough")
    router, adapter = _scripted_router(
        [_resp({}), _resp({"text": "HELLO WORLD"})]  # 1st: missing required "text"
    )
    ctx = OpContext(http=None, extras={"router": router})
    usage = {"calls": 0, "in": 0, "out": 0, "cost": 0.0}
    out = run(OcrEngines()._ocr_vlm("mock/vlm", [page], ctx, usage))
    assert out == [{"text": "HELLO WORLD", "blocks": []}]
    assert len(adapter.seen) == 2


def test_ocr_vlm_exhausts_repair_and_raises(tmp_path):
    from frisket.ops.ocr_engines import OcrEngines

    page = tmp_path / "scan.png"
    page.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-image-but-bytes-are-enough")
    router, _ = _scripted_router([_resp({}), _resp({})])
    ctx = OpContext(http=None, extras={"router": router})
    usage = {"calls": 0, "in": 0, "out": 0, "cost": 0.0}
    with pytest.raises(SchemaViolation):
        run(OcrEngines()._ocr_vlm("mock/vlm", [page], ctx, usage))


# ---------------------------------------------------------------------------
# ops/agent.py: the real pydantic-ai Agent + tools loop (the multi-tool wire, not the
# PoC's stand-in), functional end to end.
# ---------------------------------------------------------------------------


class _AgentToolAdapter:
    """Odd wire calls dispatch `search`, even calls answer in plain text --
    the two-tool loop's wire shape (the multi-tool dialect's `tool_calls`, not STEP_SCHEMA)."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def complete(self, req: Any, client: Any) -> LLMResponse:
        self.requests.append(req)
        n = len(self.requests)
        if n % 2 == 1:
            return LLMResponse(
                content=None,
                data=None,
                tool_calls=[
                    {"name": "search", "args": {"query": f"q{n}"}, "id": f"c{n}"}
                ],
                tokens_in=10,
                tokens_out=5,
                cost=0.001,
                model=req.model,
            )
        return LLMResponse(
            content="Cited answer",
            data=None,
            tokens_in=10,
            tokens_out=5,
            cost=0.001,
            model=req.model,
        )


def test_ops_agent_dispatches_search_tool_through_real_wire_and_finishes(
    tmp_path, monkeypatch
):
    from frisket.ai.research.row_answer import run_research

    calls: list[str] = []

    async def fake_search(query: str) -> tuple[str, list[str]]:
        calls.append(query)
        return f"result for {query}", [f"https://example.test/{query}"]

    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    adapter = _AgentToolAdapter()
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    data, meta = run(
        run_research(
            {"company": "Acme"},
            goal="find stuff",
            model_id="anthropic/claude-haiku-4-5",
            recipe_version="1",
            router=router,
            search=fake_search,
            fetch=lambda url: pytest.fail("No fetch expected"),
        )
    )
    assert data["answer"] == "Cited answer"
    assert data["sources"] == ["https://example.test/q1"]
    assert calls == ["q1"]
    assert len(adapter.requests) == 2
    # Every wire body carries both tool definitions with tool_choice=auto,
    # exercising the multi-tool dialect through the real caller.
    assert meta["model_calls"] and len(meta["model_calls"]) == 2


def test_ops_agent_hits_step_limit_without_finishing(monkeypatch):
    from frisket.ai.research.row_answer import MAX_STEPS, run_research

    async def fake_search(query: str) -> tuple[str, list[str]]:
        return "nothing useful", []

    router = ModelRouter(
        keys={"anthropic": "k"}, cache=None, cache_mode="off", max_retries=0
    )
    # always request the search tool -- the agent never finishes on its own.
    adapter = _ScriptedAdapter(
        outcomes=[
            lambda req, i=i: LLMResponse(
                content=None,
                data=None,
                tool_calls=[{"name": "search", "args": {"query": "x"}, "id": f"c{i}"}],
                tokens_in=5,
                tokens_out=5,
                cost=0.0001,
                model=req.model,
            )
            for i in range(MAX_STEPS + 2)
        ]
    )
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    data, _meta = run(
        run_research(
            {"company": "Acme"},
            goal="find stuff",
            model_id="anthropic/claude-haiku-4-5",
            recipe_version="1",
            router=router,
            search=fake_search,
            fetch=lambda url: pytest.fail("No fetch expected"),
        )
    )
    # Step-budget exhaustion fails immediately with a typed row error; it never
    # persists apologetic prose or starts a separate synthesis agent.
    assert data["answer"] is None
    assert data["error_code"] == "research_incomplete_step_budget"
    assert data["outcome"] == "model_error"
    assert len(adapter.seen) >= MAX_STEPS  # the loop still ran to its cap


# ---------------------------------------------------------------------------
# The pydantic-ai Agent must preserve behavior that its defaults do not
# reproduce from the earlier hand-rolled loop. Each test below pins one such
# caller-visible contract.
# ---------------------------------------------------------------------------


class _EmptyTurnAdapter:
    """A model turn with no text and no tool call -- old loop finished with
    answer="" (STEP_SCHEMA's "action" was simply absent/unparseable); the bare
    pydantic-ai Agent instead raises UnexpectedModelBehavior (probed:
    "Exceeded maximum output retries (1)")."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def complete(self, req: Any, client: Any) -> LLMResponse:
        self.requests.append(req)
        return LLMResponse(
            content="",
            data=None,
            tool_calls=None,
            tokens_in=5,
            tokens_out=0,
            cost=0.0001,
            model=req.model,
        )


def test_ops_agent_empty_model_turn_finishes_with_empty_answer():
    """An empty model turn is a graceful finish with answer="" (the old
    loop's shape), not a row error. The one configured output retry applies
    before pydantic-ai raises UnexpectedModelBehavior."""
    from frisket.ai.research.row_answer import run_research

    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    adapter = _EmptyTurnAdapter()
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    data, meta = run(
        run_research(
            {"company": "Acme"},
            goal="find stuff",
            model_id="anthropic/claude-haiku-4-5",
            recipe_version="1",
            router=router,
            search=lambda query: pytest.fail("No search expected"),
            fetch=lambda url: pytest.fail("No fetch expected"),
        )
    )
    assert data["answer"] == ""
    assert len(adapter.requests) == 2
    assert meta["tokens_in"] == 10
    assert meta["tokens_out"] == 0
    assert meta["cost"] == pytest.approx(0.0002)
    assert len(meta["model_calls"]) == 2


class _BogusToolAdapter:
    """Always calls a tool name that was never registered -- old loop:
    unknown action -> observation -> continue until MAX_STEPS. Bare
    pydantic-ai: exhausts its per-tool retry budget (default 1) and raises
    UnexpectedModelBehavior (probed: "exceeded max retries count of 1")."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def complete(self, req: Any, client: Any) -> LLMResponse:
        self.requests.append(req)
        n = len(self.requests)
        return LLMResponse(
            content=None,
            data=None,
            tool_calls=[{"name": "bogus_action", "args": {}, "id": f"c{n}"}],
            tokens_in=5,
            tokens_out=5,
            cost=0.0001,
            model=req.model,
        )


def test_ops_agent_unknown_tool_call_continues_until_step_limit():
    """A repeated bogus tool
    call must NOT exhaust pydantic-ai's tool retry budget -- it produces an
    observation (pydantic-ai's own "Unknown tool name: ..." ModelRetry) and
    the loop continues under UsageLimits.request_limit=MAX_STEPS, same as the
    old unknown-action->observation->continue-until-MAX_STEPS shape."""
    from frisket.ai.research.row_answer import MAX_STEPS, run_research

    router = ModelRouter(
        keys={"anthropic": "k"}, cache=None, cache_mode="off", max_retries=0
    )
    adapter = _BogusToolAdapter()
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    data, _meta = run(
        run_research(
            {"company": "Acme"},
            goal="find stuff",
            model_id="anthropic/claude-haiku-4-5",
            recipe_version="1",
            router=router,
            search=lambda query: pytest.fail("No search expected"),
            fetch=lambda url: pytest.fail("No fetch expected"),
        )
    )
    # The bogus tool never runs search/fetch; exhaustion becomes a typed row
    # error rather than an apologetic placeholder answer.
    assert data["answer"] is None
    assert data["error_code"] == "research_incomplete_step_budget"
    assert data["outcome"] == "model_error"
    assert len(adapter.requests) == MAX_STEPS  # request_limit is the real cap


def test_to_our_messages_dedupes_instructions_and_labels_tool_returns():
    """`_to_our_messages` must not repeat `m.instructions` (pydantic-ai re-stamps
    the Agent's system prompt onto every ModelRequest turn) and a tool return
    must render as "Observation:\\n..." (the old loop's shape), not bare
    content."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    from frisket.ai.llm.structured import FrisketRouterModel

    router, _ = _scripted_router([_resp({"x": 1})])
    model = FrisketRouterModel(router, "anthropic/claude-haiku-4-5")

    system = "You are a research agent. Never fabricate."
    messages = [
        ModelRequest(parts=[UserPromptPart(content="row")], instructions=system),
        ModelResponse(parts=[ToolCallPart(tool_name="search", args={"query": "q1"})]),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="search", content="result for q1", tool_call_id="c1"
                )
            ],
            instructions=system,  # pydantic-ai re-stamps this every turn
        ),
    ]
    out = model._to_our_messages(messages)  # noqa: SLF001

    system_msgs = [m for m in out if m["role"] == "system"]
    assert len(system_msgs) == 1, "instructions must not repeat per turn"

    tool_return_msgs = [
        m for m in out if m["role"] == "user" and "result for q1" in str(m["content"])
    ]
    assert tool_return_msgs == [
        {"role": "user", "content": "Observation:\nresult for q1"}
    ]


def test_to_our_messages_rejects_unadmitted_response_parts():
    from pydantic_ai.messages import ModelResponse, ThinkingPart

    from frisket.ai.llm.structured import FrisketRouterModel

    router, _ = _scripted_router([_resp({"x": 1})])
    model = FrisketRouterModel(router, "anthropic/claude-haiku-4-5")
    with pytest.raises(TypeError, match="unsupported response part: thinking"):
        model._to_our_messages(  # noqa: SLF001
            [ModelResponse(parts=[ThinkingPart(content="private chain")])]
        )
