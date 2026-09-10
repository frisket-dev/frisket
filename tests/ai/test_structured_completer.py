from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from frisket.ai.llm import DEFAULT_MAX_OUTPUT_TOKENS
from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.remediation import classify_llm_error
from frisket.ai.llm.structured import (
    FrisketRouterModel,
    StructuredCompleter,
    StructuredRequest,
    StructuredResult,
)
from frisket.ai.llm.types import (
    LLMRequest,
    LLMResponse,
    OutputLimitReached,
    SchemaViolation,
)

# ---------------------------------------------------------------------------
# A scripted adapter installed INTO a real ModelRouter, so cache/chaos/retry/
# receipts are the ACTUAL llm/router.py code; only the wire is scripted.
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


def _resp(
    data: dict | list | None,
    *,
    tokens_in=100,
    tokens_out=20,
    cost=0.001,
    output_limited=False,
):
    return lambda req: LLMResponse(
        content=json.dumps(data),
        data=data,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost,
        model=req.model,
        raw={"id": "mock"},
        output_limited=output_limited,
    )


def scripted_router(outcomes: list[Any], *, max_retries=3, provider="mock"):
    router = ModelRouter(keys={}, max_retries=max_retries)
    adapter = ScriptedAdapter(outcomes=list(outcomes))
    router._adapters[provider] = adapter
    return router, adapter


def run(coro):
    return asyncio.run(coro)


OBJ_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name"],
}


# ---------------------------------------------------------------------------
# FrisketRouterModel + output_validator recover a ModelRetry (invalid→retry→valid)
# ---------------------------------------------------------------------------


def test_invalid_then_valid_recovers_via_repair():
    router, adapter = scripted_router([_resp({"age": 5}), _resp({"name": "Ada"})])
    result = run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="mock/m",
                messages=[{"role": "user", "content": "go"}],
                schema=OBJ_SCHEMA,
                repair_attempts=1,
            )
        )
    )
    assert isinstance(result, StructuredResult)
    assert result.data == {"name": "Ada"}
    assert result.attempts == 2  # 1 bad + 1 corrective
    assert len(adapter.seen) == 2
    assert result.violations == ["'name' is a required property"]


def test_public_router_multimodal_repair_explains_data_instance_shape():
    """A schema-echo response gets actionable, provider-neutral feedback.

    This drives the public router seam used by live callers and pins the full
    corrective transcript: the original image survives, the invalid assistant
    value is retained, and the retry distinguishes requested data from the
    JSON Schema that describes it.
    """
    image = {
        "type": "image",
        "media_type": "image/png",
        "data": base64.b64encode(b"sentinel-image-bytes").decode(),
    }
    schema_echo = {
        "properties": {"color": "red"},
        "required": ["color"],
        "type": "object",
    }
    router, adapter = scripted_router([_resp(schema_echo), _resp({"color": "red"})])

    response = run(
        router.complete(
            LLMRequest(
                model="mock/vision",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Identify the image."},
                            image,
                        ],
                    }
                ],
                schema={
                    "type": "object",
                    "properties": {"color": {"type": "string"}},
                    "required": ["color"],
                },
            )
        )
    )

    assert response.data == {"color": "red"}
    assert len(adapter.seen) == 2
    repair_messages = adapter.seen[1].messages
    assert repair_messages[0]["content"][1] == image
    assert json.loads(repair_messages[-2]["content"]) == schema_echo
    feedback = repair_messages[-1]["content"]
    assert "data path $" in feedback
    assert "'color' is a required property" in feedback
    assert "JSON data instance" in feedback
    assert "JSON Schema" in feedback
    assert "Required properties must appear in the data object" in feedback
    assert "do not copy schema metadata" in feedback


def test_repair_feedback_names_nested_data_path():
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            }
        },
        "required": ["outer"],
    }
    router, adapter = scripted_router(
        [_resp({"outer": {}}), _resp({"outer": {"value": "ok"}})]
    )

    result = run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="mock/m",
                messages=[{"role": "user", "content": "go"}],
                schema=schema,
                repair_attempts=1,
            )
        )
    )

    assert result.data == {"outer": {"value": "ok"}}
    feedback = adapter.seen[1].messages[-1]["content"]
    assert "data path $.outer" in feedback
    assert "'value' is a required property" in feedback


def test_clean_output_is_one_attempt():
    router, adapter = scripted_router([_resp({"name": "Ada", "age": 3})])
    result = run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="mock/m",
                messages=[{"role": "user", "content": "go"}],
                schema=OBJ_SCHEMA,
            )
        )
    )
    assert result.data == {"name": "Ada", "age": 3}
    assert result.attempts == 1
    assert result.violations == []
    assert adapter.seen[0].max_tokens == DEFAULT_MAX_OUTPUT_TOKENS


