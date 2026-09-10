"""Model router: the single seam all model traffic flows through.

Pipeline per call: chaos intercept → cache lookup → adapter call (with
retry/backoff on retryable errors) → cache store. Keys come from the
environment or are passed explicitly; ops never see them (key broker).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, Protocol

import httpx

from frisket.redaction import canonical_error_code, redact_text

from .adapters import AnthropicAdapter, OpenAICompatAdapter
from .cache import CacheMiss, ResponseCache, request_key
from .chaos import ChaosConfig, ChaosMiddleware
from frisket.local_model_ids import parse_local_model_id

from .endpoint_config import LocalModelEndpointConfig
from .pricing import model_cost_source
from .types import (
    LLMError,
    LLMRequest,
    LLMResponse,
    ModelTrace,
    SchemaViolation,
    provider_from_model_id,
    validate_credential_source,
)

if TYPE_CHECKING:
    from frisket.ai.embeddings import EmbeddingBatchResult

CacheMode = Literal["replay", "fresh", "replay_strict", "off"]

# The full CacheMode vocabulary, for callers (HostedConfig, CLI) that read a
# mode from an env var and must validate it before constructing a router.
CACHE_MODES: tuple[CacheMode, ...] = ("replay", "fresh", "replay_strict", "off")


class ModelCallPolicy(Protocol):
    """Optional neutral observer around a real model transport call."""

    def prepare(self, request: LLMRequest, credential_source: str) -> LLMRequest: ...

    def before_live(self, request: LLMRequest, credential_source: str) -> None: ...

    def after_live(
        self,
        request: LLMRequest,
        response: LLMResponse,
        credential_source: str,
    ) -> None: ...


def live_calls_possible(cache_mode: CacheMode) -> bool:
    """Whether this cache mode can result in an actual live provider API call.

    ``replay_strict`` is the only cache-only posture: a strict-replay cache
    miss raises ``CacheMiss`` instead of calling out (see
    ``_complete_transport`` below), so it makes no live call. Every other mode
    can: ``replay`` is cache-FIRST but falls through to a live wire attempt on
    a miss ("hit returns cached; miss does a live call then stores"), and
    ``fresh``/``off`` always attempt a live call. Used to decide when the
    frontend's replay-mode top bar (onboard-replay-banner-v1) should warn that
    no live AI calls can be made.

    NOT AN EGRESS OR MONEY AUTHORITY. This reads the mode string ONLY, and
    the mode alone does not prove anything: ``_complete_transport``'s strict
    branch is guarded on ``self.cache is not None``, so a ``replay_strict``
    router with NO cache attached skips both replay branches and calls the
    live adapter. Anything deciding whether a call can spend, needs consent,
    or needs a checkpoint must use :func:`model_call_cannot_go_live`, which
    reads both axes; the operator-facing variant is
    ``frisket.operability.diagnostics.replay_mode_report``.
    """
    return cache_mode != "replay_strict"


def model_call_cannot_go_live(router: object) -> bool:
    """THE strict-replay proof: this router provably cannot reach a live
    adapter for a model call, so that call can neither spend provider money
    nor book a hosted meter.

    True iff BOTH axes hold — mode is ``replay_strict`` AND a ``ResponseCache``
    is attached — because that is exactly the condition under which
    :meth:`ModelRouter._complete_transport` raises ``CacheMiss`` before egress.
    With ``cache=None`` the ``self.cache is not None`` guard is false, both
    replay branches are skipped, and the request falls through to
    ``_call_with_retry`` and the provider.

    One mint site for one fact. Every money/consent authority reads THIS
    (the row-effect paidness predicate, validation's confirmation gate and
    spend cap, the reduce family's checkpoint gate), so they cannot disagree
    about whether a run can spend — they used to, each deriving the answer
    from ``cache_mode`` alone, and a cacheless strict-replay run therefore
    reached a metered provider uncapped, unconfirmed and unfenced.

    Duck-typed on purpose: callers hold a router-shaped object and must not
    have to import :class:`ModelRouter` (or its adapter/pricing surface) to
    ask a one-line question. Missing attributes fail CLOSED — an object that
    cannot prove the posture does not get the exemption.

    Scope: this is a fact about the MODEL invocation only. A recipe whose
    effect graph includes non-model paid subeffects (``research.answer``
    replays a cached model response while its tool calls perform LIVE web
    search and fetch) is NOT exempt just because its model call is cached;
    the callers scope the exemption to the model subeffect rather than to the
    recipe's whole effect graph.
    """
    return (
        getattr(router, "cache_mode", None) == "replay_strict"
        and getattr(router, "cache", None) is not None
    )


def resolve_env_cache_mode(
    env: Mapping[str, str] | None = None,
    *,
    var: str = "FRISKET_CACHE_MODE",
    default: CacheMode = "replay",
) -> CacheMode:
    """Read the open/local server's cache-mode knob from the environment.

    The settings "AI call mode" page documents this as the way an operator
    sets the active posture. Absent or empty -> ``default`` ("replay", the
    safe cache-first posture). A value in :data:`CACHE_MODES` -> that mode.
    Anything else -> :class:`ValueError`, so a typo'd ``FRISKET_CACHE_MODE``
    fails LOUDLY at server startup rather than silently defaulting and
    misleading the operator about which posture is live.
    """
    environ = os.environ if env is None else env
    raw = environ.get(var)
    if not raw:
        return default
    if raw not in CACHE_MODES:
        raise ValueError(
            f"{var}={raw!r} is not a valid cache mode "
            f"(expected one of: {', '.join(CACHE_MODES)})"
        )
    return raw  # type: ignore[return-value]


def _provider_kind(provider: str) -> str:
    """Capability-contract provider_kind (MODEL_CAPABILITY_CONTRACT). The
    table lives in frisket.models.metadata (shared with the network gate and
    the embeddings store); imported locally to avoid the models<->llm module
    cycle (frisket.models.metadata itself imports frisket.llm.types)."""
    from frisket.ai.models.metadata import PROVIDER_KIND

    return PROVIDER_KIND.get(provider, "platform_api")


class LocalEndpointAdapter:
    """Resolve every qualified local-model operation to one exact server."""

    def __init__(self, endpoints: tuple[LocalModelEndpointConfig, ...]) -> None:
        if not endpoints:
            raise ValueError("at least one local model endpoint is required")
        self._adapters = {
            endpoint.endpoint_id: OpenAICompatAdapter(
                endpoint.bearer_for_inference(),
                f"{endpoint.origin}/v1",
                image_url_as_string=True,
            )
            for endpoint in endpoints
        }
        self._endpoints = {endpoint.endpoint_id: endpoint for endpoint in endpoints}

    def resolve(
        self, model_id: str
    ) -> tuple[LocalModelEndpointConfig, OpenAICompatAdapter, str]:
        try:
            endpoint_id, bare_model = parse_local_model_id(model_id)
        except ValueError as exc:
            raise LLMError(str(exc)) from exc
        endpoint = self._endpoints.get(endpoint_id)
        adapter = self._adapters.get(endpoint_id)
        if endpoint is None or adapter is None:
            raise LLMError(f"unknown local model endpoint '{endpoint_id}'")
        return endpoint, adapter, bare_model

    def adapter_for_endpoint(self, endpoint_id: str) -> OpenAICompatAdapter:
        adapter = self._adapters.get(endpoint_id)
        if adapter is None:
            raise LLMError(f"unknown local model endpoint '{endpoint_id}'")
        return adapter

    async def complete(self, req: LLMRequest, client: httpx.AsyncClient) -> LLMResponse:
        _endpoint, adapter, model = self.resolve(req.model)
        delegated = replace(req, model=f"ollama/{model}")
        response = await adapter.complete(delegated, client, cost_model=req.model)
        response.model = req.model
        return response

    async def embed(
        self, texts: list[str], model: str, client: httpx.AsyncClient
    ) -> list[list[float]]:
        _endpoint, adapter, bare_model = self.resolve(model)
        return await adapter.embed(texts, bare_model, client)

    async def embed_with_meta(
        self, texts: list[str], model: str, client: httpx.AsyncClient
    ) -> tuple[list[list[float]], dict]:
        _endpoint, adapter, bare_model = self.resolve(model)
        vectors, meta = await adapter.embed_with_meta(texts, bare_model, client)
        meta["requested_model"] = model
        if meta.get("actual_model_id") == bare_model:
            meta["actual_model_id"] = model
        return vectors, meta


class ModelRouter:
    def __init__(
        self,
        keys: dict[str, str] | None = None,
        key_sources: dict[str, str] | None = None,
        env_key_source: str | None = None,
        cache: ResponseCache | None = None,
        cache_mode: CacheMode = "replay",
        chaos: ChaosConfig | None = None,
        max_retries: int = 3,
        use_env_keys: bool = True,
        local_endpoints: tuple[LocalModelEndpointConfig, ...] = (),
        model_call_policy: ModelCallPolicy | None = None,
    ):
        env = os.environ if use_env_keys else {}
        keys = keys or {}
        key_sources = key_sources or {}
        default_source = validate_credential_source(env_key_source or "local")
        key_sources = {
            provider: validate_credential_source(source)
            for provider, source in key_sources.items()
        }
        self._configured_keys: dict[str, str] = {}
        self._credential_sources: dict[str, str] = {}
        self._explicit_credential_sources: set[str] = set(key_sources)
        if env_key_source is not None:
            self._explicit_credential_sources.update(
                provider
                for provider, env_name in {
                    "anthropic": "ANTHROPIC_API_KEY",
                    "openai": "OPENAI_API_KEY",
                    "gemini": "GEMINI_API_KEY",
                    "openrouter": "OPENROUTER_API_KEY",
                }.items()
                if env.get(env_name) and provider not in keys
            )
        self._adapters: dict[str, object] = {}
        anth = keys.get("anthropic") or env.get("ANTHROPIC_API_KEY")
        if anth:
            self._configured_keys["anthropic"] = anth
            self._credential_sources["anthropic"] = key_sources.get(
                "anthropic", default_source
            )
            self._adapters["anthropic"] = AnthropicAdapter(
                anth,
                no_temperature_prefixes=("claude-sonnet-5", "claude-fable-5"),
            )
        oai = keys.get("openai") or env.get("OPENAI_API_KEY")
        if oai:
            self._configured_keys["openai"] = oai
            self._credential_sources["openai"] = key_sources.get(
                "openai", default_source
            )
            self._adapters["openai"] = OpenAICompatAdapter(
                oai,
                "https://api.openai.com/v1",
                max_tokens_param="max_completion_tokens",
                no_temperature_prefixes=("gpt-5", "o3", "o4"),
            )
        gem = keys.get("gemini") or env.get("GEMINI_API_KEY")
        if gem:
            self._configured_keys["gemini"] = gem
            self._credential_sources["gemini"] = key_sources.get(
                "gemini", default_source
            )
            self._adapters["gemini"] = OpenAICompatAdapter(
                gem,
                "https://generativelanguage.googleapis.com/v1beta/openai",
                no_temperature_prefixes=("gemini-3.5", "gemini-3.6"),
            )
        orouter = keys.get("openrouter") or env.get("OPENROUTER_API_KEY")
        if orouter:
            self._configured_keys["openrouter"] = orouter
            self._credential_sources["openrouter"] = key_sources.get(
                "openrouter", default_source
            )
            self._adapters["openrouter"] = OpenAICompatAdapter(
                orouter, "https://openrouter.ai/api/v1"
            )
        endpoint_ids = [endpoint.endpoint_id for endpoint in local_endpoints]
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ValueError("local model endpoint ids must be unique")
        if len({endpoint.origin for endpoint in local_endpoints}) != len(
            local_endpoints
        ):
            raise ValueError("local model endpoint origins must be unique")
        self.local_endpoints = tuple(local_endpoints)
        if self.local_endpoints:
            self._adapters["ollama"] = LocalEndpointAdapter(self.local_endpoints)

        self.cache = cache
        self.cache_mode: CacheMode = cache_mode
        self.chaos = ChaosMiddleware(chaos) if chaos else None
        self.max_retries = max_retries
        self.model_call_policy = model_call_policy
        self._client: httpx.AsyncClient | None = None
        self._client_loop: object | None = None
        # Live observability counters: per-provider attempt/error/latency
        # tallies and cache hit/miss counts, accumulated as real traffic
        # flows through the router.
        self.cache_hits = 0
        self.cache_misses = 0
        self._provider_stats: dict[str, dict] = {}

    @property
    def client(self) -> httpx.AsyncClient:
        # The client must be bound to the CURRENT event loop. Sync callers wrap each
        # router call in its own asyncio.run (a fresh loop each time), so a client
        # whose pooled connection lives on a prior, now-closed loop fails with
        # "Event loop is closed". Recreate when the running loop changes; on the
        # server's persistent loop this is a no-op, so connection pooling is preserved.
        try:
            loop: object | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient()
            self._client_loop = loop
        elif self._client_loop is not None and self._client_loop is not loop:
            # an AUTO-CREATED client bound to a stale loop — recreate for this loop.
            # (An injected/mock client has _client_loop=None and is left untouched, so
            # tests / cassettes that set router._client keep working.)
            self._client = httpx.AsyncClient()
            self._client_loop = loop
        return self._client

    def providers(self) -> list[str]:
        return list(self._adapters.keys())

    def configured_keys(self) -> dict[str, str]:
        return dict(self._configured_keys)

    def configured_key_sources(self) -> dict[str, str]:
        """Provider -> non-secret source token for the selected credential."""
        return dict(self._credential_sources)

    def has_explicit_key_source(self, provider: str) -> bool:
        """Whether a composition boundary explicitly assigned ownership."""
        return provider in self._explicit_credential_sources

    def credential_source_for(self, provider: str) -> str:
        return self._credential_sources.get(
            provider, "local" if provider == "ollama" else "none"
        )

    def adapter_for(self, provider: str):
        """The configured adapter for a provider, or None. Bespoke non-chat
        calls (e.g. the transcribe recipe's remote /audio/transcriptions
        engine) borrow its credentials/base_url; the chat path stays
        complete() — the router deliberately has no audio path."""
        return self._adapters.get(provider)

    def local_endpoint_url_for_model(self, model: str) -> str | None:
        """Return the exact local-server origin named by ``model``.

        User-facing remediation calls this after a failed request. Keeping the
        endpoint qualifier intact there prevents a failure from naming the
        wrong configured server.
        """

        try:
            endpoint, _adapter, _bare_model = self.resolve_local_model(model)
        except LLMError:
            return None
        return endpoint.origin

    def resolve_local_model(
        self, model: str
    ) -> tuple[LocalModelEndpointConfig, OpenAICompatAdapter, str]:
        adapter = self._adapters.get("ollama")
        if not isinstance(adapter, LocalEndpointAdapter):
            raise LLMError("no local model endpoints are configured")
        return adapter.resolve(model)

    def secret_values_for_model(self, model: str) -> tuple[str, ...]:
        """Return only the credential selected by one exact model identity."""
        if provider_from_model_id(model) == "ollama":
            _endpoint, adapter, _bare_model = self.resolve_local_model(model)
            return self._adapter_secret_values(adapter)
        return self._adapter_secret_values(
            self._adapters.get(provider_from_model_id(model))
        )

    def _stats_for(self, provider: str) -> dict:
        return self._provider_stats.setdefault(
            provider,
            {
                "calls": 0,
                "errors": 0,
                "latency_ms_total": 0,
                "last_error": None,
                "last_error_at": None,
                # schema_rejects: how many times the structured-output completer's
                # jsonschema validation rejected this provider's output. Turning
                # Anthropic validation on converts some previously-silent passes
                # into bounded-repair-then-maybe-failure; this counter makes that
                # rate observable via health_snapshot, sitting beside
                # errors/error_rate.
                "schema_rejects": 0,
            },
        )

    def note_schema_reject(self, provider: str) -> None:
        """Record one structured-output schema rejection for ``provider``.

        Called by the StructuredCompleter's validator each time jsonschema
        rejects a wire output (before it asks pydantic-ai to repair). Kept on
        the router so the count rides the same per-provider stats dict the
        health endpoint already surfaces — not a new ad hoc sink."""
        self._stats_for(provider)["schema_rejects"] += 1

    def health_snapshot(self) -> dict:
        """Observed live stats for the admin health endpoint: per-provider
        attempt counts, error rate and mean latency, plus cache hit/miss
        tallies — everything accumulated from real router traffic."""
        providers: dict[str, dict] = {}
        for name in self._adapters:
            s = self._stats_for(name)
            calls = s["calls"]
            providers[name] = {
                "calls": calls,
                "errors": s["errors"],
                "error_rate": round(s["errors"] / calls, 4) if calls else 0.0,
                "avg_latency_ms": (
                    int(s["latency_ms_total"] / calls) if calls else None
                ),
                "last_error": s["last_error"],
                "last_error_at": s["last_error_at"],
                "schema_rejects": s["schema_rejects"],
            }
        return {
            "providers": providers,
            "cache": {"hits": self.cache_hits, "misses": self.cache_misses},
        }

    async def probe_providers(self, timeout: float = 2.0) -> dict[str, dict]:
        """Actively probe each configured provider's API endpoint and report
        reachability + round-trip latency. Any HTTP response (even 401 from
        an unauthenticated models listing) proves the host is reachable;
        connect errors and timeouts mean it isn't."""

        async def one(name: str, adapter: object) -> tuple[str, dict]:
            base = getattr(adapter, "base_url", "")
            url = base + ("/v1/models" if name == "anthropic" else "/models")
            t0 = time.perf_counter()
            try:
                r = await self.client.get(url, timeout=timeout)
                return name, {
                    "reachable": True,
                    "latency_ms": int((time.perf_counter() - t0) * 1000),
                    "status": r.status_code,
                }
            except Exception as e:  # noqa: BLE001 — a probe reports, never raises
                secret_values = self._adapter_secret_values(adapter)
                error = redact_text(
                    e,
                    secret_values=secret_values,
                    max_chars=200,
                )
                return name, {
                    "reachable": False,
                    "latency_ms": int((time.perf_counter() - t0) * 1000),
                    "error": error or type(e).__name__,
                }

        targets = [
            (name, adapter)
            for name, adapter in self._adapters.items()
            if name != "ollama"
        ]
        local_adapter = self._adapters.get("ollama")
        if isinstance(local_adapter, LocalEndpointAdapter):
            targets.extend(
                (
                    f"ollama/@{endpoint.endpoint_id}",
                    local_adapter.adapter_for_endpoint(endpoint.endpoint_id),
                )
                for endpoint in self.local_endpoints
            )
        pairs = await asyncio.gather(*(one(n, a) for n, a in targets))
        return dict(pairs)

    async def complete(
        self,
        req: LLMRequest,
        *,
        recipe_version: str = "1",
        trace: ModelTrace | None = None,
    ) -> LLMResponse:
        """When ``trace`` is given, the router
        appends one event dict per cache decision and per adapter attempt —
        including every retried error and any chaos fault — so a run trace
        can show exactly what happened on the wire.

        Schema-bearing calls enter the structured completer here so callers of
        the public router seam get uniform validation and one bounded repair
        turn. The completer calls :meth:`complete_transport` for each wire
        attempt, keeping cache/chaos/retry below the repair loop without
        recursively re-entering this wrapper.
        """
        if req.schema is not None:
            # Local import avoids a router <-> structured module import cycle.
            from .structured import StructuredCompleter, StructuredRequest

            try:
                result = await StructuredCompleter(self).complete(
                    StructuredRequest(
                        model=req.model,
                        messages=req.messages,
                        schema=req.schema,
                        method=req.mechanism or "auto",
                        repair_attempts=1,
                        max_tokens=req.max_tokens,
                        temperature=req.temperature,
                        params=req.params,
                        reasoning_policy=req.reasoning_policy,
                    ),
                    recipe_version=recipe_version,
                    trace=trace,
                )
            except LLMError as error:
                raise self._safe_llm_error(
                    error,
                    self.secret_values_for_model(req.model),
                ) from None
            return result.response

        return await self.complete_transport(
            req, recipe_version=recipe_version, trace=trace
        )

    async def complete_transport(
        self,
        req: LLMRequest,
        *,
        recipe_version: str = "1",
        trace: ModelTrace | None = None,
    ) -> LLMResponse:
        """Execute one wire call through the router's public transport seam."""
        return await self._complete_transport(
            req, recipe_version=recipe_version, trace=trace
        )

    async def _complete_transport(
        self,
        req: LLMRequest,
        *,
        recipe_version: str = "1",
        trace: ModelTrace | None = None,
    ) -> LLMResponse:
        """Execute one router-level wire attempt below structured repair."""
        # The routed provider, stamped on every response leaving this seam --
        # live, chaos-fabricated and replayed alike -- so no downstream site
        # has to re-parse the model string to recover it. Cached bodies
        # predate the field, so a replay is stamped here too (like
        # credential_source) rather than trusting whatever the cassette held.
        provider = provider_from_model_id(req.model)
        credential_source = self.credential_source_for(provider)
        if self.model_call_policy is not None:
            req = self.model_call_policy.prepare(req, credential_source)
        provider = provider_from_model_id(req.model)
        credential_source = self.credential_source_for(provider)
        if req.reasoning_policy is not None and provider != "openrouter":
            raise LLMError("reasoning_policy is supported only for openrouter models")
        key = request_key(req, recipe_version)

        if self.cache is not None and self.cache_mode == "replay":
            hit = self.cache.get(key)
            if hit is not None:
                self.cache_hits += 1
                if trace is not None:
                    trace.append({"event": "cache", "hit": True, "key": key})
                hit.credential_source = "cache"
                hit.provider = provider
                hit.cost_source = (
                    model_cost_source(req.model) if hit.cost is not None else "unknown"
                )
                # A replay made no wire call, so it has no runtime. Clear it
                # for the same reason credential_source is restamped: never
                # trust a duration the cassette happens to hold.
                hit.duration_ms = None
                return hit
            self.cache_misses += 1
            if trace is not None:
                trace.append({"event": "cache", "hit": False, "key": key})
        if self.cache is not None and self.cache_mode == "replay_strict":
            hit = self.cache.get(key)
            if hit is not None:
                self.cache_hits += 1
                if trace is not None:
                    trace.append({"event": "cache", "hit": True, "key": key})
                hit.credential_source = "cache"
                hit.provider = provider
                hit.cost_source = (
                    model_cost_source(req.model) if hit.cost is not None else "unknown"
                )
                # A replay made no wire call, so it has no runtime. Clear it
                # for the same reason credential_source is restamped: never
                # trust a duration the cassette happens to hold.
                hit.duration_ms = None
                return hit
            self.cache_misses += 1
            raise CacheMiss(key)

        resp = await self._call_with_retry(
            req, credential_source=credential_source, trace=trace
        )
        resp.provider = provider
        resp.credential_source = credential_source
        # This seam still holds the canonical endpoint-qualified identity after
        # the local adapter receives its stripped wire model. Stamp the fact for
        # custom adapters too; accounting never re-parses the model later.
        resp.cost_source = (
            model_cost_source(req.model) if resp.cost is not None else "unknown"
        )
        if self.model_call_policy is not None:
            self.model_call_policy.after_live(req, resp, credential_source)
        if self.cache is not None and self.cache_mode in ("replay", "fresh"):
            self.cache.put(key, resp)
        return resp

    async def _call_with_retry(
        self,
        req: LLMRequest,
        *,
        credential_source: str,
        trace: ModelTrace | None = None,
    ) -> LLMResponse:
        provider = provider_from_model_id(req.model)
        adapter = self._adapters.get(provider)
        if adapter is None:
            raise LLMError(
                f"no adapter for provider '{provider}' "
                f"(configured: {', '.join(self._adapters)})"
            )
        wire_req = req
        if provider == "ollama":
            _endpoint, adapter, bare_model = self.resolve_local_model(req.model)
            wire_req = replace(req, model=f"ollama/{bare_model}")
        last: Exception | None = None

        # The adapter, rather than this generic router, owns the credential.
        # Use only that already-held value; do not enumerate router keys.
        secret_values = self._adapter_secret_values(adapter)

        def _record(attempt: int, t0: float, **extra: object) -> int:
            # health stats: every adapter attempt (ok, chaos or error) counts
            stats = self._stats_for(provider)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            stats["calls"] += 1
            stats["latency_ms_total"] += latency_ms
            if extra.get("outcome") == "error":
                stats["errors"] += 1
                stats["last_error"] = str(extra.get("error", ""))[:300]
                stats["last_error_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                )
            if trace is not None:
                trace.append(
                    {
                        "event": "attempt",
                        "attempt": attempt,
                        "latency_ms": latency_ms,
                        **extra,
                    }
                )
            # The SAME measurement the health stats and the trace already
            # take — returned instead of re-read, so the durable model-call
            # fact cannot disagree with the trace line beside it.
            return latency_ms

        for attempt in range(self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                if self.chaos is not None:
                    fabricated = await self.chaos.intercept(req)
                    if fabricated is not None:
                        fabricated.duration_ms = _record(
                            attempt, t0, outcome="chaos_fabricated"
                        )
                        return fabricated
                if self.model_call_policy is not None:
                    self.model_call_policy.before_live(req, credential_source)
                if provider == "ollama":
                    assert isinstance(adapter, OpenAICompatAdapter)
                    resp = await adapter.complete(
                        wire_req,
                        self.client,
                        cost_model=req.model,
                    )
                else:
                    resp = await adapter.complete(wire_req, self.client)
                if provider == "ollama":
                    resp.model = req.model
                resp.duration_ms = _record(attempt, t0, outcome="ok")
                return resp
            except LLMError as e:
                safe_error = self._safe_llm_error(e, secret_values)
                last = safe_error
                chaos_error = bool(
                    self.chaos is not None and str(safe_error).startswith("chaos:")
                )
                retry_is_proven_safe = (
                    chaos_error
                    or safe_error.transport_kind == "connect"
                    # A provider response that categorically rejected the
                    # request is not an ambiguous accepted completion.
                    or safe_error.status == 429
                )
                safe_error.post_egress_ambiguous = bool(
                    safe_error.retryable and not retry_is_proven_safe
                )
                _record(
                    attempt,
                    t0,
                    outcome="error",
                    error=str(safe_error),
                    status=safe_error.status,
                    retryable=safe_error.retryable,
                    chaos=chaos_error,
                )
                # A SchemaViolation is never blind-retried here with the
                # identical body (`SchemaViolation.retryable=False`) — the
                # StructuredCompleter owns schema repair for every
                # schema-bearing caller, so a transport-level retry could
                # never fix it anyway.  More generally, ``retryable`` is an
                # eventual-resume classification, not proof that another
                # immediate wire call is safe: an adapter can raise after the
                # provider accepted work but before its response was decoded.
                # Only a pre-egress connect/chaos failure or a categorical
                # rate-limit rejection may be retried inside the router.
                if (
                    not safe_error.retryable
                    or not retry_is_proven_safe
                    or attempt == self.max_retries
                ):
                    raise safe_error from None
                await asyncio.sleep(
                    min(2**attempt * 0.5, 8.0) * (1.0 if self.chaos else 1.0)
                )
            except httpx.HTTPError as e:
                last = self._safe_transport_error(e, secret_values)
                detail = str(last)
                _record(
                    attempt,
                    t0,
                    outcome="error",
                    error=detail,
                    retryable=True,
                )
                # ConnectError proves no connection was established. Other
                # transport failures (read timeout, protocol disconnect,
                # partial write) may occur after accepted provider work and
                # therefore stay with the durable row reservation for
                # reconciliation rather than buying the row again.
                if last.transport_kind != "connect" or attempt == self.max_retries:
                    raise last from None
                await asyncio.sleep(min(2**attempt * 0.5, 8.0))
        raise LLMError(redact_text(f"exhausted retries: {last}")) from None

    def has_embedding_backend(self) -> bool:
        """True when a provider that can serve embeddings is configured.

        Embeddings are the hosted/GPU upgrade behind semantic search; the
        keyless base image has none, and callers fall back to lexical search
        rather than fake meaning-ranking. Only providers configured with an
        explicit key and a reliable /embeddings endpoint count. Configured
        local endpoints are not yet an embedding-index capability.
        """
        return bool({"openai", "gemini"} & set(self._adapters))

    async def embed(
        self, texts: list[str], *, model: str = "openai/text-embedding-3-small"
    ) -> list[list[float]]:
        """Embed ``texts`` into vectors via an OpenAI-compatible /embeddings
        endpoint. Raises LLMError if the provider isn't configured.

        Vector-only by design: this is the seam ``frisket.semantic`` already
        depends on. Native embeddings call :meth:`embed_batch` for the
        metadata-rich result."""
        provider = provider_from_model_id(model)
        adapter = self._adapters.get(provider)
        requested = model.split("/", 1)[-1]
        if provider == "ollama":
            _endpoint, adapter, requested = self.resolve_local_model(model)
        if adapter is None or not hasattr(adapter, "embed"):
            raise LLMError(
                f"no embedding backend for '{provider}' "
                f"(configured: {', '.join(self._adapters)})"
            )
        # Fresh per-call client: the sync embedding seam runs each call in its own
        # asyncio.run loop, so reusing the router's persistent client would bind a
        # pooled connection to a dead loop ("Event loop is closed") on the next call.
        async with httpx.AsyncClient() as client:
            try:
                return await adapter.embed(texts, requested, client)
            except LLMError as error:
                raise self._safe_llm_error(
                    error, self._adapter_secret_values(adapter)
                ) from None
            except httpx.HTTPError as error:
                raise self._safe_transport_error(
                    error, self._adapter_secret_values(adapter)
                ) from None

    async def embed_batch(
        self,
        texts: list[str],
        *,
        model: str = "openai/text-embedding-3-small",
        modality: str = "text",
    ) -> "EmbeddingBatchResult":
        """Metadata-rich embedding: vectors PLUS the provider/model facts native
        embedding spaces need (actual model id, provider id/kind, dimension,
        usage). The metadata-loss in :meth:`embed` is why this exists; existing
        vector-only callers stay on ``embed``."""
        from frisket.ai.embeddings import build_batch_result

        provider = provider_from_model_id(model)
        requested = model.split("/", 1)[-1]
        adapter = self._adapters.get(provider)
        if provider == "ollama":
            _endpoint, adapter, requested = self.resolve_local_model(model)
        if adapter is None or not hasattr(adapter, "embed_with_meta"):
            raise LLMError(
                f"no embedding backend for '{provider}' "
                f"(configured: {', '.join(self._adapters)})"
            )
        # fresh per-call client — see embed() above (asyncio.run-per-call must not
        # reuse a pooled connection bound to a closed loop).
        async with httpx.AsyncClient() as client:
            try:
                vectors, meta = await adapter.embed_with_meta(texts, requested, client)
            except LLMError as error:
                raise self._safe_llm_error(
                    error, self._adapter_secret_values(adapter)
                ) from None
            except httpx.HTTPError as error:
                raise self._safe_transport_error(
                    error, self._adapter_secret_values(adapter)
                ) from None
        provider_cost_usd = (
            0.0 if provider == "ollama" else meta.get("provider_cost_usd")
        )
        cost_source = (
            "free_local"
            if provider == "ollama"
            else meta.get("cost_source")
            or ("provider_reported" if provider_cost_usd is not None else "unknown")
        )
        return build_batch_result(
            vectors,
            provider_id=provider,
            provider_kind=_provider_kind(provider),
            requested_model=model,
            actual_model_id=(
                model
                if provider == "ollama" and meta.get("actual_model_id") == requested
                else meta.get("actual_model_id") or requested
            ),
            modality=modality,
            dimension=meta.get("dimension"),
            usage=meta.get("usage") or {},
            provider_request_id=meta.get("provider_request_id"),
            credential_source=self.credential_source_for(provider),
            provider_reported_cost_usd=meta.get("provider_reported_cost_usd"),
            provider_cost_usd=provider_cost_usd,
            cost_source=cost_source,
        )

    @staticmethod
    def _adapter_secret_values(adapter: object | None) -> tuple[str, ...]:
        key = getattr(adapter, "api_key", None)
        return (key,) if isinstance(key, str) else ()

    @staticmethod
    def _safe_llm_error(error: LLMError, secret_values: tuple[str, ...]) -> LLMError:
        detail = redact_text(error, secret_values=secret_values)
        if isinstance(error, SchemaViolation):
            return SchemaViolation(
                detail,
                raw_text=(
                    redact_text(
                        error.raw_text,
                        secret_values=secret_values,
                        max_chars=4096,
                        one_line=False,
                    )
                    if error.raw_text is not None
                    else None
                ),
            )
        provider_code = error.provider_code
        if provider_code is not None:
            provider_code = redact_text(
                provider_code,
                secret_values=secret_values,
                max_chars=256,
            )
        try:
            transport_kind = getattr(error, "transport_kind", None)
        except BaseException:  # hostile legacy subclasses must fail closed
            transport_kind = None
        try:
            post_egress_ambiguous = bool(getattr(error, "post_egress_ambiguous", False))
        except BaseException:  # hostile legacy subclasses must fail closed
            post_egress_ambiguous = True
        if transport_kind is None and isinstance(error.__cause__, httpx.ConnectError):
            transport_kind = "connect"
        return LLMError(
            detail,
            status=error.status,
            retryable=error.retryable,
            provider_code=(
                canonical_error_code(provider_code, fallback="provider_error")
                if provider_code is not None
                else None
            ),
            transport_kind=transport_kind,
            post_egress_ambiguous=post_egress_ambiguous,
        )

    @staticmethod
    def _safe_transport_error(
        error: httpx.HTTPError, secret_values: tuple[str, ...]
    ) -> LLMError:
        cause_detail = redact_text(error, secret_values=secret_values)
        return LLMError(
            redact_text(
                f"transport error: {cause_detail}", secret_values=secret_values
            ),
            retryable=True,
            transport_kind="connect" if isinstance(error, httpx.ConnectError) else None,
            post_egress_ambiguous=not isinstance(error, httpx.ConnectError),
        )

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except RuntimeError:
                # client bound to an already-closed event loop — nothing to
                # release that process teardown won't handle
                pass
