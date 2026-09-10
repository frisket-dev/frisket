"""Provider adapters. Two wire shapes cover the supported model families:
- AnthropicAdapter: native Messages API; structured output via forced tool use.
- OpenAICompatAdapter: OpenAI chat completions dialect — covers OpenAI, Gemini
  (their /openai/ compatibility endpoint), OpenRouter, and Ollama/LM Studio.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from frisket.redaction import canonical_error_code, redact_text

from . import structured_capabilities as capabilities
from .pricing import cost_of_with_source
from .structured import _extract_prose_json
from .structured_capabilities import NATIVE_STRICT, PROSE_JSON
from .types import LLMError, LLMRequest, LLMResponse, SchemaViolation

TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def _resolve_mechanism(req: LLMRequest) -> str:
    """The wire mechanism for this request: the explicit
    ``LLMRequest.mechanism`` when the caller set one, else the capability
    table's resolution for
    ``req.model`` for every mechanism-less request -- the
    table is the single source now that the per-adapter ``strict_schema``
    constructor booleans are retired."""
    return req.mechanism or capabilities.resolve(req.model).structured_mode


def _parse_structured(text: str, schema: dict | None) -> dict | None:
    """Wire-level JSON parse ONLY; the old presence-only required-field check
    this used to call is retired. Schema validation
    (presence, type, ``additionalProperties``, the omitted-required
    semantics, ...) is now EXCLUSIVELY the completer's
    job (``llm/structured.py``'s ``jsonschema.Draft202012Validator``, run
    uniformly across every mechanism including ``native_tool`` — the
    Anthropic hole this used to leave open). The ``json.loads`` failure path
    is unchanged: an unparseable wire
    text still raises ``SchemaViolation`` here — there is no meaningful
    ``data`` to hand the completer if the text isn't even JSON."""
    if schema is None:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise SchemaViolation(
            f"unparseable structured output: {e}", raw_text=text
        ) from e


class AnthropicAdapter:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        no_temperature_prefixes: tuple[str, ...] = (),
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.no_temperature_prefixes = no_temperature_prefixes

    async def complete(self, req: LLMRequest, client: httpx.AsyncClient) -> LLMResponse:
        model = req.model.split("/", 1)[-1]
        system_parts = [m["content"] for m in req.messages if m["role"] == "system"]
        messages = [
            {"role": m["role"], "content": self._content(m["content"])}
            for m in req.messages
            if m["role"] != "system"
        ]
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens,
            "messages": messages,
        }
        if not model.startswith(self.no_temperature_prefixes or ("\x00",)):
            body["temperature"] = req.temperature
        if system_parts:
            body["system"] = "\n".join(
                p if isinstance(p, str) else json.dumps(p) for p in system_parts
            )
        # Mechanism resolution mirrors OpenAICompatAdapter's (`_resolve_mechanism`,
        # module-level above) so an explicit `req.mechanism=PROSE_JSON` override
        # actually changes this wire body instead of being silently ignored. "auto"
        # (no explicit mechanism) still resolves to NATIVE_TOOL for anthropic/*
        # models (capabilities.SEED), so the default forced-tool body below is
        # unchanged for every caller that doesn't pin PROSE_JSON explicitly.
        mechanism = _resolve_mechanism(req) if req.schema is not None else None
        if req.schema is not None and mechanism == PROSE_JSON:
            # The Messages API has no `response_format` concept at all (no
            # json_schema strict mode, no json_object mode) -- PROSE_JSON's
            # wire shape here is "no tools, ask in the system prompt", the
            # only mechanism this dialect can express besides NATIVE_TOOL
            # (capabilities.py's `dialect_mechanisms`).
            instruction = "Respond ONLY with JSON matching this schema:\n" + json.dumps(
                req.schema
            )
            body["system"] = (
                f"{body['system']}\n\n{instruction}" if system_parts else instruction
            )
        elif req.schema is not None:
            body["tools"] = [
                {
                    "name": "emit",
                    "description": "Emit the structured result.",
                    "input_schema": req.schema,
                }
            ]
            body["tool_choice"] = {"type": "tool", "name": "emit"}
        elif req.tools:
            # Arbitrary multi-tool defs, tool_choice=auto (not forced).
            # Mutually exclusive with `schema` (LLMRequest docstring): a schema call is
            # the completer's single forced "emit" tool; a tools call is
            # FrisketRouterModel's function-tool dispatch for a pydantic-ai Agent.
            body["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters")
                    or {"type": "object", "properties": {}},
                }
                for t in req.tools
            ]
            body["tool_choice"] = {"type": "auto"}

        resp = await client.post(
            f"{self.base_url}/v1/messages",
            json=body,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=TIMEOUT,
        )
        _raise_for_status(resp, secret_values=(self.api_key,))
        out = _response_object(resp, "completion", self.api_key)
        usage = _object_or_empty(out.get("usage"), "completion usage", self.api_key)
        tokens_in = _usage_count(usage, "input_tokens", self.api_key)
        tokens_out = _usage_count(usage, "output_tokens", self.api_key)
        data = None
        content = None
        # Named (non-"emit") tool_use blocks round-trip to `tool_calls`
        # when this request carried `tools` -- collected as a list even though
        # today's callers dispatch one tool per turn, since Anthropic's wire
        # protocol allows multiple tool_use blocks in a single response
        # (parallel tool calls).
        tool_calls = [] if req.tools else None
        for block in out["content"]:
            btype = block["type"]
            if btype == "tool_use":
                name = block["name"]
                if req.schema is not None and name == "emit":
                    data = block["input"]
                elif tool_calls is not None:
                    tool_calls.append(
                        {
                            "name": name,
                            "args": block["input"],
                            "id": block["id"],
                        }
                    )
            elif btype == "text":
                content = block["text"]
        if req.schema is not None and mechanism == PROSE_JSON:
            # No tool_use block to read `data` off -- the schema was asked
            # for in prose (this branch's whole point). Same tolerant
            # fenced/unfenced extraction OpenAICompatAdapter's PROSE_JSON
            # path uses; an unparseable/no-match result is a real
            # SchemaViolation, not a silent None (mirrors `_parse_structured`).
            data = _parse_structured(
                _extract_prose_json(content) if content is not None else "", req.schema
            )
        if tool_calls is not None and not tool_calls:
            tool_calls = None  # "no calls this turn" stays None, not []
        stop_reason = out.get("stop_reason")
        cost, cost_source = cost_of_with_source(req.model, tokens_in, tokens_out)
        return LLMResponse(
            content=content,
            data=data,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            model=req.model,
            cost_source=cost_source,
            raw={"id": out.get("id"), "stop_reason": stop_reason},
            tool_calls=tool_calls,
            output_limited=stop_reason == "max_tokens",
        )

    @staticmethod
    def _content(content: Any) -> Any:
        if isinstance(content, str):
            return content
        parts = []
        for p in content:
            if p.get("type") == "text":
                parts.append({"type": "text", "text": p["text"]})
            elif p.get("type") == "image":
                parts.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": p["media_type"],
                            "data": p["data"],
                        },
                    }
                )
        return parts