def test_schema_valid_output_limit_fails_without_repair_and_keeps_wire_call():
    router, adapter = scripted_router([_resp({"name": "Ada"}, output_limited=True)])

    with pytest.raises(OutputLimitReached) as raised:
        run(
            StructuredCompleter(router).complete(
                StructuredRequest(
                    model="mock/m",
                    messages=[{"role": "user", "content": "go"}],
                    schema=OBJ_SCHEMA,
                    repair_attempts=3,
                )
            )
        )

    assert len(adapter.seen) == 1
    assert len(raised.value.wire_calls) == 1
    assert raised.value.wire_calls[0].output_limited is True
    assert classify_llm_error(raised.value).code == "invalid_output"


def test_repair_exhausted_raises_schema_violation():
    router, adapter = scripted_router([_resp({"age": 5}), _resp({"age": 6})])
    with pytest.raises(SchemaViolation, match="name"):
        run(
            StructuredCompleter(router).complete(
                StructuredRequest(
                    model="mock/m",
                    messages=[{"role": "user", "content": "go"}],
                    schema=OBJ_SCHEMA,
                    repair_attempts=1,
                )
            )
        )
    assert len(adapter.seen) == 2  # 1 + 1 corrective, then give up


def test_fail_hard_repair_attempts_zero():
    router, adapter = scripted_router([_resp({"age": 5})])
    with pytest.raises(SchemaViolation):
        run(
            StructuredCompleter(router).complete(
                StructuredRequest(
                    model="mock/m",
                    messages=[{"role": "user", "content": "go"}],
                    schema=OBJ_SCHEMA,
                    repair_attempts=0,
                )
            )
        )
    assert len(adapter.seen) == 1  # no corrective turn at all


# ---------------------------------------------------------------------------
# Cost/token summation across attempts.
# ---------------------------------------------------------------------------


def test_cost_and_tokens_summed_across_repair_attempts():
    router, _ = scripted_router(
        [
            _resp({"age": 5}, tokens_in=100, tokens_out=20, cost=0.001),
            _resp({"name": "Ada"}, tokens_in=50, tokens_out=10, cost=0.002),
        ]
    )
    result = run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="mock/m",
                messages=[{"role": "user", "content": "go"}],
                schema=OBJ_SCHEMA,
                repair_attempts=1,
            )
        )
    )
    # both wire attempts summed, not just the last one that succeeded.
    assert result.response.tokens_in == 150
    assert result.response.tokens_out == 30
    assert result.response.cost == pytest.approx(0.003)


# ---------------------------------------------------------------------------
# ordering gate: a completer-issued SchemaViolation is NOT blind-retried
# by the transport; the repair loop owns it. `SchemaViolation
# .retryable` is `False` at the class level (types.py, flipped the retryability policy), so this
# holds unconditionally now -- no per-request flag needed anymore.
# ---------------------------------------------------------------------------


def test_transport_does_not_blind_retry_completer_schema_faults():
    # transport max_retries=3 would blind-retry a retryable LLMError 4x;
    # SchemaViolation.retryable=False must stop that regardless -- the adapter
    # sees exactly repair_attempts+1 = 3 calls, not 3*(1+3).
    router, adapter = scripted_router(
        [
            SchemaViolation("chaos: malformed", raw_text="{bad"),
            SchemaViolation("chaos: malformed", raw_text="{bad"),
            _resp({"name": "Ada"}),
        ],
        max_retries=3,
    )
    result = run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="mock/m",
                messages=[{"role": "user", "content": "go"}],
                schema=OBJ_SCHEMA,
                repair_attempts=2,
            )
        )
    )
    assert result.data == {"name": "Ada"}
    assert len(adapter.seen) == 3


# ---------------------------------------------------------------------------
# Transcript-copy invariant: repair turns never leak into the
# caller's persistent message list.
# ---------------------------------------------------------------------------


def test_repair_does_not_mutate_caller_messages():
    original = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "go"},
    ]
    snapshot = json.loads(json.dumps(original))
    router, _ = scripted_router([_resp({"age": 5}), _resp({"name": "Ada"})])
    run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="mock/m",
                messages=original,
                schema=OBJ_SCHEMA,
                repair_attempts=1,
            )
        )
    )
    assert original == snapshot


# ---------------------------------------------------------------------------
# Top-level-array schema wrap/unwrap for an affected op family (entities).
# ---------------------------------------------------------------------------

ENTITY_OUTPUT_ARRAY_SCHEMA = {  # ops/entities.py:41 — TOP-LEVEL ARRAY
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "type": {"type": "string"},
        },
        "required": ["text"],
    },
}


