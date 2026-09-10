"""The structured-output completer — the single chokepoint for schema-bearing
model calls.

Engine: a pydantic-ai ``Agent`` driven over a custom ``pydantic_ai.models.Model``
(:class:`FrisketRouterModel`) that sits ABOVE our ``ModelRouter.complete`` seam.
pydantic-ai owns the repair loop, the corrective-turn construction, the
transcript copy, and token accumulation; the router keeps cache / chaos /
receipts / broker below it. Keeping the shim above that seam preserves all five
house integrations while pydantic-ai owns only structured-output orchestration.

What stays OURS (all named and budgeted):
  * VALIDATION. ``StructuredDict`` validates as ``dict[str, Any]`` and enforces
    nothing, so our :class:`jsonschema.Draft202012Validator` is the
    only enforcement — plugged in via ``@agent.output_validator`` raising
    ``pydantic_ai.ModelRetry`` on violation. This runs for EVERY mechanism,
    including Anthropic tool_use output (closing the #1 hole, adapters.py:104-121).
  * SchemaViolation → ModelRetry translation at the shim: a raw
    ``SchemaViolation`` reaching pydantic-ai is NOT caught by it (only
    ``ModelRetry`` / pydantic validation errors are), so the shim translates.
  * Dollar-cost summation across attempts: pydantic-ai's ``RunUsage``
    sums tokens only; our per-call ``LLMResponse.cost`` (llm/pricing.py::cost_of)
    is no framework field, so the shim accumulates it. ``genai-prices`` is IGNORED.
  * Top-level-array schemas are wrapped ``{"items": <array>}`` because
    ``StructuredDict`` requires ``type == "object"``, then unwrapped on return.
  * Schema faults never reach the transport's blind retry: the initial rollout
    used a request-scoped ``schema_retryable=False`` flag on every request;
    ``structured-retryable-flip-v1`` retired that flag once a grep-guard
    proved every schema-bearing caller was migrated behind this completer —
    ``SchemaViolation.retryable`` is now ``False`` at the class level
    (``types.py``), so ``router._call_with_retry`` never blind-retries one at
    all, for any caller, and the per-request flag was permanently redundant.

Agent lifecycle: a FRESH ``Agent`` + ``FrisketRouterModel`` per ``complete()``
call. Argued: (1) correctness — the model accumulates this call's wire receipts
(cost/token summation) as instance state, so a per-call instance yields a clean
receipt with zero cross-call leakage; (2) the ``Agent`` constructor does no I/O
and is negligible beside the wire call it wraps (measured overhead is not
order-of-magnitude); (3) the ``output_validator`` closure binds this call's
schema + provider for the ``schema_rejects`` metric — caching would force keying
on schema+model+mechanism and resetting the accumulator, reintroducing exactly
the shared-mutable-state bugs a per-call instance avoids.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic_ai import Agent, ModelRetry, StructuredDict
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from frisket.ai.model_defaults import DEFAULT_MAX_OUTPUT_TOKENS

from . import structured_capabilities as capabilities
from .structured_capabilities import (
    JSON_LOOSE,
    MECHANISMS,  # noqa: F401 -- re-exported for callers that import this name from structured.py
    NATIVE_STRICT,
    NATIVE_TOOL,
    PROSE_JSON,
)
from .types import (
    LLMRequest,
    LLMResponse,
    ModelRouterTransport,
    ModelTrace,
    OutputLimitReached,
    ReasoningPolicy,
    SchemaViolation,
    provider_from_model_id,
)

# Mechanism ids — the closed vocabulary (NOT a registry). Re-exported from
# llm/structured_capabilities.py (the single source of mechanism identity)
# so existing imports of these names off llm/structured.py keep working.
# "auto" resolves via ``capabilities.resolve(req.model)``; these string
# values are pinned by tests — keep them identical.

# our mechanism ids -> pydantic-ai output modes (used only to drive a
# ModelProfile).
_MECH_TO_MODE = {
    NATIVE_STRICT: "native",
    NATIVE_TOOL: "tool",
    JSON_LOOSE: "prompted",
    PROSE_JSON: "prompted",
}

# PROSE_JSON extractor. Adapters call this on a PROSE_JSON wire response's
# raw text BEFORE handing it to the json.loads/validation path
# (adapters.py:_parse_structured) -- the mechanism whose request carries no
# response_format at all, for providers/models that reject that field.

# fence delimiters are a fixed literal ("```"), so this can't catastrophically
# backtrack the way an unbounded {...} regex could (a ReDoS class) -- the
# balanced-brace scan below is the one that must NOT be a regex.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_prose_json(text: str) -> str:
    """Tolerant of the two shapes a model produces when not constrained by
    ``response_format``: (1) a fenced code block -- first fenced ```json``` or
    bare ``` ``` span, fence stripped; (2) unfenced prose -- the first
    balanced ``{``...``}`` span, found by a bracket-depth scan (not a greedy
    regex). No match -> ``text`` unchanged, so it passes through
    into the existing ``json.loads`` failure path (``SchemaViolation``,
    today's behavior, unchanged)."""
    fence = _FENCE_RE.search(text)
    if fence:
        return fence.group(1)
    depth = 0
    start: int | None = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    return text[start : i + 1]
    return text


@dataclass
class StructuredRequest:
    """Inputs to :meth:`StructuredCompleter.complete`."""

    model: str
    messages: list[dict[str, Any]]
    schema: dict[str, Any]  # JSON Schema — the lingua franca
    method: str = "auto"  # "auto" (capability-resolved) | a mechanism id
    repair_attempts: int = 1  # 0 = fail-hard; N = feed violations back N times
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    temperature: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)
    reasoning_policy: ReasoningPolicy | None = None


