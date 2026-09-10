"""Shared request/response types for the model layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict, cast

from frisket.ai.model_defaults import DEFAULT_MAX_OUTPUT_TOKENS

from .pricing import ModelCostSource


CredentialSource = Literal[
    "none", "local", "cache", "project_key", "org_byok", "platform_key"
]
TransportKind = Literal["connect"]
ReasoningPolicy = Literal["disabled", "low_exclude"]
CREDENTIAL_SOURCES: frozenset[str] = frozenset(
    {"none", "local", "cache", "project_key", "org_byok", "platform_key"}
)

ModelTraceEvent = dict[str, Any]
ModelTrace = list[ModelTraceEvent]


class ModelRouterTransport(Protocol):
    """The typed call boundary shared by routing, structured output, and tracing."""

    def adapter_for(self, provider: str) -> Any: ...

    def resolve_local_model(self, model: str) -> tuple[Any, Any, str]: ...

    async def complete(
        self,
        req: "LLMRequest",
        *,
        recipe_version: str = "1",
        trace: ModelTrace | None = None,
    ) -> "LLMResponse": ...

    async def complete_transport(
        self,
        req: "LLMRequest",
        *,
        recipe_version: str = "1",
        trace: ModelTrace | None = None,
    ) -> "LLMResponse": ...

    def secret_values_for_model(self, model: str) -> tuple[str, ...]: ...

    def note_schema_reject(self, provider: str) -> None: ...


def validate_credential_source(value: object) -> CredentialSource:
    """Return a supported non-secret source token, rejecting unknown input."""
    if not isinstance(value, str) or value not in CREDENTIAL_SOURCES:
        raise ValueError(f"unsupported credential source: {value!r}")
    return cast(CredentialSource, value)


@dataclass
class LLMRequest:
    """Provider-agnostic request. model is routed by prefix:
    anthropic/..., openai/..., gemini/..., openrouter/...,
    ollama/@<endpoint-id>/...
    messages: [{"role": "user"|"system"|"assistant", "content": str | list[part]}]
    where part = {"type":"text","text":...} or {"type":"image","media_type":...,"data":<b64>}.
    schema: JSON Schema, the provider-neutral structured-output contract.
    mechanism: the resolved wire strategy (llm/structured.py mechanism id) when a
        request is issued by the StructuredCompleter; None for direct callers
        (adapters then fall back to their own strict/loose choice). It is a
        first-class field (not a params entry) so it is legible next to
        model/schema and can join the cache key (cache.py request_key).
    tools: arbitrary multi-tool definitions, kept independent of any one provider:
        each entry {"name": str, "description": str, "parameters": <json schema>}.
        When set, adapters send ALL of them with tool_choice="auto" (the model
        picks, or emits plain text) -- orthogonal to `schema`, which still forces
        the single "emit" tool for NATIVE_TOOL structured output, unchanged. Only
        ONE of schema/tools is set per request: a schema call is the completer's
        single-shot structured output; a tools call is FrisketRouterModel's
        function-tool dispatch for a pydantic-ai Agent
        (sdk/ops/research_answer.py's AgentRecipe).
    """

    model: str
    messages: list[dict[str, Any]]
    schema: dict[str, Any] | None = None
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    temperature: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)
    mechanism: str | None = None
    tools: list[dict[str, Any]] | None = None
    # Closed OpenRouter control used by perception-style calls. Keeping this
    # first-class prevents arbitrary provider-body overrides through ``params``.
    reasoning_policy: ReasoningPolicy | None = None


class LLMToolCall(TypedDict):
    name: str
    args: str | dict[str, Any]
    id: str


@dataclass
class LLMResponse:
    content: str | None
    data: dict[str, Any] | None
    tokens_in: int
    tokens_out: int
    cost: float | None
    model: str
    # Minted beside the pricing lookup and carried through accounting. A zero
    # alone cannot distinguish structural local compute from a pinned $0 rate.
    cost_source: ModelCostSource = "unknown"
    cached: bool = False
    # The provider ModelRouter actually routed this call to, stamped beside
    # credential_source (router._complete_transport) on every path -- live,
    # chaos-fabricated and cache replay. Receipts read THIS instead of
    # re-parsing `model`; the pre-stamp default is the same "unknown" those
    # re-derivations used to fall back to.
    provider: str = "unknown"
    # Non-secret provenance for the credential selected by ModelRouter.
    credential_source: str = "none"
    raw: dict[str, Any] = field(default_factory=dict)
    # Named tool_use/tool_calls blocks parsed from a tools-bearing request.
    tool_calls: list[LLMToolCall] | None = None
    # Wall time of the ACCEPTED adapter attempt, milliseconds, from the
    # monotonic clock ModelRouter._call_with_retry already runs for its health
    # stats — the one site that sees both ends of the wire call. None on a
    # cache replay (no call happened) and on any response that did not come
    # through that bracket; a receipt reads THIS rather than timing the
    # surrounding row, which would also count rendering and validation.
    duration_ms: int | None = None
    # Provider-neutral evidence that generation stopped at its output-token
    # limit. False keeps older cache payloads and response constructors valid.
    output_limited: bool = False


def provider_from_model_id(model: str) -> str:
    """The provider segment of a routed ``provider/model`` id — one mint.

    Exactly the derivation ``ModelRouter`` performs to select an adapter, so
    every site reports the provider the router would actually route to. The
    ONLY spelling of it, and only for sites holding a model STRING and no
    response: anything holding an :class:`LLMResponse` reads
    ``response.provider`` (stamped by the router) instead of re-deriving. An
    empty/unparseable id yields ``"unknown"``, matching
    ``LLMResponse.provider``'s pre-stamp default.
    """
    provider, _sep, _rest = (model or "").partition("/")
    return provider or "unknown"


class LLMError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        provider_code: str | None = None,
        transport_kind: TransportKind | None = None,
        post_egress_ambiguous: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        # The provider's machine-readable error reason (e.g. the OpenAI-compat
        # error body's `error.code`, "insufficient_quota"), parsed by
        # adapters._raise_for_status. HTTP status alone cannot distinguish a
        # momentary 429 rate limit from exhausted BYOK spend; remediation's
        # classify_resumable_provider_error branches on this, never on the
        # human-prose message text.
        self.provider_code = provider_code
        # Closed, non-secret transport classification.  This is intentionally
        # not an exception/cause: callers may use it for remediation without
        # retaining a third-party exception whose text can contain a URL or
        # credential-bearing request details.
        self.transport_kind = transport_kind
        # The provider may have accepted/billed work even though no response
        # fact was recoverable.  This is deliberately separate from
        # ``retryable``: the latter means a later operator-driven resume may
        # succeed, not that an immediate identical wire call is safe.
        self.post_egress_ambiguous = bool(post_egress_ambiguous)


class SchemaViolation(LLMError):
    """Model returned output that doesn't parse/validate against the schema.

    ``retryable=False``. Every schema-bearing model call now runs behind
    ``StructuredCompleter``, which owns bounded repair itself (the whole
    point of Section 6's ordering gate); blind-retrying the identical
    request at the transport layer (``router._call_with_retry``) was always
    the WRONG recovery for a malformed/invalid structured response -- it
    can't fix a schema fault, only a repair (a corrective turn) can. This
    flip is gated on a grep-guard (tests/ai/test_structured_retryable_flip.py)
    proving zero remaining callers construct a schema-bearing ``LLMRequest``
    and call ``router.complete`` directly outside ``llm/structured.py`` --
    the retired request-scoped ``schema_retryable`` flag is permanently
    redundant:
    ``not e.retryable`` alone blocks every SchemaViolation from the transport
    retry path, for every caller, forever."""

    def __init__(
        self,
        message: str,
        raw_text: str | None = None,
        *,
        wire_calls: list[LLMResponse] | None = None,
    ):
        super().__init__(message, retryable=False)
        self.raw_text = raw_text
        # A schema failure happens after transport success.  Retain the
        # neutral response envelopes so the row failure cannot erase calls
        # the provider already billed.  Structural SchemaViolations raised
        # before transport naturally carry an empty list.
        self.wire_calls = list(wire_calls or [])


class OutputLimitReached(SchemaViolation):
    """A schema-valid response that may be incomplete due to an output limit."""
