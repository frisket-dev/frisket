from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from frisket.ai.llm import LLMResponse, ModelRouter, RemediatedError, classify_llm_error
from frisket.ai.llm.adapters import OpenAICompatAdapter
from frisket.ai.llm.remediation import INVALID_OUTPUT
from frisket.ai.llm.types import SchemaViolation
from frisket.engine.sandbox.broker import (
    BROKER_MAX_REPAIR_ATTEMPTS,
    CLIENT_SNIPPET,
    KeyBroker,
    _clamp_repair_attempts,
)
from frisket.engine.sandbox.shim import run_python_op


@pytest.fixture
def sock_dir():
    """AF_UNIX paths cap at ~104 chars on macOS; pytest tmp_path is too deep.
    Windows has no /tmp (this file isn't in the Windows workflow selection,
    but keep it portable for consistency with test_sandbox.py's fixture --
    portability requirement)."""
    base_dir = tempfile.gettempdir() if os.name == "nt" else "/tmp"
    with tempfile.TemporaryDirectory(dir=base_dir, prefix="fk-") as d:
        yield Path(d)


# ---------------------------------------------------------------------------
# Grep guards: ``_validate_required`` is retired; ``_strictify`` applies only
# to native-strict wire schemas because loose-mode validation uses the original.
# ---------------------------------------------------------------------------


def _run_openai_adapter_capture_body(
    mechanism_field: dict, schema: dict | None = None
) -> dict:
    """Drive OpenAICompatAdapter.complete through a MockTransport, returning
    the JSON body the adapter actually sent -- so we can inspect whether the
    schema in the body was strictified."""
    from frisket.ai.llm.types import LLMRequest

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "mock",
                "choices": [
                    {
                        "message": {"content": json.dumps({"name": "Ada"})},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    if schema is None:
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name"],
        }
    req = LLMRequest(
        model="test/model",
        messages=[{"role": "user", "content": "hi"}],
        schema=schema,
        **mechanism_field,
    )
    adapter = OpenAICompatAdapter("k", "https://example.invalid/v1")

    async def _go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await adapter.complete(req, client)

    asyncio.run(_go())
    return captured["body"]


def test_strictify_only_applied_to_native_strict_wire_body():
    """Dynamic proof: a NATIVE_STRICT request's wire body carries a
    strictified schema (additionalProperties:false, all-required); a
    JSON_LOOSE request's body -- schema-in-prompt -- carries the ORIGINAL,
    never-strictified schema. Validation must run against the
    original schema, not the strictified one."""
    strict_body = _run_openai_adapter_capture_body({"mechanism": "native_strict"})
    schema_sent = strict_body["response_format"]["json_schema"]["schema"]
    assert schema_sent["additionalProperties"] is False
    assert set(schema_sent["required"]) == {"name", "age"}  # strictified: ALL required

    loose_body = _run_openai_adapter_capture_body({"mechanism": "json_loose"})
    assert "response_format" in loose_body
    assert loose_body["response_format"] == {"type": "json_object"}
    prompt = loose_body["messages"][0]["content"]
    embedded_schema = json.loads(prompt.split("schema:\n", 1)[1])
    assert embedded_schema["required"] == ["name"]  # ORIGINAL, not strictified
    assert "additionalProperties" not in embedded_schema


def _object_nodes_missing_closed_marker(node: Any, path: tuple = ()) -> list[tuple]:
    """Every object node in `node` whose `additionalProperties` is not
    exactly False, by JSON-pointer-ish path. OpenAI's structured-outputs API
    requires the marker "to be supplied and to be false" on EVERY object
    node, not only the root."""
    bad: list[tuple] = []
    if isinstance(node, dict):
        kind = node.get("type")
        if kind == "object" or (isinstance(kind, list) and "object" in kind):
            if node.get("additionalProperties") is not False:
                bad.append(path)
        for key, value in node.items():
            bad.extend(_object_nodes_missing_closed_marker(value, path + (key,)))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            bad.extend(_object_nodes_missing_closed_marker(value, path + (i,)))
    return bad


def test_native_strict_closes_every_object_node_in_the_tree():
    """The live 400 this reproduces, verbatim from an `openai/gpt-5-mini`
    `map.extract` run against the real API:

        Invalid schema for response_format 'result': In
        context=('properties','person'), 'additionalProperties' is required
        to be supplied and to be false.

    `map.extract`'s grounded schema (the typed ``extract`` prompt's
    ``response_schema``) OPENS each per-field wrapper and each evidence item
    with an explicit `additionalProperties: True`, so that page/bbox/snippet
    can ride along on the OCR/PDF path. `_strictify` used `setdefault`, which
    preserved that `True` all the way to the wire and 400'd EVERY OpenAI GPT
    model. The wire body must close every object node; validation still runs
    against the original, open schema (asserted above).

    Recorded fixture, not a live call: the real action's schema driven
    through the real adapter over a MockTransport.
    """
    from frisket.actions.extract import ExtractParams, extract
    from frisket.actions.types import Row

    params = ExtractParams.model_validate(
        {
            "source": ["document"],
            "model": "openai/gpt-5-mini",
            "fields": [
                {"name": "person", "type": "text"},
                {"name": "moments", "type": "list", "items": {"type": "string"}},
            ],
            "grounding": {"enabled": True},
        }
    )
    schema = extract(params, Row({"document": "Ada met Babbage."})).response_schema
    # Precondition: the ORIGINAL schema really is open at the node OpenAI
    # named, so this test would go red again if _strictify stopped closing it.
    assert schema["properties"]["person"]["additionalProperties"] is True

    body = _run_openai_adapter_capture_body(
        {"mechanism": "native_strict"}, schema=schema
    )
    sent = body["response_format"]["json_schema"]["schema"]
    assert _object_nodes_missing_closed_marker(sent) == []
    # ...and the caller's schema was not mutated on the way through: the
    # loose/prose bodies and Section 5 validation still read the open one.
    assert schema["properties"]["person"]["additionalProperties"] is True


def test_native_strict_closes_object_nodes_without_properties():
    """A bare `{"type": "object"}` and a NULLABLE object (`["object","null"]`)
    are object nodes too; strict mode demands the marker on both, and the old
    `type == "object" and "properties" in node` guard skipped both."""
    schema = {
        "type": "object",
        "properties": {
            "freeform": {"type": "object"},
            "maybe": {
                "type": ["object", "null"],
                "properties": {"n": {"type": "integer"}},
            },
        },
        "required": ["freeform", "maybe"],
    }
    body = _run_openai_adapter_capture_body(
        {"mechanism": "native_strict"}, schema=schema
    )
    sent = body["response_format"]["json_schema"]["schema"]
    assert _object_nodes_missing_closed_marker(sent) == []


# ---------------------------------------------------------------------------
# The broker routes schema-bearing requests through the completer.
# ---------------------------------------------------------------------------


class _RepairingAdapter:
    """A fake provider installed on the router: first call returns a
    schema-invalid payload (missing "name"), second call returns a valid one.
    A raw `router.complete()` has NO repair loop, so it would surface the
    first attempt's SchemaViolation immediately; only routing through
    `StructuredCompleter` (the completer change) makes this sequence succeed."""

    def __init__(self):
        self.calls = 0

    async def complete(self, req: Any, client: Any) -> LLMResponse:
        self.calls += 1
        data = {"age": 5} if self.calls == 1 else {"name": "Ada", "age": 5}
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            tokens_in=10,
            tokens_out=5,
            cost=0.0,
            model=req.model,
        )


SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name"],
}


def test_broker_routes_schema_bearing_request_through_completer_and_repairs(
    sock_dir,
):
    async def run():
        router = ModelRouter(keys={"anthropic": "k"})
        adapter = _RepairingAdapter()
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        sock = str(sock_dir / "broker.sock")
        broker = KeyBroker(router, sock)
        await broker.start()
        try:
            code = CLIENT_SNIPPET + (
                "\nimport json\n"
                "resp = frisket_complete('anthropic/claude-haiku-4-5',"
                " [{'role': 'user', 'content': 'go'}],"
                f" schema={SCHEMA!r}, repair_attempts=1)\n"
                "print(json.dumps({'data': resp['data']}))\n"
            )
            return await run_python_op(code, {}, broker_endpoint=broker.endpoint)
        finally:
            await broker.stop()
            await router.aclose()

    out = asyncio.run(run())
    assert out["data"] == {"name": "Ada", "age": 5}


def test_broker_schemaless_request_still_bypasses_completer(sock_dir):
    """A request with no schema stays on the direct ``router.complete`` path.
    Schemaless callers stay direct by design. ``_RepairingAdapter`` returns a
    schema-shaped payload missing "name" on its FIRST call; if this request
    went through the completer, it would trigger a repair (a second wire
    call). It doesn't: exactly one wire call, and the raw first-call payload
    passes through unvalidated."""
    adapter = _RepairingAdapter()

    async def run():
        router = ModelRouter(keys={"anthropic": "k"})
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        sock = str(sock_dir / "broker.sock")
        broker = KeyBroker(router, sock)
        await broker.start()
        try:
            code = CLIENT_SNIPPET + (
                "\nimport json\n"
                "resp = frisket_complete('anthropic/claude-haiku-4-5',"
                " [{'role': 'user', 'content': 'go'}])\n"
                "print(json.dumps({'data': resp['data']}))\n"
            )
            return await run_python_op(code, {}, broker_endpoint=broker.endpoint)
        finally:
            await broker.stop()
            await router.aclose()

    out = asyncio.run(run())
    # The FIRST (uncorrected, schema-invalid-shaped) payload passes through
    # verbatim -- no completer, no repair, no validation at all.
    assert out["data"] == {"age": 5}
    assert adapter.calls == 1