@dataclass
class StructuredResult:
    """Outputs of :meth:`StructuredCompleter.complete`."""

    data: Any  # validated against schema — always (list for top-level arrays)
    content: str | None  # the final wire text (for schemaless/audit)
    method_used: str  # audit tag: the mechanism that actually ran
    # Completer-visible completion attempts: 1 per validation-loop iteration
    # (clean = 1; each repair or translated adapter fault adds 1). Router-
    # internal transport retries and cache hits are deliberately invisible
    # below this seam -- transport receipts are the router's concern.
    attempts: int
    violations: list[str]  # every schema-violation message seen across attempts
    response: LLMResponse  # cost/tokens SUMMED across every wire attempt
    # Individual transport results are retained as non-secret accounting input.
    # Callers must select neutral fields from these responses; ``raw`` remains
    # transport/debug material and must never enter durable provider facts.
    wire_calls: list[LLMResponse] = field(default_factory=list)


# Message translation (our list[dict{role,content}] <-> pydantic-ai typed parts).
# Bidirectional and image-preserving so multimodal callers (map_runner image
# rows) do not regress: our image parts round-trip through BinaryContent.


def _content_to_pai(content: Any) -> Any:
    """our message content -> pydantic-ai user content (str or UserContent list)."""
    if isinstance(content, str):
        return content
    out: list[Any] = []
    for p in content:
        if p["type"] == "text":
            out.append(p["text"])
        elif p["type"] == "image":
            out.append(
                BinaryContent(
                    data=base64.b64decode(p["data"]),
                    media_type=p["media_type"],
                )
            )
    return out


def _content_from_pai(content: Any) -> Any:
    """pydantic-ai user content -> our message content."""
    if isinstance(content, str):
        return content
    parts: list[dict[str, Any]] = []
    for c in content:
        if isinstance(c, str):
            parts.append({"type": "text", "text": c})
        else:
            parts.append(
                {
                    "type": "image",
                    "media_type": c.media_type,
                    "data": base64.b64encode(c.data).decode(),
                }
            )
    return parts


def messages_to_history(messages: list[dict[str, Any]]) -> list[ModelMessage]:
    """Forward: our messages -> a pydantic-ai message_history. system/user parts
    coalesce into ``ModelRequest``s; assistant turns become ``ModelResponse``s.
    The history is only pydantic-ai's bookkeeping context — the actual wire
    content is rebuilt by the shim from THIS list on each request, so images
    survive the round-trip via BinaryContent."""
    history: list[ModelMessage] = []
    pending: list[Any] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "assistant":
            if pending:
                history.append(ModelRequest(parts=pending))
                pending = []
            history.append(ModelResponse(parts=[TextPart(content=str(content))]))
        elif role == "system":
            pending.append(SystemPromptPart(content=str(content)))
        else:  # user (default)
            pending.append(UserPromptPart(content=_content_to_pai(content)))
    if pending:
        history.append(ModelRequest(parts=pending))
    return history


