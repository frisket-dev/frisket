from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from frisket.ai.llm.types import validate_credential_source

# LLM chat-provider kinds, shared by the network gate, the router, and the
# embeddings store as ONE table. Remote hosted APIs are platform_api; ollama is
# an operator-local HTTP server whose locality is a shipped-default
# convention (the URL is configurable), not a structural guarantee.
PROVIDER_KIND: dict[str, str] = {
    "openai": "platform_api",
    "gemini": "platform_api",
    "openrouter": "platform_api",
    "anthropic": "platform_api",
    "ollama": "local_http",
}

# Provider kinds whose calls leave the operator's infrastructure. Shared with
# embeddings/store.py's refresh gate.
REMOTE_PROVIDER_KINDS = frozenset(
    {"platform_api", "platform_http", "platform_function", "org_key_api"}
)


def is_remote_provider(provider: str) -> bool:
    """Server-side remoteness of a provider id string. Unknown providers are
    treated as remote (fail closed) — the same default the router applies
    when stamping provider_kind on call facts."""
    return PROVIDER_KIND.get(provider, "platform_api") in REMOTE_PROVIDER_KINDS


# THE provider-cost domain. Four surfaces used to decide independently what
# counts as unknown, invalid, or acceptable money — fact insertion, terminal
# enrichment, the SQL run projection, and the receipt derivation below — and
# they disagreed (a 1.1e308 cost was unknown to SQL, a number in Python, and
# 0.0 by the time the SDK printed it). They all read this rule now; the SQL
# spelling binds these constants rather than repeating a literal.
#
# The domain bounds a fact AND a sum of facts, because both are computed.  A
# per-fact maximum alone cannot prove a total finite: the bound used to be
# 1e308 with a comment claiming a sum could not reach inf, and two facts at
# 1e308 summed to inf — SQL answered NULL, Python answered inf, and the
# spend accrual raised OverflowError converting inf to micro-dollars.
#
# Above these, a "cost" is not a dollar amount, it is a corrupt meter
# reading.  One provider call above $1M and one execution above $1B are both
# far past any real invoice, and the per-fact bound is small enough that no
# reachable number of facts can overflow a float.
PROVIDER_COST_MAX_USD = 1e6
PROVIDER_COST_TOTAL_MAX_USD = 1e9


def provider_cost_value(raw: Any) -> float | None:
    """The dollar value of one provider-cost fact, or ``None`` if there isn't
    one.

    NULL, non-numeric, non-finite, negative, and absurd (above
    :data:`PROVIDER_COST_MAX_USD`) all answer the same way: this call's cost is
    NOT KNOWN. Callers that add costs up must propagate that unknown rather
    than substitute zero — a real BYOK call must never render like a free
    local one.
    """
    if raw is None:
        return None
    try:
        cost = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(cost) or cost < 0 or cost > PROVIDER_COST_MAX_USD:
        return None
    return cost


def validate_provider_cost(raw: Any, *, call_id: str) -> float | None:
    """The same domain, applied where a cost is WRITTEN.

    An absent cost (``None``) is a legitimate honest unknown and passes
    through. A STATED cost the domain cannot accept — negative, non-finite,
    absurd, non-numeric — is a defect at the source and refuses here, naming
    the knob, instead of being stored and silently reinterpreted as unknown
    downstream (which would let a negative "cost" credit a spend cap while the
    run figure stayed NULL).
    """
    if raw is None:
        return None
    cost = provider_cost_value(raw)
    if cost is None:
        raise ValueError(
            f"model-call {call_id!r}: provider_cost_usd must be a finite "
            f"non-negative number no greater than {PROVIDER_COST_MAX_USD:g} "
            f"(or NULL for an honest unknown), got {raw!r}"
        )
    return cost


# The widest value the ``model_calls.duration_ms`` column can hold: SQLite
# INTEGER is signed 64-bit, and sqlite3 raises OverflowError — not a
# constraint failure — for anything past it. The bound is enforced in the
# domain below rather than at the two insert sites, so it cannot be enforced
# in one writer and forgotten in the other. (~292 million years; a "duration"
# anywhere near it is a corrupt reading, not a slow call.)
DURATION_MS_MAX = 2**63 - 1


