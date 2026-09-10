from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from frisket.ai.llm.adapters import AnthropicAdapter, OpenAICompatAdapter
from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.structured import (
    StructuredCompleter,
    StructuredRequest,
    _extract_prose_json,
)
from frisket.ai.llm.types import LLMRequest, LLMResponse, SchemaViolation

# ---------------------------------------------------------------------------
# Mechanism ids -- Section 2's closed vocabulary. Duplicated here (not
# imported) so this suite's fixtures stay independent of
# `llm/structured_capabilities.py`'s internal naming; the string values are
# pinned identical across both modules by `tests/test_structured_capabilities.py`.
# ---------------------------------------------------------------------------
NATIVE_STRICT = "native_strict"
NATIVE_TOOL = "native_tool"
JSON_LOOSE = "json_loose"
PROSE_JSON = "prose_json"
MECHANISMS = (NATIVE_STRICT, NATIVE_TOOL, JSON_LOOSE, PROSE_JSON)


# ---------------------------------------------------------------------------
# Mock wire payloads -- literal provider response bodies, one shape per
# transport dialect. native_strict/json_loose/prose_json all arrive as the
# OpenAI-compat `choices[0].message.content` string (adapters.py:198-225) --
# they differ only in the REQUEST body (Section 2/3), never the response
# wire shape, so one builder covers all three.
# ---------------------------------------------------------------------------


def _openai_wire(content: str, *, finish_reason: str = "stop") -> dict:
    return {
        "id": "chatcmpl-mock",
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 42, "completion_tokens": 17},
    }


def _anthropic_wire(tool_input: dict | None, *, text: str | None = None) -> dict:
    content: list[dict[str, Any]] = []
    if tool_input is not None:
        content.append({"type": "tool_use", "name": "emit", "input": tool_input})
    if text is not None:
        content.append({"type": "text", "text": text})
    return {
        "id": "msg-mock",
        "stop_reason": "tool_use" if tool_input is not None else "end_turn",
        "content": content,
        "usage": {"input_tokens": 42, "output_tokens": 17},
    }


def test_adapters_normalize_provider_output_limit_reasons():
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    openai = _run_openai_adapter(
        OpenAICompatAdapter("k", "https://example.invalid/v1"),
        _req(schema),
        _openai_wire('{"name":"Ada"}', finish_reason="length"),
    )
    anthropic_wire = _anthropic_wire({"name": "Ada"})
    anthropic_wire["stop_reason"] = "max_tokens"
    anthropic = _run_anthropic_adapter(
        AnthropicAdapter("k"), _req(schema), anthropic_wire
    )

    assert openai.output_limited is True
    assert anthropic.output_limited is True


def _wire_for(mechanism: str, data: dict) -> dict:
    """The 'clean' wire payload for a given mechanism + candidate data."""
    if mechanism == NATIVE_TOOL:
        return _anthropic_wire(data)
    text = json.dumps(data)
    if mechanism == PROSE_JSON:
        # exercise the fenced-code-block shape by default; dedicated
        # fenced/unfenced extraction cases build their own wire directly.
        return _openai_wire(f"Sure, here you go:\n```json\n{text}\n```\nLet me know!")
    return _openai_wire(text)


def _unparseable_wire(mechanism: str) -> dict:
    if mechanism == NATIVE_TOOL:
        # the equivalent extraction failure for tool_use: model declines the
        # forced tool and answers in prose instead (adapters.py:111-112).
        return _anthropic_wire(None, text="I decline to use the tool.")
    if mechanism == PROSE_JSON:
        # no fence, no balanced {...} span -- Section 2's "no match" case:
        # passes through unchanged into the json.loads failure path.
        return _openai_wire("I can't help with that request.")
    return _openai_wire("not valid json {{{")