class FrisketRouterModel(Model):
    """Custom pydantic-ai Model mapping pydantic-ai's request (messages + resolved
    output schema/tools) onto our :class:`LLMRequest`, calling
    ``router.complete_transport`` (the real transport seam), and mapping the
    ``LLMResponse`` back to a
    ``ModelResponse``. Promoted and hardened from the original experimental
    shim: its ``_tool_call`` stand-in is dropped because adapters now carry
    arbitrary
    multi-tool defs + ``tool_choice=auto`` + named tool_use/tool_calls parsing for
    both dialects, so pydantic-ai's ``function_tools`` (an Agent's
    ``@agent.tool_plain`` registrations) now ride
    ``LLMRequest.tools`` and round-trip through ``LLMResponse.tool_calls`` — no
    stand-in convention needed. Structured output still rides the single
    ``emit`` tool / JSON body via `schema`, mutually exclusive with `tools`.
    """

    def __init__(
        self,
        router: ModelRouterTransport,
        model_id: str,
        *,
        mechanism: str | None = None,
        recipe_version: str = "1",
        max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: float = 0.0,
        params: dict[str, Any] | None = None,
        reasoning_policy: ReasoningPolicy | None = None,
        capability: dict | None = None,
        trace: ModelTrace | None = None,
    ):
        self.router = router
        self._model_id = model_id
        self._mechanism = mechanism
        self.recipe_version = recipe_version
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._params = dict(params or {})
        self._reasoning_policy = reasoning_policy
        self._trace = trace
        # per-run receipt accumulator — every wire attempt incl. repairs.
        self.wire_calls: list[LLMResponse] = []
        # SchemaViolations the shim translated to ModelRetry (adapter-origin
        # faults: malformed JSON, no tool_use block) — kept so the completer can
        # surface the ORIGINAL message/raw_text if the repair loop exhausts,
        # instead of pydantic-ai's opaque "Exceeded maximum output retries".
        self.translated: list[SchemaViolation] = []
        # ONE counter for every wire call this run makes — successes AND
        # adapter-origin schema-fault translations both hit the wire, so both
        # count. `len(wire_calls)` alone undercounts: a translated fault never
        # reaches `wire_calls` (it raises before that append), so a run with 1
        # success + 1 translated fault previously reported attempts=1 for 2
        # actual wire calls. `attempts` is the ONLY correct source for this.
        self.attempts: int = 0
        super().__init__(profile=self._profile_from_capability(capability))

    # --- Model ABC surface -------------------------------------------------
    @property
    def provider(self) -> None:
        return None

    @property
    def model_name(self) -> str:
        return self._model_id

    @property
    def system(self) -> str:
        return provider_from_model_id(self._model_id)

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        model_settings, params = self.prepare_request(
            model_settings, model_request_parameters
        )
        schema, mode = self._resolve_schema(params)
        # function_tools (an Agent's @agent.tool_plain registrations) ride
        # LLMRequest.tools, orthogonal to `schema` -- mutually exclusive in
        # every caller today (a StructuredCompleter run never registers
        # tools; sdk/ops/research_answer.py's AgentRecipe never sets
        # output_type=StructuredDict),
        # but resolved independently here so a request only carries `tools`
        # when the Agent actually registered function tools.
        tools = self._resolve_tools(params)
        req = LLMRequest(
            model=self._model_id,
            messages=self._to_our_messages(messages),
            schema=schema,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            params=self._params,
            mechanism=self._mechanism,
            # the completer owns schema repair — the transport never
            # blind-retries a SchemaViolation with the identical body
            # (SchemaViolation.retryable=False at the class level — no
            # per-request opt-out needed).
            tools=tools,
            reasoning_policy=self._reasoning_policy,
        )
        try:
            resp = await self.router.complete_transport(
                req,
                recipe_version=self.recipe_version,
                trace=self._trace,
            )
        except SchemaViolation as sv:
            # A raw SchemaViolation is invisible to pydantic-ai's retry loop
            # (it only retries ModelRetry / validation errors) — translate so
            # chaos/malformed-JSON faults compose with the repair loop.
            self.translated.append(sv)
            self.attempts += 1  # this WAS a wire call — it just faulted.
            raise ModelRetry(f"invalid structured output: {sv}") from sv
        self.wire_calls.append(resp)
        self.attempts += 1
        if schema is not None and resp.output_limited:
            raise OutputLimitReached(
                "structured output stopped at the provider output limit",
                raw_text=resp.content,
                wire_calls=list(self.wire_calls),
            )
        return self._to_model_response(resp, params, mode)

    # --- profile <- the capability row -------------------------------------
    @staticmethod
    def _profile_from_capability(cap: dict | None) -> ModelProfile:
        if cap is None:
            return ModelProfile(
                supports_json_schema_output=True,
                supports_json_object_output=True,
                supports_tools=True,
            )
        return ModelProfile(
            default_structured_output_mode=_MECH_TO_MODE.get(
                cap.get("default_mechanism", NATIVE_TOOL), "tool"
            ),
            supports_json_schema_output=cap.get("supports_native_strict", False),
            supports_json_object_output=cap.get("supports_json_object", False),
            supports_tools=cap.get("supports_native_tool", True),
        )

    # --- translation helpers ----------------------------------------------
    def _to_our_messages(self, messages: list[ModelMessage]) -> list[dict]:
        out: list[dict] = []
        # pydantic-ai repeats `m.instructions` on every ModelRequest; deduplicate
        # identical prompts to preserve message-history hygiene and cache determinism.
        last_instructions: str | None = None
        for m in messages:
            if m.kind == "request":
                if m.instructions and m.instructions != last_instructions:
                    out.append({"role": "system", "content": m.instructions})
                    last_instructions = m.instructions
                for p in m.parts:
                    if p.part_kind == "system-prompt":
                        out.append({"role": "system", "content": p.content})
                    elif p.part_kind == "user-prompt":
                        out.append(
                            {"role": "user", "content": _content_from_pai(p.content)}
                        )
                    elif p.part_kind == "tool-return":
                        out.append(
                            {
                                "role": "user",
                                "content": f"Observation:\n{p.model_response_str()}",
                            }
                        )
                    elif p.part_kind == "retry-prompt":
                        out.append({"role": "user", "content": p.model_response()})
                    else:
                        raise TypeError(f"unsupported request part: {p.part_kind}")
            else:
                for p in m.parts:
                    if p.part_kind == "text":
                        out.append({"role": "assistant", "content": p.content})
                    elif p.part_kind == "tool-call":
                        out.append(
                            {
                                "role": "assistant",
                                "content": json.dumps(
                                    {"tool": p.tool_name, "args": p.args_as_dict()}
                                ),
                            }
                        )
                    else:
                        raise TypeError(f"unsupported response part: {p.part_kind}")
        return out

    @staticmethod
    def _resolve_schema(params: ModelRequestParameters) -> tuple[dict | None, str]:
        mode = params.output_mode
        if params.output_tools:
            return params.output_tools[0].parameters_json_schema, "tool"
        if params.output_object is not None:
            return params.output_object.json_schema, mode
        return None, "text"

    @staticmethod
    def _resolve_tools(params: ModelRequestParameters) -> list[dict[str, Any]] | None:
        """Map an Agent's ``@agent.tool_plain`` registrations
        (``params.function_tools``) to ``LLMRequest.tools``. Distinct from
        ``output_tools`` (the single forced structured-output
        tool, resolved by ``_resolve_schema`` above) -- these are the tools a
        multi-step Agent dispatches mid-run (AgentRecipe search/fetch)."""
        if not params.function_tools:
            return None
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.parameters_json_schema,
            }
            for t in params.function_tools
        ]

    def _to_model_response(
        self, resp: LLMResponse, params: ModelRequestParameters, mode: str
    ) -> ModelResponse:
        usage = RequestUsage(
            input_tokens=resp.tokens_in,
            output_tokens=resp.tokens_out,
            # cost may be None (unknown pricing — never fabricate a cost);
            # this is only a pydantic-ai usage detail, not our receipt.
            details={"frisket_cost_usd_milli": int((resp.cost or 0.0) * 1000)},
        )
        data = resp.data
        if mode == "tool" and params.output_tools:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=params.output_tools[0].name,
                        args=data,
                    )
                ],
                usage=usage,
                model_name=self.model_name,
            )
        if resp.tool_calls:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=tc["name"],
                        args=tc["args"],
                        tool_call_id=tc["id"],
                    )
                    for tc in resp.tool_calls
                ],
                usage=usage,
                model_name=self.model_name,
            )
        if data is not None:
            text = json.dumps(data)
        elif resp.content is not None:
            text = resp.content
        else:
            text = ""
        return ModelResponse(
            parts=[TextPart(content=text)], usage=usage, model_name=self.model_name
        )