class OpenAICompatAdapter:
    """OpenAI chat-completions dialect. Wire body shaped per-request by the
    resolved mechanism: ``NATIVE_STRICT`` (strict
    ``json_schema``), ``JSON_LOOSE`` (``json_object`` + schema-in-prompt), or
    ``PROSE_JSON`` (no ``response_format`` at all, for models/providers that
    reject it outright -- the same schema-in-prompt instruction as
    ``JSON_LOOSE``, extracted from prose on the way back in). The mechanism
    is ``req.mechanism`` when the caller set one, else the capability
    table's resolution for ``req.model`` (:func:`_resolve_mechanism`) -- the
    table is the single source now; the old per-adapter ``strict_schema``
    constructor boolean is retired."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        max_tokens_param: str = "max_tokens",
        no_temperature_prefixes: tuple[str, ...] = (),
        image_url_as_string: bool = False,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_tokens_param = max_tokens_param
        # reasoning-model families that reject an explicit temperature
        self.no_temperature_prefixes = no_temperature_prefixes
        # Ollama's OpenAI-compatible chat endpoint documents the image_url
        # value as the data-URL string itself. OpenAI and the other compatible
        # providers use the nested {"url": ...} object, so keep that as the
        # default and select Ollama's wire variant at adapter construction.
        self.image_url_as_string = image_url_as_string

    async def complete(
        self,
        req: LLMRequest,
        client: httpx.AsyncClient,
        *,
        cost_model: str | None = None,
    ) -> LLMResponse:
        model = req.model.split("/", 1)[-1]
        messages = [
            {"role": m["role"], "content": self._content(m["content"])}
            for m in req.messages
        ]
        body: dict[str, Any] = {
            "model": model,
            self.max_tokens_param: req.max_tokens,
            "messages": messages,
        }
        if req.reasoning_policy == "disabled":
            body["reasoning"] = {"enabled": False}
        elif req.reasoning_policy == "low_exclude":
            body["reasoning"] = {"effort": "low", "exclude": True}
        if not model.startswith(self.no_temperature_prefixes or ("\x00",)):
            body["temperature"] = req.temperature
        top_p = req.params.get("top_p")
        if top_p is not None:
            if (
                isinstance(top_p, bool)
                or not isinstance(top_p, (int, float))
                or not 0.0 <= float(top_p) <= 1.0
            ):
                raise LLMError("top_p must be a number between 0 and 1")
            body["top_p"] = float(top_p)
        mechanism = _resolve_mechanism(req) if req.schema is not None else None
        if req.schema is not None:
            if mechanism == NATIVE_STRICT:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "result",
                        "strict": True,
                        "schema": _strictify(req.schema),
                    },
                }
            else:
                # JSON_LOOSE and PROSE_JSON share the identical instruction
                # text; PROSE_JSON's one distinguishing property is
                # the ABSENCE of response_format below.
                if mechanism != PROSE_JSON:
                    body["response_format"] = {"type": "json_object"}
                messages.insert(
                    0,
                    {
                        "role": "system",
                        "content": "Respond ONLY with JSON matching this schema:\n"
                        + json.dumps(req.schema),
                    },
                )
        elif req.tools:
            # Arbitrary multi-tool defs, tool_choice="auto" -- mirrors the
            # Anthropic branch above; mutually exclusive with `schema`.
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters")
                        or {"type": "object", "properties": {}},
                    },
                }
                for t in req.tools
            ]
            body["tool_choice"] = "auto"

        resp = await client.post(
            f"{self.base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=TIMEOUT,
        )
        _raise_for_status(resp, secret_values=(self.api_key,))
        out = _response_object(resp, "completion", self.api_key)
        usage = _object_or_empty(out.get("usage"), "completion usage", self.api_key)
        tokens_in = _usage_count(usage, "prompt_tokens", self.api_key)
        tokens_out = _usage_count(usage, "completion_tokens", self.api_key)
        choice = out["choices"][0]
        message = choice["message"]
        content = message.get("content")
        # PROSE_JSON: the wire text is free prose that may wrap the JSON in a
        # fence or surrounding sentences -- extract before parsing.
        # `content` on the returned LLMResponse stays the RAW wire text (the
        # repair loop's corrective turn echoes it verbatim, same as every
        # other mechanism).
        text_for_parse = (
            _extract_prose_json(content) if mechanism == PROSE_JSON else content
        )
        data = _parse_structured(text_for_parse, req.schema) if req.schema else None
        # Named tool_calls round-trip the same way as the Anthropic
        # tool_use blocks above -- a list of {"name","args","id"}, collected
        # even though today's callers dispatch one tool per turn (OpenAI's
        # wire protocol allows several parallel tool_calls in one message).
        tool_calls: list[dict[str, Any]] | None = None
        if req.tools:
            tool_calls = []
            for tc in message.get("tool_calls") or []:
                fn = tc["function"]
                tool_calls.append(
                    {
                        "name": fn["name"],
                        "args": fn["arguments"],
                        "id": tc["id"],
                    }
                )
            tool_calls = tool_calls or None
        cost, cost_source = cost_of_with_source(
            cost_model or req.model, tokens_in, tokens_out
        )
        return LLMResponse(
            content=content,
            data=data,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            model=req.model,
            cost_source=cost_source,
            raw={
                "id": out.get("id"),
                "finish_reason": choice.get("finish_reason"),
            },
            tool_calls=tool_calls,
            output_limited=choice.get("finish_reason") == "length",
        )

    async def embed(
        self, texts: list[str], model: str, client: httpx.AsyncClient
    ) -> list[list[float]]:
        """OpenAI-compatible /embeddings. Returns one vector per input text,
        in input order. Vector-only — the legacy semantic cache's contract."""
        vectors, _meta = await self.embed_with_meta(texts, model, client)
        return vectors

    async def embed_with_meta(
        self, texts: list[str], model: str, client: httpx.AsyncClient
    ) -> tuple[list[list[float]], dict[str, Any]]:
        """OpenAI-compatible /embeddings, metadata-rich. Returns the vectors plus
        the facts native embedding spaces need: the *actual* provider-reported
        model id (not just the requested alias), dimension, usage, and the
        provider request id when the response header carries one."""
        resp = await client.post(
            f"{self.base_url}/embeddings",
            json={"model": model, "input": texts},
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=TIMEOUT,
        )
        _raise_for_status(resp, secret_values=(self.api_key,))
        out = _response_object(resp, "embeddings", self.api_key)
        data = out["data"]
        # OpenAI returns a per-row `index`; Gemini's OpenAI-compat endpoint omits
        # it. Sort by index when ALL rows carry one, else trust response order.
        if all("index" in item for item in data):
            data = sorted(data, key=lambda item: item["index"])
        vectors = [item["embedding"] for item in data]
        meta = {
            # actual model the provider used, falling back to the request if the
            # provider does not echo one
            "actual_model_id": out.get("model") or model,
            "requested_model": model,
            "dimension": len(vectors[0]) if vectors else 0,
            "usage": _object_or_empty(
                out.get("usage"), "embeddings usage", self.api_key
            ),
            "provider_request_id": resp.headers.get("x-request-id") or out.get("id"),
        }
        return vectors, meta

    def _content(self, content: Any) -> Any:
        if isinstance(content, str):
            return content
        parts = []
        for p in content:
            if p.get("type") == "text":
                parts.append({"type": "text", "text": p["text"]})
            elif p.get("type") == "image":
                data_url = f"data:{p['media_type']};base64,{p['data']}"
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": (
                            data_url if self.image_url_as_string else {"url": data_url}
                        ),
                    }
                )
        return parts