def duration_ms_value(raw: Any) -> int | None:
    """The write-side domain for a model call's measured runtime.

    Deliberately NOT shaped like :func:`validate_provider_cost`: a duration is
    descriptive, so an unusable value must not be able to refuse a fact that a
    provider was already paid for. Absent, non-numeric, non-finite, negative,
    and past :data:`DURATION_MS_MAX` all answer the same way — "not measured"
    (NULL) — which is exactly what the column already means for every
    unbracketed transport.

    The bound is checked on the FLOAT, before narrowing to ``int``, because
    the float round-trip is itself lossy at that magnitude: ``float(2**63 - 1)``
    is ``2**63``, so an in-range input would otherwise come back out of range
    and raise OverflowError inside the INSERT — discarding a fact for a
    provider that was already paid, which is exactly what this domain exists
    to prevent.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or value < 0 or value > DURATION_MS_MAX:
        return None
    return int(value)


def provider_cost_total(raw_costs: Iterable[Any]) -> float | None:
    """THE aggregate operation of the cost domain: bounded addition.

    Every surface that adds provider costs up uses this one rule, so they
    cannot answer differently. A value the domain cannot accept makes the
    total unknown (``None``) rather than contributing zero, and a total past
    :data:`PROVIDER_COST_TOTAL_MAX_USD` is a corrupt aggregate and answers
    unknown too — never ``inf``, which is neither a dollar amount nor
    something a spend counter can hold.

    The SQL spelling of this same rule lives in
    ``RunResultStore._project_cost_actual`` and is pinned to this one by a
    parity test; it binds both constants rather than repeating a literal.
    """
    total = 0.0
    for raw in raw_costs:
        cost = provider_cost_value(raw)
        if cost is None:
            return None
        total += cost
        if total > PROVIDER_COST_TOTAL_MAX_USD:
            return None
    return round(total, 8)


def model_calls_cost_actual(model_calls: Iterable[Any]) -> float | None:
    """Derive this execution's provider cost from complete durable facts.

    Cache rows can retain historical price provenance but incurred no cost in
    the current execution. Every live known cost contributes to the sum; one
    live cost that :func:`provider_cost_value` cannot value — or a total
    :func:`provider_cost_total` will not accept — makes the receipt aggregate
    unknown instead of manufacturing an exact zero.
    """

    def value(call: Any, key: str) -> Any:
        try:
            return call[key]
        except (IndexError, KeyError, TypeError):
            return None

    return provider_cost_total(
        value(call, "provider_cost_usd")
        for call in model_calls
        if value(call, "credential_source") != "cache"
    )


@dataclass(frozen=True)
class ModelCallMeta:
    """Neutral provider facts for one model capability call.

    This code records facts; a deployment's settlement policy prices them
    package boundary. A fact carries what
    actually happened at the provider — units, provider-reported/derived cost,
    cost source, cache evidence, and the credential provenance of the call —
    and never a tariff, a customer price, or a billability verdict.
    """

    capability: str
    fact_version: str = field(default="frisket.model-call-fact.v1", init=False)
    engine: str
    provider: str
    provider_kind: str
    model_ids: list[str] = field(default_factory=list)
    credential_source: str = "none"
    provider_reported_cost_usd: float | None = None
    provider_cost_usd: float | None = None
    cost_source: str = "unknown"
    units: dict[str, float | int | str | bool] = field(default_factory=dict)
    cache: dict[str, float | int | str | bool] = field(default_factory=dict)
    request_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    # How long THIS call took, milliseconds, measured monotonically at the site
    # that brackets the request. Descriptive only — nothing branches on it and
    # it is never an input to cost. NULL means "not measured here", which is
    # the truthful answer for a cache hit (no call happened) and for any
    # transport whose bracket is not wired yet; it is never guessed from a
    # wall-clock read at the fact-assembly site, which would silently include
    # media materialization and row bookkeeping.
    duration_ms: int | None = None
    id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        validate_credential_source(self.credential_source)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "fact_version": self.fact_version,
            "capability": self.capability,
            "engine": self.engine,
            "provider": self.provider,
            "provider_kind": self.provider_kind,
            "model_ids": self.model_ids,
            "credential_source": self.credential_source,
            "provider_reported_cost_usd": self.provider_reported_cost_usd,
            "provider_cost_usd": self.provider_cost_usd,
            "cost_source": self.cost_source,
            "units": self.units,
            "cache": self.cache,
            "request_id": self.request_id,
            "warnings": self.warnings,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def local(
        cls,
        *,
        capability: str,
        engine: str,
        provider: str = "local",
        model_ids: list[str] | None = None,
        units: dict[str, float | int | str | bool] | None = None,
        warnings: list[str] | None = None,
    ) -> "ModelCallMeta":
        return cls(
            capability=capability,
            engine=engine,
            provider=provider,
            provider_kind="local_process",
            model_ids=model_ids or [],
            credential_source="local",
            provider_cost_usd=0.0,
            provider_reported_cost_usd=0.0,
            cost_source="free_local",
            units=units or {},
            warnings=warnings or [],
        )

    @classmethod
    def sidecar(
        cls,
        *,
        capability: str,
        engine: str,
        model_ids: list[str] | None = None,
        units: dict[str, float | int | str | bool] | None = None,
        warnings: list[str] | None = None,
    ) -> "ModelCallMeta":
        return cls(
            capability=capability,
            engine=engine,
            provider="frisket-sidecar",
            provider_kind="local_http",
            model_ids=model_ids or [],
            credential_source="local",
            provider_cost_usd=0.0,
            provider_reported_cost_usd=0.0,
            cost_source="free_local",
            units=units or {},
            warnings=warnings or [],
        )

    @classmethod
    def provider_call(
        cls,
        *,
        capability: str,
        engine: str,
        provider: str,
        provider_kind: str,
        model_ids: list[str] | None = None,
        credential_source: str,
        provider_reported_cost_usd: float | None,
        provider_cost_usd: float | None,
        units: dict[str, float | int | str | bool] | None = None,
        cost_source: str,
        request_id: str | None = None,
        warnings: list[str] | None = None,
        duration_ms: int | None,
    ) -> "ModelCallMeta":
        """A real call against an external provider.

        `credential_source` is the per-call credential provenance an external
        settlement consumer keys pricing on; it is a small enum-like token
        (platform_key / project_key / org_byok / cache / local), never key
        material.

        `duration_ms` is required so no call site can forget it: pass the
        bracketed monotonic elapsed time, or explicitly pass ``None`` when
        this transport is not bracketed — NULL means "not measured here",
        never a fabricated number.
        """
        return cls(
            capability=capability,
            engine=engine,
            provider=provider,
            provider_kind=provider_kind,
            model_ids=model_ids or [],
            credential_source=credential_source,
            provider_reported_cost_usd=provider_reported_cost_usd,
            provider_cost_usd=provider_cost_usd,
            cost_source=cost_source,
            units=units or {},
            request_id=request_id,
            warnings=warnings or [],
            duration_ms=duration_ms,
        )

    @classmethod
    def cache_hit(
        cls,
        *,
        capability: str,
        engine: str,
        provider: str,
        provider_kind: str,
        model_ids: list[str] | None = None,
        units: dict[str, float | int | str | bool] | None = None,
        request_id: str | None = None,
        warnings: list[str] | None = None,
    ) -> "ModelCallMeta":
        return cls(
            capability=capability,
            engine=engine,
            provider=provider,
            provider_kind=provider_kind,
            model_ids=model_ids or [],
            credential_source="cache",
            provider_reported_cost_usd=0.0,
            provider_cost_usd=0.0,
            cost_source="cache_hit",
            units=units or {},
            cache={"hit": True},
            request_id=request_id,
            warnings=warnings or [],
        )