def _wrap_array_schema(schema: dict) -> tuple[dict, bool]:
    """Wrap top-level arrays because StructuredDict requires an object.

    Returns ``(schema_to_use, was_wrapped)``.
    """
    if isinstance(schema, dict) and schema.get("type") == "array":
        return (
            {
                "type": "object",
                "properties": {"items": schema},
                "required": ["items"],
            },
            True,
        )
    return schema, False


def _data_path(parts: Any) -> str:
    """Render jsonschema's instance path as a compact, model-readable path."""
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        elif isinstance(part, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part):
            path += f".{part}"
        else:
            path += f"[{json.dumps(part, ensure_ascii=False)}]"
    return path


def _validation_retry_message(error: Any) -> str:
    """Turn one jsonschema failure into actionable corrective-turn context.

    ``ValidationError.message`` alone can tell a small model that a property is
    missing without explaining *where* it belongs or that the requested output
    is a data instance rather than the JSON Schema definition. Keep the original
    error text for receipts, but make the ephemeral retry prompt unambiguous.
    """
    guidance = (
        "Return ONLY a corrected JSON data instance that validates against the "
        "schema. Do not return or describe the JSON Schema itself. Fields such "
        "as `properties`, `required`, and `type` are schema metadata unless the "
        "schema explicitly names them as data properties; do not copy schema "
        "metadata into the requested data."
    )
    if error.validator == "required":
        guidance += (
            " Required properties must appear in the data object at the location "
            "named by the error."
        )
    return (
        f"schema violation at data path {_data_path(error.absolute_path)}: "
        f"{error.message}\n\n{guidance}"
    )