def _strictify(schema: dict) -> dict:
    """OpenAI strict mode requires additionalProperties:false and all-required.

    This is a wire-shaping helper invoked ONLY for the
    ``NATIVE_STRICT`` mechanism's request body (the one call site above,
    inside ``if mechanism == NATIVE_STRICT:``) — never for ``JSON_LOOSE``/
    ``PROSE_JSON``, whose bodies carry the ORIGINAL (never-strictified)
    schema. Validation (Section 5) always runs against the original schema
    too, so a strictified body no longer collides with a separate,
    retired all-required default the way the old loose-path parser
    used to (the snippet-refine saga, ``research.py:1417-1431``).

    ``additionalProperties`` is ASSIGNED, not defaulted. OpenAI's structured
    outputs API requires the key "to be supplied and to be false" on EVERY
    object node in the tree, so a schema that deliberately declares an OPEN
    object (``map.extract``'s grounded per-field wrapper and its evidence
    items both set ``additionalProperties: True``, to let page/bbox/snippet
    ride along) is rejected outright with an ``Invalid schema for
    response_format 'result'`` 400 on every GPT model. ``setdefault``
    preserved that ``True`` and shipped it to the wire. Closing it here costs
    nothing the caller relies on: this is a wire-shaping helper, and Section
    5 validation still runs against the ORIGINAL, open schema."""
    s = json.loads(json.dumps(schema))

    def is_object_node(node: dict) -> bool:
        # A nullable object is spelled ``"type": ["object", "null"]``; strict
        # mode requires the closed marker on that node too.
        kind = node.get("type")
        return kind == "object" or (isinstance(kind, list) and "object" in kind)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if is_object_node(node):
                node["additionalProperties"] = False
                props = node.get("properties")
                if isinstance(props, dict):
                    node["required"] = list(props.keys())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)
    return s