def _extract_wire(
    mechanism: str, wire: dict
) -> tuple[str | None, dict | None, str | None]:
    """From a mocked wire payload to a candidate `data` dict, PRIOR to
    jsonschema validation -- mirrors what the structured completer does at the
    extraction step (Section 3). Returns
    (raw_text_for_repair, parsed_data_or_None, parse_error_or_None)."""
    if mechanism == NATIVE_TOOL:
        blocks = wire.get("content", [])
        for block in blocks:
            if block.get("type") == "tool_use" and block.get("name") == "emit":
                return None, block.get("input"), None
        text = next((b.get("text") for b in blocks if b.get("type") == "text"), None)
        return text, None, "no tool_use block in response"
    content = wire["choices"][0]["message"]["content"]
    text = _extract_prose_json(content) if mechanism == PROSE_JSON else content
    try:
        return content, json.loads(text), None
    except (json.JSONDecodeError, TypeError) as e:
        return content, None, f"unparseable structured output: {e}"


def _target_errors(data: Any, schema: dict) -> list[str]:
    """Validate with jsonschema against the original, unmodified schema."""
    return [e.message for e in Draft202012Validator(schema).iter_errors(data)]


# ---------------------------------------------------------------------------
# The oracle: a thin harness interface the structured completer must satisfy.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OracleResult:
    """Shape-mirrors ``StructuredResult`` (``data``, ``method_used``,
    `attempts`). The completer's `response` (the summed-cost `LLMResponse`,
    Section 6) is asserted directly in `tests/test_structured_completer.py`;
    this matrix pins the data/method/attempts contract only."""

    data: dict | None
    method_used: str
    attempts: int


class _ScriptedAdapter:
    """A wire stand-in installed INTO a real ModelRouter: cache/chaos/retry/
    receipts are the ACTUAL llm/router.py code, only the wire is scripted.
    Each outcome is an ``LLMResponse`` or a ``SchemaViolation`` (a parse
    failure the adapter would raise)."""

    def __init__(self, outcomes: list):
        self.outcomes = outcomes
        self._i = 0

    async def complete(self, req: LLMRequest, client) -> LLMResponse:
        outcome = self.outcomes[min(self._i, len(self.outcomes) - 1)]
        self._i += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _run_target(
    mechanism: str, wire_payloads: list[dict], schema: dict, *, repair_attempts: int = 1
) -> OracleResult:
    """The oracle records what the real ``StructuredCompleter.complete()``
    returns for this case. It drives the actual module the matrix was built
    to pin. ``_extract_wire`` still stands in for the adapter's mechanism-
    specific wire-to-data extraction (adapter responsibility, not the
    completer's job); the extracted data (or the parse failure) is fed to the
    completer through a scripted transport, and the completer does the
    validation and bounded repair. It raises ``SchemaViolation`` when
    every attempt (1 clean + `repair_attempts` corrective re-calls) is
    exhausted."""
    outcomes: list = []
    for wire in wire_payloads:
        raw_text, data, parse_error = _extract_wire(mechanism, wire)
        if parse_error:
            outcomes.append(SchemaViolation(parse_error, raw_text=raw_text))
        else:
            outcomes.append(
                LLMResponse(
                    content=raw_text if raw_text is not None else json.dumps(data),
                    data=data,
                    tokens_in=42,
                    tokens_out=17,
                    cost=0.0,
                    model="mock/model",
                    raw={"id": "mock"},
                )
            )
    router = ModelRouter(keys={}, max_retries=3)
    router._adapters["mock"] = _ScriptedAdapter(outcomes)
    result = asyncio.run(
        StructuredCompleter(router).complete(
            StructuredRequest(
                model="mock/model",
                messages=[{"role": "user", "content": "go"}],
                schema=schema,
                method=mechanism,  # explicit mechanism -> method_used echoes it
                repair_attempts=repair_attempts,
            )
        )
    )
    return OracleResult(
        data=result.data, method_used=result.method_used, attempts=result.attempts
    )


# ---------------------------------------------------------------------------
# Schema-shape axes -- one constant per axis, documented.
# ---------------------------------------------------------------------------

# object w/ optional: "age" may be absent even though it's declared.
SCHEMA_OPTIONAL = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name"],
}

# nested object, its own explicit required list one level down.
SCHEMA_NESTED = {
    "type": "object",
    "properties": {
        "user": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "email": {"type": "string"}},
            "required": ["id"],
        }
    },
    "required": ["user"],
}