def test_top_level_array_schema_wraps_and_unwraps():
    entities = [{"text": "Ada", "type": "PERSON"}, {"text": "NYC", "type": "PLACE"}]
    # The provider is asked for {"items": [...]} (the wrap); the completer unwraps.
    router, _ = scripted_router([_resp({"items": entities})])
    result = run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="mock/m",
                messages=[{"role": "user", "content": "extract"}],
                schema=ENTITY_OUTPUT_ARRAY_SCHEMA,
            )
        )
    )
    assert result.data == entities  # unwrapped back to a top-level list
    assert isinstance(result.data, list)


def test_top_level_array_item_violation_repairs():
    bad = {"items": [{"type": "PERSON"}]}  # item missing required "text"
    good = {"items": [{"text": "Ada", "type": "PERSON"}]}
    router, adapter = scripted_router([_resp(bad), _resp(good)])
    result = run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="mock/m",
                messages=[{"role": "user", "content": "extract"}],
                schema=ENTITY_OUTPUT_ARRAY_SCHEMA,
                repair_attempts=1,
            )
        )
    )
    assert result.data == [{"text": "Ada", "type": "PERSON"}]
    assert len(adapter.seen) == 2


# ---------------------------------------------------------------------------
# Anthropic validation ON — the cutover. Drives the REAL AnthropicAdapter
# (forced `emit` tool_use, adapters.py:80-121) through httpx.MockTransport, so
# the tool_use `input` that is returned UNVALIDATED today now flows through the
# completer's jsonschema validator.
# ---------------------------------------------------------------------------


def _anthropic_tool_wire(tool_input: dict) -> dict:
    return {
        "id": "msg-mock",
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": "emit", "input": tool_input}],
        "usage": {"input_tokens": 42, "output_tokens": 17},
    }


def _anthropic_router(wire_bodies: list[dict], *, max_retries=3):
    """A real ModelRouter whose anthropic adapter is a real AnthropicAdapter
    backed by a MockTransport returning ``wire_bodies`` in order."""
    counter = {"i": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        body = wire_bodies[min(counter["i"], len(wire_bodies) - 1)]
        counter["i"] += 1
        return httpx.Response(200, json=body)

    router = ModelRouter(keys={"anthropic": "k"}, max_retries=max_retries)
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return router


def test_anthropic_missing_required_now_raises():
    # today (adapters.py:104-121) this tool_use input is returned unvalidated;
    # the completer validates native_tool output like everything else.
    router = _anthropic_router([_anthropic_tool_wire({"age": 5})])  # no "name"
    with pytest.raises(SchemaViolation, match="name"):
        run(
            StructuredCompleter(router).complete(
                StructuredRequest(
                    model="anthropic/claude-x",
                    messages=[{"role": "user", "content": "go"}],
                    schema=OBJ_SCHEMA,
                    repair_attempts=0,
                )
            )
        )


def test_anthropic_cutover_repair_succeeds():
    # a currently-silent missing-required response → bounded repair → success.
    router = _anthropic_router(
        [_anthropic_tool_wire({"age": 5}), _anthropic_tool_wire({"name": "Ada"})]
    )
    result = run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="anthropic/claude-x",
                messages=[{"role": "user", "content": "go"}],
                schema=OBJ_SCHEMA,
                repair_attempts=1,
            )
        )
    )
    assert result.data == {"name": "Ada"}
    assert result.attempts == 2


def test_anthropic_cutover_repair_exhausts():
    # …and the exhausted branch: never valid → surfaced SchemaViolation.
    router = _anthropic_router([_anthropic_tool_wire({"age": 5})])
    with pytest.raises(SchemaViolation, match="name"):
        run(
            StructuredCompleter(router).complete(
                StructuredRequest(
                    model="anthropic/claude-x",
                    messages=[{"role": "user", "content": "go"}],
                    schema=OBJ_SCHEMA,
                    repair_attempts=1,
                )
            )
        )


# ---------------------------------------------------------------------------
# schema_rejects metric surfaced via health_snapshot() per provider.
# ---------------------------------------------------------------------------


def test_health_snapshot_reports_schema_rejects_per_provider():
    router = _anthropic_router(
        [_anthropic_tool_wire({"age": 5}), _anthropic_tool_wire({"name": "Ada"})]
    )
    run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="anthropic/claude-x",
                messages=[{"role": "user", "content": "go"}],
                schema=OBJ_SCHEMA,
                repair_attempts=1,
            )
        )
    )
    snap = router.health_snapshot()
    assert snap["providers"]["anthropic"]["schema_rejects"] == 1


def test_health_snapshot_schema_rejects_defaults_zero():
    router, _ = scripted_router([_resp({"name": "Ada"})])
    run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="mock/m",
                messages=[{"role": "user", "content": "go"}],
                schema=OBJ_SCHEMA,
            )
        )
    )
    snap = router.health_snapshot()
    # every provider row carries the counter, starting at 0.
    for prov in snap["providers"].values():
        assert prov["schema_rejects"] == 0