# ---------------------------------------------------------------------------
# Repair-attempt clamp for untrusted operation callers.
# ---------------------------------------------------------------------------


def test_clamp_repair_attempts_pure_function():
    assert _clamp_repair_attempts(None) == 1
    assert _clamp_repair_attempts(0) == 0
    assert _clamp_repair_attempts(1) == 1
    assert (
        _clamp_repair_attempts(BROKER_MAX_REPAIR_ATTEMPTS) == BROKER_MAX_REPAIR_ATTEMPTS
    )
    assert _clamp_repair_attempts(999999) == BROKER_MAX_REPAIR_ATTEMPTS
    assert _clamp_repair_attempts(-5) == 0


class _AlwaysBadAdapter:
    """Every wire call returns a schema-invalid payload -- repair never
    succeeds, so the total number of wire calls made directly measures how
    many repair attempts the completer actually ran."""

    def __init__(self):
        self.calls = 0

    async def complete(self, req: Any, client: Any) -> LLMResponse:
        self.calls += 1
        data = {"age": 5}  # always missing "name"
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            tokens_in=1,
            tokens_out=1,
            cost=0.0,
            model=req.model,
        )


def test_broker_clamps_repair_attempts_server_side(sock_dir):
    """An op requesting an absurdly large `repair_attempts` is clamped to
    `BROKER_MAX_REPAIR_ATTEMPTS` -- the total wire calls made is bounded by
    the clamp (+1 for the initial attempt), never the requested number."""
    adapter = _AlwaysBadAdapter()

    async def run():
        router = ModelRouter(keys={"anthropic": "k"})
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        sock = str(sock_dir / "broker.sock")
        broker = KeyBroker(router, sock)
        await broker.start()
        try:
            code = CLIENT_SNIPPET + (
                "\ntry:\n"
                "    frisket_complete('anthropic/claude-haiku-4-5',"
                " [{'role': 'user', 'content': 'go'}],"
                f" schema={SCHEMA!r}, repair_attempts=999999)\n"
                "    print('{\"raised\": false}')\n"
                "except RuntimeError as e:\n"
                "    import json; print(json.dumps({'raised': True}))\n"
            )
            return await run_python_op(code, {}, broker_endpoint=broker.endpoint)
        finally:
            await broker.stop()
            await router.aclose()

    out = asyncio.run(run())
    assert out["raised"] is True  # repair never succeeds -- exhausts and fails
    # 1 clean attempt + BROKER_MAX_REPAIR_ATTEMPTS repairs, never 999999+1.
    assert adapter.calls == BROKER_MAX_REPAIR_ATTEMPTS + 1


# ---------------------------------------------------------------------------
# Broker-supplied invalid schema -> SchemaViolation, not a crash.
# ---------------------------------------------------------------------------


def test_broker_wraps_structurally_invalid_schema_as_schema_violation(sock_dir):
    """A structurally malformed op-supplied schema (`type` set to a value the
    JSON Schema meta-schema rejects) raises `jsonschema.exceptions.SchemaError`
    inside the completer, wrapped to `SchemaViolation` -- the broker's
    existing `except LLMError` branch handles it cleanly (ok: false), the
    connection handler does not crash, and the client sees a RuntimeError
    with a readable message rather than a dropped/reset connection."""
    bad_schema = {"type": "not-a-real-json-schema-type"}

    async def run():
        router = ModelRouter(keys={"anthropic": "k"})
        router._adapters["anthropic"] = _AlwaysBadAdapter()  # never reached
        sock = str(sock_dir / "broker.sock")
        broker = KeyBroker(router, sock)
        await broker.start()
        try:
            code = CLIENT_SNIPPET + (
                "\ntry:\n"
                "    frisket_complete('anthropic/claude-haiku-4-5',"
                " [{'role': 'user', 'content': 'go'}],"
                f" schema={bad_schema!r})\n"
                "    print('{\"raised\": false}')\n"
                "except RuntimeError as e:\n"
                "    import json; print(json.dumps({'raised': True, 'msg': str(e)[:80]}))\n"
            )
            return await run_python_op(code, {}, broker_endpoint=broker.endpoint)
        finally:
            await broker.stop()
            await router.aclose()

    out = asyncio.run(run())
    assert out["raised"] is True
    assert "invalid schema" in out["msg"]


# ---------------------------------------------------------------------------
# SchemaViolation also covers structural faults that have nothing to do with
# an LLM's wire output. This stage doesn't touch remediation's classification
# -- confirm that.
# ---------------------------------------------------------------------------


def test_structural_schema_violation_is_classified_invalid_output():
    exc = SchemaViolation("structural metadata is missing")
    result = classify_llm_error(exc)
    assert isinstance(result, RemediatedError)
    assert result.code == INVALID_OUTPUT