# array of objects, item schema has its own required list.
SCHEMA_ARRAY = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"sku": {"type": "string"}, "qty": {"type": "integer"}},
                "required": ["sku", "qty"],
            },
        }
    },
    "required": ["items"],
}

# nullable-optional convention (Section 5): the field IS required (strict
# mode demands the key), but its type admits null as the "not applicable"
# sentinel a compliant provider emits instead of omitting the key.
SCHEMA_NULLABLE = {
    "type": "object",
    "properties": {"note": {"type": ["string", "null"]}},
    "required": ["note"],
}

# omitted-required (Section 5's semantics-flip axis): NO "required" key at
# all. Under jsonschema this means nothing is required.
SCHEMA_OMITTED_REQUIRED = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
}

# additionalProperties:false so the extra-properties axis has something to
# violate under the target validator.
SCHEMA_STRICT_NO_EXTRA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# The worked example (spec-mandated): request in, mock wire response,
# expected StructuredResult out -- narrated in full once; the parametrized
# matrix below reuses the identical oracle compactly.
# ---------------------------------------------------------------------------


def test_worked_example_end_to_end():
    """Full trace for one case: mechanism=native_tool, schema=SCHEMA_NESTED,
    a REPAIR sequence (first attempt bad, second attempt corrected) --
    exercising the mechanism axis, the shape axis, AND the attempts/repair
    axis in one narrated example."""
    schema = SCHEMA_NESTED
    # Attempt 1: model forgets the nested "id" -- a real wire response.
    bad_wire = _anthropic_wire({"user": {"email": "a@b.com"}})
    # Attempt 2: corrective turn lands; model resends with "id" included.
    good_wire = _anthropic_wire({"user": {"id": "u1", "email": "a@b.com"}})

    result = _run_target(NATIVE_TOOL, [bad_wire, good_wire], schema, repair_attempts=1)

    assert result.data == {"user": {"id": "u1", "email": "a@b.com"}}
    assert result.method_used == NATIVE_TOOL
    assert result.attempts == 2  # 1 clean-attempt slot consumed bad, repair succeeded

    # Consistency check the rest of CASES is held to: the SAME oracle, given
    # only the first (bad) wire payload and repair_attempts=0 (fail-hard),
    # must raise SchemaViolation naming the missing field.
    with pytest.raises(SchemaViolation, match="id"):
        _run_target(NATIVE_TOOL, [bad_wire], schema, repair_attempts=0)


# ---------------------------------------------------------------------------
# The matrix: mechanisms x shape axes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConformanceCase:
    id: str
    mechanism: str
    wire_payloads: list[dict]
    schema: dict
    expect_raises: bool
    expected_data: dict | None = None
    repair_attempts: int = 1


def _valid_axes() -> list[tuple[str, dict, dict]]:
    return [
        ("object_optional", SCHEMA_OPTIONAL, {"name": "Ada"}),
        ("nested", SCHEMA_NESTED, {"user": {"id": "u1"}}),
        ("array", SCHEMA_ARRAY, {"items": [{"sku": "A1", "qty": 2}]}),
        ("nullable_optional_value", SCHEMA_NULLABLE, {"note": "hi"}),
        ("nullable_optional_null", SCHEMA_NULLABLE, {"note": None}),
        # omitted-required: TARGET treats this as valid (nothing required) --
        # the flip's "future" half. The "today" half is pinned separately in
        # TestOmittedRequiredFlip using the real adapter code.
        ("omitted_required", SCHEMA_OMITTED_REQUIRED, {"name": "Ada"}),
    ]


def _invalid_axes() -> list[tuple[str, dict, dict]]:
    return [
        ("nested_missing_required", SCHEMA_NESTED, {"user": {"email": "a@b.com"}}),
        ("array_item_missing_required", SCHEMA_ARRAY, {"items": [{"sku": "A1"}]}),
        ("nullable_optional_key_omitted", SCHEMA_NULLABLE, {}),
        ("missing_required", SCHEMA_OPTIONAL, {"age": 5}),
        ("type_violation", SCHEMA_OPTIONAL, {"name": "Ada", "age": "old"}),
        ("extra_properties", SCHEMA_STRICT_NO_EXTRA, {"name": "Ada", "extra": "nope"}),
    ]


