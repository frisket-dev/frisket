from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

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
from frisket.ai.llm.structured_probe import PROBE_SCHEMA, ProbeStore, probe_mechanism
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
    router = ModelRouter(keys={}, max_retries=max_retries)
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
        StructuredCompleter(router).complete(
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
        # Anthropic's Messages API has no `response_format` concept at all --
        # neither strict json_schema mode nor json_object mode exists on that
        # wire dialect (adapters.py::AnthropicAdapter).
        ("anthropic", "anthropic/claude-haiku-4-5", NATIVE_STRICT),
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
            StructuredCompleter(router).complete(
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
        StructuredCompleter(router).complete(
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
        StructuredCompleter(router).complete(
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


def test_anthropic_auto_still_forces_emit_tool_unaffected_by_prose_json_branch():
    """Regression guard for tests/test_structured_capabilities.py::
    test_anthropic_wire_body_always_forces_emit_tool_regardless_of_table --
    adding the PROSE_JSON branch must not change the default (no explicit
    mechanism) behavior."""
    from frisket.ai.llm.types import LLMRequest

    adapter = AnthropicAdapter("k")
    req = LLMRequest(
        model="anthropic/claude-haiku-4-5",
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


# ---------------------------------------------------------------------------
# Item 2: probe_mechanism() -- one wire call, records, never cascades.
# ---------------------------------------------------------------------------


def test_probe_records_supported_on_success(tmp_path):
    store = ProbeStore(tmp_path / "probes.db")
    router, adapter = scripted_router("openai", [_resp({"ok": True})])
    record = run(
        probe_mechanism(router, "openai/gpt-5-mini", NATIVE_STRICT, store=store)
    )
    assert record.supported is True
    assert record.model == "openai/gpt-5-mini"
    assert record.mechanism == NATIVE_STRICT
    assert len(adapter.seen) == 1  # exactly one wire call
    # persisted -- a fresh store handle reading the same file sees it.
    reopened = ProbeStore(tmp_path / "probes.db")
    fetched = reopened.get("openai/gpt-5-mini", NATIVE_STRICT)
    assert fetched is not None and fetched.supported is True


def test_probe_records_unsupported_on_failure_and_never_retries(tmp_path):
    store = ProbeStore(tmp_path / "probes.db")
    router, adapter = scripted_router(
        "openai", [SchemaViolation("no dice", raw_text="not json")]
    )
    record = run(
        probe_mechanism(router, "openai/gpt-5-mini", NATIVE_STRICT, store=store)
    )
    assert record.supported is False
    # one attempt, repair_attempts=0 (fail-hard) -- never cascades to a
    # different mechanism, never re-tries the same one either.
    assert len(adapter.seen) == 1
    assert adapter.seen[0].mechanism == NATIVE_STRICT


def test_probe_uses_a_trivial_known_schema_not_a_user_schema(tmp_path):
    store = ProbeStore(tmp_path / "probes.db")
    router, adapter = scripted_router("openai", [_resp({"ok": True})])
    run(probe_mechanism(router, "openai/gpt-5-mini", NATIVE_STRICT, store=store))
    # pydantic-ai's StructuredDict may re-derive the wire json_schema (title/
    # additionalProperties normalization) -- assert on PROBE_SCHEMA's actual
    # content (the "ok" property + requiredness), not byte-identity.
    seen_schema = adapter.seen[0].schema
    assert set(PROBE_SCHEMA["properties"]) <= set(seen_schema.get("properties", {}))
    assert seen_schema.get("required") == PROBE_SCHEMA["required"]


def test_probe_never_runs_as_a_side_effect_of_a_normal_completion(tmp_path):
    """Record-don't-experiment: a normal StructuredCompleter.complete() call
    -- even one wired to a probe_store -- never WRITES a probe record itself;
    only an explicit probe_mechanism() call does. The store stays empty after
    ordinary user-row traffic."""
    store = ProbeStore(tmp_path / "probes.db")
    router, _ = scripted_router("openai", [_resp({"ok": True})])
    run(
        StructuredCompleter(router, probe_store=store).complete(
            StructuredRequest(
                model="openai/gpt-5-mini",
                messages=[{"role": "user", "content": "go"}],
                schema=OK_SCHEMA,
            )
        )
    )
    assert store.all_for_model("openai/gpt-5-mini") == []


def test_a_recorded_unsupported_probe_blocks_a_later_override_without_rerunning_it(
    tmp_path,
):
    """A probe records an unsupported mechanism without rerunning a user row.
    Once ``probe_mechanism`` has
    recorded NATIVE_STRICT unsupported for a model, a completer opted into
    that store (`probe_store=...`) rejects a later explicit pin of the SAME
    mechanism pre-flight (no new wire call), rather than silently sending the
    user's row under NATIVE_STRICT again to re-discover the same failure."""
    store = ProbeStore(tmp_path / "probes.db")
    router, adapter = scripted_router(
        "openrouter", [SchemaViolation("nope", raw_text="bad")]
    )
    run(
        probe_mechanism(
            router, "openrouter/some-vendor/model", NATIVE_STRICT, store=store
        )
    )
    assert len(adapter.seen) == 1

    with pytest.raises(UnsupportedMechanismError):
        run(
            StructuredCompleter(router, probe_store=store).complete(
                StructuredRequest(
                    model="openrouter/some-vendor/model",
                    messages=[{"role": "user", "content": "go"}],
                    schema=OK_SCHEMA,
                    method=NATIVE_STRICT,
                )
            )
        )
    # the rejected user row made ZERO additional wire calls -- the probe's
    # own single call from earlier is still the only one on record.
    assert len(adapter.seen) == 1


def test_probe_store_without_recorded_row_does_not_block_the_override():
    """A probe_store with no entry for this (model, mechanism) is silence,
    not a rejection -- the override still validates against dialect facts
    only and proceeds."""
    from frisket.ai.llm.structured_probe import ProbeStore as _PS

    store = _PS(":memory:")
    router, adapter = scripted_router("openai", [_resp({"ok": True})])
    result = run(
        StructuredCompleter(router, probe_store=store).complete(
            StructuredRequest(
                model="openai/gpt-5-mini",
                messages=[{"role": "user", "content": "go"}],
                schema=OK_SCHEMA,
                method=NATIVE_STRICT,
            )
        )
    )
    assert result.data == {"ok": True}
    assert len(adapter.seen) == 1


# ---------------------------------------------------------------------------
# Item 3: RECORDED_PROMOTIONS -- openrouter/openai/* -> NATIVE_STRICT, opt-in
# only, the dialect split parity untouched by default.
# ---------------------------------------------------------------------------


def test_resolve_default_stays_parity_only_no_promotion():
    """Regression guard mirroring tests/test_structured_capabilities.py::
    test_resolve_per_provider -- capability refinement landing must NOT change resolve()'s
    default (no promotions=) output for ANY model, ever."""
    row = capabilities.resolve("openrouter/openai/gpt-5")
    assert row.structured_mode == JSON_LOOSE


def test_resolve_with_recorded_promotions_opts_in_the_openai_vendor_glob():
    row = capabilities.resolve(
        "openrouter/openai/gpt-5", promotions=capabilities.RECORDED_PROMOTIONS
    )
    assert row.structured_mode == NATIVE_STRICT


def test_recorded_promotion_does_not_leak_to_other_openrouter_vendors():
    row = capabilities.resolve(
        "openrouter/anthropic/claude-x", promotions=capabilities.RECORDED_PROMOTIONS
    )
    assert row.structured_mode == JSON_LOOSE  # untouched -- only openai/* promoted


def test_completer_auto_resolution_only_promotes_with_a_probe_store(tmp_path):
    """End-to-end: the SAME "auto" request against the SAME model resolves
    differently depending only on whether this completer instance opted into
    the recorded-capability layer -- the promotion is never unconditional."""
    router, adapter = scripted_router("openrouter", [_resp({"ok": True})])
    result_default = run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="openrouter/openai/gpt-5",
                messages=[{"role": "user", "content": "go"}],
                schema=OK_SCHEMA,
            )
        )
    )
    assert result_default.method_used == JSON_LOOSE

    store = ProbeStore(tmp_path / "probes.db")
    result_opted_in = run(
        StructuredCompleter(router, probe_store=store).complete(
            StructuredRequest(
                model="openrouter/openai/gpt-5",
                messages=[{"role": "user", "content": "go"}],
                schema=OK_SCHEMA,
            )
        )
    )
    assert result_opted_in.method_used == NATIVE_STRICT


def test_recorded_promotion_evidence(tmp_path):
    """THE evidence RECORDED_PROMOTIONS' `source` field cites (spec: the
    promotion must land "behind a RECORDED probe/fixture"). This is not a
    live OpenRouter API call (no key in this environment) -- it is a REAL
    `probe_mechanism()` run (real completer code, real jsonschema
    validation) against a fixture wire response reproducing OpenRouter's
    documented `openai/*` behavior: their `openai/*`-family models proxy
    OpenAI's own `/chat/completions` wire dialect 1:1, so a strict
    `json_schema` request gets the identical OpenAI-shaped response body
    OpenAICompatAdapter already parses uniformly for every provider it
    covers (tests/test_structured_capabilities.py's
    `test_openai_wire_body_is_native_strict_...` pins the request-side half
    of that same claim). The probe's job is only to confirm the RESPONSE
    side actually validates end-to-end -- it does."""
    store = ProbeStore(tmp_path / "probes.db")
    router, adapter = scripted_router("openrouter", [_resp({"ok": True})])
    record = run(
        probe_mechanism(
            router,
            "openrouter/openai/gpt-5",
            NATIVE_STRICT,
            store=store,
            note=(
                "recorded 2026-07-09: OpenRouter's openai/* proxy speaks the "
                "identical OpenAI json_schema-strict wire dialect "
                "OpenAICompatAdapter already implements uniformly (no "
                "special-casing by provider); probe_mechanism() sent the "
                "trivial known schema under native_strict and the response "
                "validated cleanly on the first attempt."
            ),
        )
    )
    assert record.supported is True
    assert record.mechanism == NATIVE_STRICT
    assert len(adapter.seen) == 1
    assert adapter.seen[0].mechanism == NATIVE_STRICT