class UnsupportedMechanismError(ValueError):
    """An explicit ``StructuredRequest.method`` pins a mechanism that's
    KNOWABLY unsupported for this model. This is raised BEFORE any wire call,
    with the same pre-flight posture as the ``schema is None`` check below.
    Two sources of
    knowability, both consulted by :meth:`StructuredCompleter.complete`:

    1. Hard dialect facts (always checked, every completer instance,
       ``capabilities.dialect_mechanisms`` -- e.g. Anthropic's Messages API
       has no ``response_format`` concept, so pinning ``NATIVE_STRICT`` or
       ``JSON_LOOSE`` there is unsupported regardless of which specific
       model).
    2. A recorded probe (``llm/structured_probe.py::ProbeStore``, opt-in via
       the ``probe_store`` constructor arg) that already ran this exact
       (model, mechanism) pair and recorded ``supported=False``.

    "auto" never raises this (there's no override to validate); an unknown
    model's UNTESTED explicit mechanism does not raise it either -- silence
    (no dialect conflict, no contrary probe) is not the same as "knowably
    unsupported" (record-don't-experiment: this contract never fabricates a
    rejection for something it hasn't observed)."""


class StructuredCompleter:
    """The single chokepoint for schema-bearing model calls.

    ``probe_store`` defaults to ``None`` for zero behavior change. Supplying a
    ``llm/structured_probe.py::ProbeStore``
    (even an empty one) opts this completer instance into the full recorded-
    capability layer for BOTH knobs -- ``structured_capabilities.RECORDED_PROMOTIONS``
    (the static, committed evidence, e.g. ``openrouter/openai/*`` ->
    ``NATIVE_STRICT``) plus whatever this store has itself recorded for the
    exact model in play, merged into "auto" resolution and consulted by the
    explicit-override validation above. Without a store, "auto" uses the
    parity-only static table and only the hard dialect check
    applies to an override -- the store is what turns on the OPT-IN half of
    "regardless of the capability table"."""

    def __init__(
        self,
        router: ModelRouterTransport,
        *,
        probe_store: Any | None = None,
    ):
        self.router = router
        self.probe_store = probe_store

    async def complete(
        self,
        req: StructuredRequest,
        *,
        recipe_version: str = "1",
        trace: ModelTrace | None = None,
    ) -> StructuredResult:
        if req.schema is None:
            raise ValueError("StructuredRequest.schema is required")

        wrapped_schema, was_wrapped = _wrap_array_schema(req.schema)
        # Untrusted/broker-supplied schemas can be structurally invalid; catch it
        # here as a typed SchemaViolation rather than letting an unhandled
        # jsonschema exception crash the caller.
        try:
            Draft202012Validator.check_schema(wrapped_schema)
        except SchemaError as e:
            raise SchemaViolation(f"invalid schema: {e.message}") from e
        validator = Draft202012Validator(wrapped_schema)

        provider = provider_from_model_id(req.model)
        # The table (llm/structured_capabilities.py) is the single source for
        # what "auto" means for this model. An explicit mechanism id is
        # still honored as both the audit tag and the WIRE mechanism.
        #
        # Cache-key / cassette parity (this mint's own constraint — see
        # structured_capabilities.py's module docstring): the wire mechanism
        # stays None for "auto", so cache.py:request_key's hash of
        # req.mechanism is byte-identical to every already-committed
        # cassette for a request that doesn't override the method. Only
        # method_used (an audit tag, not part of any request) and the
        # ModelProfile fed to pydantic-ai (an in-process hint that doesn't
        # touch the wire body) resolve through the table for "auto" calls.
        explicit = req.method if req.method != "auto" else None
        if explicit is not None:
            # Validate the pinned mechanism where knowable, honest error
            # where not -- BEFORE any wire call (SchemaViolation.retryable=
            # False means a wire-level rejection wouldn't get a useful retry
            # anyway; failing pre-flight is strictly more honest than
            # sending a request the adapter can't actually shape).
            dialect_ok = explicit in capabilities.dialect_mechanisms(req.model)
            if not dialect_ok:
                raise UnsupportedMechanismError(
                    f"{req.model!r}'s wire dialect does not support the "
                    f"pinned mechanism {explicit!r} "
                    f"(supports: {sorted(capabilities.dialect_mechanisms(req.model))})"
                )
            if self.probe_store is not None:
                probed = self.probe_store.get(req.model, explicit)
                if probed is not None and not probed.supported:
                    raise UnsupportedMechanismError(
                        f"a recorded probe found {req.model!r} does not "
                        f"support {explicit!r}"
                        + (f" ({probed.note})" if probed.note else "")
                    )

        if self.probe_store is not None:
            # Opt-in recorded-capability layer: the static committed
            # promotions plus whatever this exact model has been probed for
            # -- merged into the SAME specificity contest
            # capabilities.resolve() already runs (module docstring).
            probed_rows = tuple(
                r.as_capability_row() for r in self.probe_store.all_for_model(req.model)
            )
            cap_row = capabilities.resolve(
                req.model,
                promotions=capabilities.RECORDED_PROMOTIONS + probed_rows,
            )
        else:
            cap_row = capabilities.resolve(req.model)
        method_used = explicit or cap_row.structured_mode

        model = FrisketRouterModel(
            self.router,
            req.model,
            mechanism=explicit,
            recipe_version=recipe_version,
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            params=req.params,
            reasoning_policy=req.reasoning_policy,
            capability=cap_row.as_profile_dict(),
            trace=trace,
        )

        violations: list[str] = []

        def _validate(data: Any) -> Any:
            errs = sorted(validator.iter_errors(data), key=str)
            if errs:
                self.router.note_schema_reject(provider)
                error = errs[0]
                msg = error.message
                violations.append(msg)
                raise ModelRetry(_validation_retry_message(error))
            return data

        agent = Agent(
            model=model,
            output_type=StructuredDict(wrapped_schema),
            retries=req.repair_attempts,
        )
        agent.output_validator(_validate)

        history = messages_to_history(req.messages)
        try:
            run = await agent.run(message_history=history)
            validated = run.output
        except UnexpectedModelBehavior as e:
            # retries exhausted — surface as a SchemaViolation carrying the last
            # violation + last wire text, so map_runner's classify_llm_error
            # tags it invalid_output.
            if violations:  # a jsonschema validation failure
                detail = violations[-1]
                if model.wire_calls:
                    last = model.wire_calls[-1]
                    # tool-use mechanisms (Anthropic) carry the payload in
                    # `.data`, not `.content` — content is None there, so
                    # falling back on it alone would drop the raw payload
                    # the trace (trace.py) records as raw_response.
                    last_raw = (
                        last.content
                        if last.content is not None
                        else json.dumps(last.data, default=str)
                    )
                else:
                    last_raw = None
            elif model.translated:  # an adapter-origin fault (malformed/no-tool)
                sv = model.translated[-1]
                detail = str(sv)
                last_raw = sv.raw_text
            else:
                detail = str(e)
                last_raw = None
            raise SchemaViolation(
                detail,
                raw_text=last_raw,
                wire_calls=list(model.wire_calls),
            ) from e
        except Exception as e:
            # A corrective request can fail at the transport after an earlier
            # schema-invalid response already returned (and was billed).  The
            # transport exception remains the row's real failure, but it must
            # carry the successful wire history so the outer runner can write
            # those provider facts instead of erasing them with the repair.
            prior = list(getattr(e, "wire_calls", []) or [])
            seen = {id(call) for call in model.wire_calls}
            e.wire_calls = [
                *model.wire_calls,
                *(call for call in prior if id(call) not in seen),
            ]
            raise

        data = validated["items"] if was_wrapped else validated
        return StructuredResult(
            data=data,
            content=(model.wire_calls[-1].content if model.wire_calls else None),
            method_used=method_used,
            attempts=model.attempts,
            violations=violations,
            # `data` here is the FINAL UNWRAPPED value (StructuredResult.data,
            # above) — the receipt response must never disagree with the
            # contract data (would otherwise leak the wrapped {"items": [...]}
            # form via wire_calls[-1].data for top-level-array schemas).
            response=_sum_response(req.model, model.wire_calls, data),
            wire_calls=list(model.wire_calls),
        )