def _build_matrix() -> list[ConformanceCase]:
    cases: list[ConformanceCase] = []
    for mechanism in MECHANISMS:
        for axis, schema, data in _valid_axes():
            cases.append(
                ConformanceCase(
                    id=f"{mechanism}:{axis}",
                    mechanism=mechanism,
                    wire_payloads=[_wire_for(mechanism, data)],
                    schema=schema,
                    expect_raises=False,
                    expected_data=data,
                )
            )
        for axis, schema, data in _invalid_axes():
            cases.append(
                ConformanceCase(
                    id=f"{mechanism}:{axis}",
                    mechanism=mechanism,
                    wire_payloads=[_wire_for(mechanism, data)],
                    schema=schema,
                    expect_raises=True,
                    repair_attempts=0,  # fail-hard: prove the FIRST attempt is bad
                )
            )
        cases.append(
            ConformanceCase(
                id=f"{mechanism}:unparseable",
                mechanism=mechanism,
                wire_payloads=[_unparseable_wire(mechanism)],
                schema=SCHEMA_OPTIONAL,
                expect_raises=True,
                repair_attempts=0,
            )
        )

    # prose_json-only: fenced vs. unfenced extraction (Section 2's two shapes).
    fenced = _openai_wire('```json\n{"name": "Ada"}\n```')
    unfenced = _openai_wire('The answer is {"name": "Ada"} -- hope that helps.')
    cases.append(
        ConformanceCase(
            id="prose_json:fenced_extraction",
            mechanism=PROSE_JSON,
            wire_payloads=[fenced],
            schema=SCHEMA_OPTIONAL,
            expect_raises=False,
            expected_data={"name": "Ada"},
        )
    )
    cases.append(
        ConformanceCase(
            id="prose_json:unfenced_extraction",
            mechanism=PROSE_JSON,
            wire_payloads=[unfenced],
            schema=SCHEMA_OPTIONAL,
            expect_raises=False,
            expected_data={"name": "Ada"},
        )
    )

    # attempts/repair axis (Section 6): one success-on-repair case, one
    # exhausted-after-repair case, distinct from the worked example's.
    bad = _wire_for(JSON_LOOSE, {"age": 5})  # missing required "name"
    good = _wire_for(JSON_LOOSE, {"name": "Ada"})
    cases.append(
        ConformanceCase(
            id="json_loose:repair_succeeds",
            mechanism=JSON_LOOSE,
            wire_payloads=[bad, good],
            schema=SCHEMA_OPTIONAL,
            expect_raises=False,
            expected_data={"name": "Ada"},
            repair_attempts=1,
        )
    )
    cases.append(
        ConformanceCase(
            id="json_loose:repair_exhausted",
            mechanism=JSON_LOOSE,
            wire_payloads=[bad, bad],
            schema=SCHEMA_OPTIONAL,
            expect_raises=True,
            repair_attempts=1,
        )
    )
    return cases


CASES = _build_matrix()


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_matrix(case: ConformanceCase):
    if case.expect_raises:
        with pytest.raises(SchemaViolation):
            _run_target(
                case.mechanism,
                case.wire_payloads,
                case.schema,
                repair_attempts=case.repair_attempts,
            )
        return
    result = _run_target(
        case.mechanism,
        case.wire_payloads,
        case.schema,
        repair_attempts=case.repair_attempts,
    )
    assert result.data == case.expected_data
    assert result.method_used == case.mechanism
    assert result.attempts == len(case.wire_payloads)


def test_matrix_covers_every_mechanism_and_axis():
    """Sanity gate on the matrix itself: every mechanism appears, and the
    omitted-required semantics-flip axis is present for all four."""
    seen_mechanisms = {c.mechanism for c in CASES}
    assert seen_mechanisms == set(MECHANISMS)
    omitted_required_ids = {c.id for c in CASES if c.id.endswith(":omitted_required")}
    assert omitted_required_ids == {f"{m}:omitted_required" for m in MECHANISMS}