def _raise_for_status(
    resp: httpx.Response, *, secret_values: tuple[str | None, ...] = ()
) -> None:
    if resp.status_code == 200:
        return
    retryable = resp.status_code in (408, 409, 429) or resp.status_code >= 500
    try:
        detail = resp.json()
    except Exception:
        detail = resp.text
    # Preserve the provider's machine-readable reason (OpenAI-compat error
    # bodies: {"error": {"code": ..., "type": ...}}) so remediation can
    # distinguish exhausted BYOK spend (insufficient_quota) from a momentary
    # rate limit without string-matching the prose message.
    provider_code: str | None = None
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict):
            code = error.get("code") or error.get("type")
            if isinstance(code, str) and code:
                provider_code = code
    # This boundary owns the key used for the request.  Never inspect a wider
    # credential set merely to make an echoed provider body safe.
    safe_detail = redact_text(
        f"provider returned {resp.status_code}: {detail}",
        secret_values=secret_values,
    )
    if provider_code is not None:
        provider_code = redact_text(
            provider_code,
            secret_values=secret_values,
            max_chars=256,
        )
    raise LLMError(
        safe_detail,
        status=resp.status_code,
        retryable=retryable,
        provider_code=(
            canonical_error_code(provider_code, fallback="provider_error")
            if provider_code is not None
            else None
        ),
    ) from None


def _response_object(
    resp: httpx.Response, operation: str, api_key: str
) -> dict[str, Any]:
    """Decode a successful provider response without leaking its body on error."""
    try:
        out = resp.json()
    except Exception as error:  # provider JSON decoder can include its input
        raise _malformed_response_error(operation, error, api_key) from None
    if not isinstance(out, dict):
        raise _malformed_response_error(
            operation, TypeError("response is not an object"), api_key
        )
    return out


def _object_or_empty(value: object, label: str, api_key: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    raise _malformed_response_error(label, TypeError("value is not an object"), api_key)


def _usage_count(usage: dict[str, Any], field: str, api_key: str) -> int:
    value = usage.get(field, 0)
    if type(value) is not int or value < 0:
        raise _malformed_response_error(
            "completion usage",
            TypeError(f"{field} is not a non-negative integer"),
            api_key,
        )
    return value


def _malformed_response_error(
    operation: str, error: Exception, api_key: str
) -> LLMError:
    error_detail = redact_text(error, secret_values=(api_key,), max_chars=500)
    return LLMError(
        redact_text(
            f"malformed {operation} response: {error_detail}",
            secret_values=(api_key,),
            max_chars=500,
        ),
        retryable=True,
    )