# ---------------------------------------------------------------------------
# The shim is a real pydantic-ai Model whose profile reflects a capability row.
# ---------------------------------------------------------------------------


def test_frisket_router_model_profile_from_capability():
    router, _ = scripted_router([_resp({"name": "Ada"})])
    cap = {
        "default_mechanism": "native_tool",
        "supports_native_strict": False,
        "supports_native_tool": True,
        "supports_json_object": False,
    }
    model = FrisketRouterModel(router, "anthropic/claude", capability=cap)
    prof = model.profile  # pydantic-ai exposes the merged profile as a mapping
    assert prof.get("default_structured_output_mode") == "tool"
    assert prof.get("supports_json_schema_output") is False


# ---------------------------------------------------------------------------
# Structurally invalid schema -> typed SchemaViolation, not a raw jsonschema
# crash; the boundary applies uniformly.
# ---------------------------------------------------------------------------


def test_invalid_schema_raises_typed_violation():
    router, _ = scripted_router([_resp({"name": "Ada"})])
    with pytest.raises(SchemaViolation, match="invalid schema"):
        run(
            StructuredCompleter(router).complete(
                StructuredRequest(
                    model="mock/m",
                    messages=[{"role": "user", "content": "go"}],
                    schema={"type": "not-a-real-type"},
                )
            )
        )


# ---------------------------------------------------------------------------
# attempts == every wire call, including adapter-origin schema-fault
# translations (A regression: `len(wire_calls)` alone undercounts, since a
# translated SchemaViolation raises BEFORE the `wire_calls.append` — a run with
# 1 translated fault + 1 success made 2 wire calls but reported attempts=1).
# ---------------------------------------------------------------------------


def test_attempts_counts_translated_schema_fault_wire_calls():
    router, adapter = scripted_router(
        [
            SchemaViolation("chaos: malformed", raw_text="{bad"),
            _resp({"name": "Ada"}),
        ],
        max_retries=3,
    )
    result = run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="mock/m",
                messages=[{"role": "user", "content": "go"}],
                schema=OBJ_SCHEMA,
                repair_attempts=1,
            )
        )
    )
    assert result.data == {"name": "Ada"}
    assert len(adapter.seen) == 2  # 1 adapter-origin fault + 1 success
    assert result.attempts == 2  # must count BOTH, not just the success


def test_attempts_counts_multiple_translated_faults_then_success():
    router, adapter = scripted_router(
        [
            SchemaViolation("chaos: malformed", raw_text="{bad"),
            SchemaViolation("chaos: malformed", raw_text="{bad2"),
            _resp({"name": "Ada"}),
        ],
        max_retries=5,
    )
    result = run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="mock/m",
                messages=[{"role": "user", "content": "go"}],
                schema=OBJ_SCHEMA,
                repair_attempts=2,
            )
        )
    )
    assert len(adapter.seen) == 3
    assert result.attempts == 3


# ---------------------------------------------------------------------------
# response.data always matches the completer's own unwrapped result.data
# (A regression, the structured.py end): _sum_response used to keep
# wire_calls[-1].data verbatim, which is the WIRE-WRAPPED {"items": [...]}
# form for a top-level-array schema — the receipt disagreed with the contract.
# ---------------------------------------------------------------------------


def test_array_schema_response_data_matches_unwrapped_result_data():
    entities = [{"text": "Ada", "type": "PERSON"}]
    router, _ = scripted_router([_resp({"items": entities})])
    result = run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="mock/m",
                messages=[{"role": "user", "content": "extract"}],
                schema=ENTITY_OUTPUT_ARRAY_SCHEMA,
            )
        )
    )
    assert result.data == entities
    assert result.response.data == entities  # NOT {"items": [...]}
    assert result.response.data == result.data  # never disagree with the contract


# ---------------------------------------------------------------------------
# Exhausted-repair raw_text falls back to the payload when `.content` is None
# (A regression): Anthropic tool_use puts the payload in `.data`
# (adapters.py:107), so `wire_calls[-1].content` alone is None there — the
# exhausted SchemaViolation's raw_text (recorded as trace.py's raw_response)
# must not silently drop the actual model output.
# ---------------------------------------------------------------------------


def test_anthropic_exhausted_raw_text_falls_back_to_data_payload():
    router = _anthropic_router([_anthropic_tool_wire({"age": 5})])  # no "name"
    with pytest.raises(SchemaViolation) as exc_info:
        run(
            StructuredCompleter(router).complete(
                StructuredRequest(
                    model="anthropic/claude-x",
                    messages=[{"role": "user", "content": "go"}],
                    schema=OBJ_SCHEMA,
                    repair_attempts=0,
                )
            )
        )
    assert exc_info.value.raw_text == json.dumps({"age": 5})
