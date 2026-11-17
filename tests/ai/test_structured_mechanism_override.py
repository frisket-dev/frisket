from __future__ import annotations
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from frisket.execution.dispatch_control import STANDALONE
from frisket.ai.llm import structured_capabilities as capabilities
from frisket.ai.llm.adapters import AnthropicAdapter
from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.structured import (
    StructuredCompleter,
    StructuredRequest,
    UnsupportedMechanismError,
)
from frisket.ai.llm.structured_capabilities import (
    JSON_LOOSE,
    NATIVE_STRICT,
    NATIVE_TOOL,
    PROSE_JSON,
)
from frisket.ai.llm.types import LLMResponse, SchemaViolation

# ---------------------------------------------------------------------------
# A scripted adapter installed into a real ModelRouter under an arbitrary
# provider name -- same technique as tests/test_structured_completer.py's
# ScriptedAdapter, duplicated here (house convention: each structured test
# file is self-contained) so cache/chaos/retry/receipts are the ACTUAL
# llm/router.py code and only the wire is scripted.
# ---------------------------------------------------------------------------


@dataclass
class ScriptedAdapter:
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


def _resp(data: dict, *, tokens_in=10, tokens_out=5, cost=0.0001):
    return lambda req: LLMResponse(
        content=json.dumps(data),
        data=data,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost,
        model=req.model,
        raw={"id": "mock"},
    )


def scripted_router(provider: str, outcomes: list[Any], *, max_retries: int = 0):
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY, keys={}, max_retries=max_retries
    )
    adapter = ScriptedAdapter(outcomes=list(outcomes))
    router._adapters[provider] = adapter
    return router, adapter


def run(coro):
    return asyncio.run(coro)


OK_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}


# ---------------------------------------------------------------------------
# Item 1: the explicit override, capable + incapable pairs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider,model,mechanism",
    [
        ("openai", "openai/gpt-5-mini", NATIVE_STRICT),
        ("anthropic", "anthropic/claude-haiku-4-5", NATIVE_TOOL),
        ("openrouter", "openrouter/some-vendor/model", JSON_LOOSE),
        ("openai", "openai/gpt-5-mini", PROSE_JSON),
        ("anthropic", "anthropic/claude-haiku-4-5", PROSE_JSON),
    ],
)
def test_override_succeeds_on_a_capable_model(provider, model, mechanism):
    router, adapter = scripted_router(provider, [_resp({"ok": True})])
    result = run(
        StructuredCompleter(router.for_execution(STANDALONE)).complete(
            StructuredRequest(
                model=model,
                messages=[{"role": "user", "content": "go"}],
                schema=OK_SCHEMA,
                method=mechanism,
            )
        )
    )
    assert result.data == {"ok": True}
    assert result.method_used == mechanism
    assert len(adapter.seen) == 1


@pytest.mark.parametrize(
    "provider,model,mechanism",
    [
        # Older Claude models lack native JSON output; new 5.5 models reject
        # forced tool use. Messages has no json_object mode.
        ("anthropic", "anthropic/claude-sonnet-4-0", NATIVE_STRICT),
        ("anthropic", "anthropic/claude-sonnet-5-5", NATIVE_TOOL),
        ("anthropic", "anthropic/claude-haiku-4-5", JSON_LOOSE),
        # OpenAICompatAdapter has never wired a forced-single-tool "emit"
        # dialect for structured output -- NATIVE_TOOL is Anthropic-only.
        ("openai", "openai/gpt-5-mini", NATIVE_TOOL),
        ("openrouter", "openrouter/some-vendor/model", NATIVE_TOOL),
    ],
)
def test_override_rejects_a_knowably_incapable_model(provider, model, mechanism):
    router, adapter = scripted_router(provider, [_resp({"ok": True})])
    with pytest.raises(UnsupportedMechanismError, match=mechanism):
        run(
            StructuredCompleter(router.for_execution(STANDALONE)).complete(
                StructuredRequest(
                    model=model,
                    messages=[{"role": "user", "content": "go"}],
                    schema=OK_SCHEMA,
                    method=mechanism,
                )
            )
        )
    # honest error is pre-flight -- no wire call was ever made.
    assert adapter.seen == []