# ---------------------------------------------------------------------------
# ``_validate_required`` is retired: there is no adapter-level behavior to pin
# against jsonschema's target -- the completer's jsonschema validator IS the
# only validator now, for every mechanism. This class is FLIPPED (not
# deleted) to assert directly that an omitted ``required`` key
# means NOTHING is required (jsonschema semantics), full stop, pinned through
# the real `_run_target`/completer path so a regression that reintroduces an
# implicit all-required default is still caught.
# ---------------------------------------------------------------------------


class TestOmittedRequiredFlip:
    def test_omitted_required_means_nothing_required(self):
        """``SCHEMA_OMITTED_REQUIRED`` has no ``required`` key at
        all, so jsonschema (the completer's ONLY validator now) requires
        nothing -- "age" may be absent without raising, through both the
        oracle (`_target_errors`) and the real completer (`_run_target`)."""
        data = {"name": "Ada"}  # "age" is a declared property, absent here
        assert _target_errors(data, SCHEMA_OMITTED_REQUIRED) == []
        result = _run_target(
            JSON_LOOSE, [_wire_for(JSON_LOOSE, data)], SCHEMA_OMITTED_REQUIRED
        )
        assert result.data == data

    def test_explicit_required_still_enforced(self):
        """Control case: an EXPLICIT `required` list (SCHEMA_OPTIONAL requires
        "name") is unaffected by the retirement -- only OMISSION flipped."""
        data = {"age": 5}  # "name" is required and absent
        assert _target_errors(data, SCHEMA_OPTIONAL) != []
        with pytest.raises(SchemaViolation, match="name"):
            _run_target(
                JSON_LOOSE,
                [_wire_for(JSON_LOOSE, data)],
                SCHEMA_OPTIONAL,
                repair_attempts=0,
            )


# ---------------------------------------------------------------------------
# TestCurrentLayerPinned -- drives the REAL, currently-shipped adapters.py
# code (not the oracle) through ``httpx.MockTransport``. With
# ``_validate_required`` retired, the
# adapter layer no longer validates ANYTHING (not presence, not type, not
# additionalProperties) for any mechanism, including `native_tool` (which
# never validated). Every divergence this class used to pin
# between "today's adapter" and "the target completer" has therefore
# COLLAPSED: the adapter is uniformly permissive, and only `_run_target`
# (the real completer) enforces the schema. This class is FLIPPED to assert
# that collapse explicitly, per-axis, so a regression that reintroduces
# adapter-level validation (re-colliding with the completer's, the shape of
# the original snippet-refine bug) is still caught.
# ---------------------------------------------------------------------------


def _run_openai_adapter(adapter: OpenAICompatAdapter, req: LLMRequest, wire: dict):
    async def _go():
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=wire)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.complete(req, client)

    return asyncio.run(_go())


def _run_anthropic_adapter(adapter: AnthropicAdapter, req: LLMRequest, wire: dict):
    async def _go():
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=wire)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.complete(req, client)

    return asyncio.run(_go())


def _req(schema: dict) -> LLMRequest:
    return LLMRequest(
        model="test/model", messages=[{"role": "user", "content": "hi"}], schema=schema
    )