def _sum_response(
    model_id: str, wire_calls: list[LLMResponse], data: Any = None
) -> LLMResponse:
    """Sum cost/tokens across every wire attempt (clean + repairs) into ONE
    receipt, avoiding the map-runner under-count that billed only the last
    attempt. ``cached`` is True only for a lone cache hit. ``data`` is
    the completer's FINAL unwrapped value — the receipt's ``.data`` always
    matches ``StructuredResult.data``, never the raw (possibly wrapped) last
    wire call's payload."""
    if not wire_calls:
        return LLMResponse(
            content=None,
            data=data,
            tokens_in=0,
            tokens_out=0,
            cost=0.0,
            model=model_id,
            provider=provider_from_model_id(model_id),
        )
    last = wire_calls[-1]
    # Cache cassettes retain the historical provider cost as evidence about
    # the original call.  A replay spends none of it in this run.  Conversely,
    # one unknown live cost makes the live total unknown rather than a partial
    # number assembled from only the priced calls.
    live_calls = [response for response in wire_calls if not response.cached]
    if not live_calls:
        total_cost = 0.0
    elif any(response.cost is None for response in live_calls):
        total_cost = None
    else:
        total_cost = sum(response.cost for response in live_calls)
    # Same shape as total_cost: the router already measured every live wire
    # attempt (clean + repairs), so their sum is the real wall time this
    # completion spent, not a fabricated NULL. A wholly-cached response or
    # any live attempt whose bracket didn't fire makes the total unknown
    # rather than a confident partial sum.
    if not live_calls:
        total_duration_ms = None
    elif any(response.duration_ms is None for response in live_calls):
        total_duration_ms = None
    else:
        total_duration_ms = sum(response.duration_ms for response in live_calls)
    return LLMResponse(
        content=last.content,
        data=data,
        tokens_in=sum(r.tokens_in for r in wire_calls),
        tokens_out=sum(r.tokens_out for r in wire_calls),
        cost=total_cost,
        model=model_id,
        cached=all(response.cached for response in wire_calls),
        provider=last.provider,
        credential_source=last.credential_source,
        cost_source=last.cost_source,
        raw=last.raw,
        duration_ms=total_duration_ms,
        output_limited=last.output_limited,
    )