def test_prose_json_has_no_incapable_pairing_by_design():
    """PROSE_JSON is the universal fallback for providers and models that
    reject ``response_format`` outright. Every wire dialect this codebase
    speaks can express it: ``AnthropicAdapter`` and ``OpenAICompatAdapter``
    both implement it, so no model/provider combination excludes it from
    ``dialect_mechanisms``."""
    assert PROSE_JSON in capabilities.dialect_mechanisms("anthropic/claude-haiku-4-5")
    assert PROSE_JSON in capabilities.dialect_mechanisms("openai/gpt-5-mini")
    assert PROSE_JSON in capabilities.dialect_mechanisms("openrouter/x/y")
    assert PROSE_JSON in capabilities.dialect_mechanisms("some-new-provider/m")


def test_auto_never_validates_mechanism_support():
    """ "auto" has no override to validate -- an unknown/incapable-seeming
    resolution never raises UnsupportedMechanismError; only an explicit
    `method=` does."""
    router, adapter = scripted_router("anthropic", [_resp({"ok": True})])
    result = run(
        StructuredCompleter(router.for_execution(STANDALONE)).complete(
            StructuredRequest(
                model="anthropic/claude-haiku-4-5",
                messages=[{"role": "user", "content": "go"}],
                schema=OK_SCHEMA,
                # method="auto" (default) -- resolves to NATIVE_TOOL via SEED,
                # never touches the override-validation path at all.
            )
        )
    )
    assert result.method_used == NATIVE_TOOL
    assert len(adapter.seen) == 1


def test_untested_model_explicit_override_is_not_rejected():
    """An unknown provider's dialect defaults to the OpenAI-compatible
    mechanism set (capabilities.dialect_mechanisms's fallback branch) -- a
    knowable dialect fact even for a model the SEED/FALLBACK table has never
    seen, so pinning a dialect-supported mechanism there is allowed through
    (record-don't-experiment: silence about a SPECIFIC model's quality is
    not the same as a known dialect incompatibility)."""
    router, adapter = scripted_router("brand-new-provider", [_resp({"ok": True})])
    result = run(
        StructuredCompleter(router.for_execution(STANDALONE)).complete(
            StructuredRequest(
                model="brand-new-provider/mystery-model",
                messages=[{"role": "user", "content": "go"}],
                schema=OK_SCHEMA,
                method=JSON_LOOSE,
            )
        )
    )
    assert result.data == {"ok": True}


# ---------------------------------------------------------------------------
# AnthropicAdapter's new PROSE_JSON wire body -- the override is only
# HONESTLY "fully exercisable" if there's a real wire shape behind it, not
# just a validation pass. Real adapter through httpx.MockTransport (same
# technique as tests/test_structured_capabilities.py).
# ---------------------------------------------------------------------------


def _run_capturing_request(
    adapter: Any, req: Any, wire: dict
) -> tuple[dict, LLMResponse]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=wire)

    async def _go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.complete(req, client)

    resp = asyncio.run(_go())
    return captured["body"], resp


def test_anthropic_prose_json_wire_body_has_no_tools():
    from frisket.ai.llm.types import LLMRequest

    adapter = AnthropicAdapter("k")
    req = LLMRequest(
        model="anthropic/claude-haiku-4-5",
        messages=[{"role": "user", "content": "hi"}],
        schema=OK_SCHEMA,
        mechanism=PROSE_JSON,
    )
    wire = {
        "id": "msg-mock",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": '```json\n{"ok": true}\n```'}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    body, resp = _run_capturing_request(adapter, req, wire)
    assert "tools" not in body
    assert "tool_choice" not in body
    assert "Respond ONLY with JSON matching this schema" in body["system"]
    assert resp.data == {"ok": True}


def test_anthropic_prose_json_unparseable_raises_schema_violation():
    from frisket.ai.llm.types import LLMRequest

    adapter = AnthropicAdapter("k")
    req = LLMRequest(
        model="anthropic/claude-haiku-4-5",
        messages=[{"role": "user", "content": "hi"}],
        schema=OK_SCHEMA,
        mechanism=PROSE_JSON,
    )
    wire = {
        "id": "msg-mock",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "I can't help with that."}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    with pytest.raises(SchemaViolation, match="unparseable"):
        _run_capturing_request(adapter, req, wire)


def test_older_anthropic_auto_still_forces_emit_tool():
    from frisket.ai.llm.types import LLMRequest

    adapter = AnthropicAdapter("k")
    req = LLMRequest(
        model="anthropic/claude-sonnet-4-0",
        messages=[{"role": "user", "content": "hi"}],
        schema=OK_SCHEMA,
    )
    wire = {
        "id": "msg-mock",
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": "emit", "input": {"ok": True}}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    body, resp = _run_capturing_request(adapter, req, wire)
    assert body["tool_choice"] == {"type": "tool", "name": "emit"}
    assert resp.data == {"ok": True}