class TestCurrentLayerPinned:
    def test_omitted_required_passes_adapter_and_target_agrees(self):
        """The adapter no longer validates, so an omitted
        "age" passes it -- and jsonschema's target agrees (nothing required
        when `required` is omitted), so the two no longer diverge here."""
        payload = {"name": "Ada"}  # "age" omitted
        # "test/model" (below, via _req) has no capability-table row, so the
        # conservative fallback (JSON_LOOSE) applies.
        adapter = OpenAICompatAdapter("k", "https://example.invalid/v1")
        resp = _run_openai_adapter(
            adapter, _req(SCHEMA_OMITTED_REQUIRED), _wire_for(JSON_LOOSE, payload)
        )
        assert resp.data == payload  # adapter: unvalidated pass-through
        assert _target_errors(payload, SCHEMA_OMITTED_REQUIRED) == []

    def test_anthropic_native_tool_skips_validation_at_the_adapter(self):
        """adapters.py never validated `native_tool` output (the #1 hole);
        retiring adapter validation changes nothing about that fact at the adapter layer; the
        completer (tests/test_structured_completer.py) is what closes it."""
        payload = {"age": 5}  # SCHEMA_OPTIONAL requires "name"; absent here
        adapter = AnthropicAdapter("k")
        resp = _run_anthropic_adapter(
            adapter, _req(SCHEMA_OPTIONAL), _anthropic_wire(payload)
        )
        assert resp.data == payload  # adapter: silently accepted, unchanged
        # The completer validates the Anthropic payload:
        # this same payload raises once jsonschema validates native_tool too.
        assert _target_errors(payload, SCHEMA_OPTIONAL) != []

    def test_type_violation_passes_adapter_target_catches_it(self):
        """The adapter never inspected field types; even before retirement,
        `_validate_required` was presence-only -- a wrong-typed-but-present
        field passes the adapter; only the target (completer) catches it."""
        payload = {"name": "Ada", "age": "old"}  # age should be an integer
        adapter = OpenAICompatAdapter("k", "https://example.invalid/v1")
        resp = _run_openai_adapter(
            adapter, _req(SCHEMA_OPTIONAL), _wire_for(JSON_LOOSE, payload)
        )
        assert resp.data == payload  # adapter: type unchecked
        assert (
            _target_errors(payload, SCHEMA_OPTIONAL) != []
        )  # target: jsonschema catches it

    def test_extra_properties_passes_adapter_target_catches_it(self):
        """The adapter never checked ``additionalProperties``;
        -- only the target (completer) does."""
        payload = {"name": "Ada", "extra": "nope"}
        adapter = OpenAICompatAdapter("k", "https://example.invalid/v1")
        resp = _run_openai_adapter(
            adapter, _req(SCHEMA_STRICT_NO_EXTRA), _wire_for(JSON_LOOSE, payload)
        )
        assert resp.data == payload  # adapter: additionalProperties unchecked
        assert _target_errors(payload, SCHEMA_STRICT_NO_EXTRA) != []

    def test_missing_required_passes_adapter_target_catches_it(self):
        """An explicit ``required`` list (``SCHEMA_OPTIONAL`` requires
        "name") used to be enforced by `_validate_required` at the adapter
        layer -- now retired, the adapter passes a bare-missing payload
        through unvalidated too; only the target (completer) still enforces
        it. Only the target completer now enforces this axis."""
        payload = {"age": 5}  # SCHEMA_OPTIONAL requires "name", explicit list
        adapter = OpenAICompatAdapter("k", "https://example.invalid/v1")
        resp = _run_openai_adapter(
            adapter, _req(SCHEMA_OPTIONAL), _wire_for(JSON_LOOSE, payload)
        )
        assert resp.data == payload  # adapter: no longer validates at all
        assert _target_errors(payload, SCHEMA_OPTIONAL) != []

    def test_unparseable_raises_today_and_target_unchanged(self):
        """Section 5: the json.loads failure path is explicitly UNCHANGED by
        the contract -- both today's adapter and the target oracle raise."""
        # "test/model" (below, via _req) has no capability-table row, so the
        # conservative fallback (JSON_LOOSE) applies -- byte-identical to the
        # retired strict_schema=False constructor arg this test used to pass.
        adapter = OpenAICompatAdapter("k", "https://example.invalid/v1")
        with pytest.raises(SchemaViolation, match="unparseable"):
            _run_openai_adapter(
                adapter, _req(SCHEMA_OPTIONAL), _unparseable_wire(JSON_LOOSE)
            )
        with pytest.raises(SchemaViolation):
            _run_target(
                JSON_LOOSE,
                [_unparseable_wire(JSON_LOOSE)],
                SCHEMA_OPTIONAL,
                repair_attempts=0,
            )

    def test_no_tool_use_block_is_left_to_structured_validation(self):
        """The adapter parses successful output; the completer owns rejection."""
        adapter = AnthropicAdapter("k")
        wire = _unparseable_wire(NATIVE_TOOL)
        response = _run_anthropic_adapter(adapter, _req(SCHEMA_OPTIONAL), wire)
        assert response.data is None
        with pytest.raises(SchemaViolation):
            _run_target(NATIVE_TOOL, [wire], SCHEMA_OPTIONAL, repair_attempts=0)
