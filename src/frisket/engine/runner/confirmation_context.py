"""The ONE confirmation envelope every 402 gate mints its quote hash from.

Consent used to be minted at four sites with three private payload shapes —
one per executor architecture plus one per params style — so "what does this
approval bind" had three answers and no reader could compare them. A
confirmation minted by one architecture could, in principle, hash-collide
with the other's; nothing in the payloads said which family produced them.

This module holds one envelope and one mint:

  * :class:`MoneyConfirmationContext` — a gate that costs money. Carries a
    :class:`ConsentQuote`, the typed projection of the money-bearing
    estimate fields.
  * :class:`ScopeConfirmationContext` — a gate that binds SCOPE and has no
    price at all (``derive.join``'s fan-out cap, ``derive.collection_expand``'s
    preview cap). It has no ``quote`` field to null out: modelled as a
    separate variant precisely so a ``cost: None`` sentinel cannot creep into
    a gate that was never about cost.
  * :func:`mint_confirmation_hash` — the ONE hash. Both variants carry
    ``family_kind`` INSIDE the hashed payload, so a confirmation minted for
    one family can never be echoed back to satisfy another's gate, in either
    executor architecture.

The scope union names WHICH identity the approval is pinned to, and the
variant tag is hashed, so the three architectures can never collide:

  * :class:`RunnerScope` — the MapRunner lane (runner spec, row ids,
    canonical work scope).
  * :class:`ActionScope` — families that hold the whole ``ActionSpec``
    envelope (its confirmed-excluded params hash IS the identity).
  * :class:`ParamsScope` — deterministic families that receive typed params
    rather than the outer envelope.

Two projection policies, and they differ for a reason (see
:func:`recipe_confirmation` and :func:`action_confirmation`):

  * The recipe lane DROPS rostered display keys. Adding a page count or a
    warning to a recipe's ``estimate()`` must not invalidate every persisted
    consent — that is the defect ``ConsentQuote`` was introduced to fix — so
    the roster is a closed classification and an unclassified key REFUSES at
    mint rather than silently changing (or silently not changing) the quote.
  * The executor families BIND every non-quote key. They hash their whole
    estimate today and nothing is dropped, so no key can go unbound: a new
    money-bearing fact appearing in one of those estimates changes the hash
    and forces a re-confirm whether or not anyone classified it. Where
    nothing is dropped there is nothing for a roster to protect.

Placed beside ``confirmation_echo.py``, which solved the same problem for
the comparison side: the runner validation gates, the executor families,
and the authoring workbench all import it without a layering or import-cycle
concern (contracts is a leaf; nothing here reaches back into the engine).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError

from frisket.contracts.action import ContractModel
from frisket.execution.pricing_policy import (
    MICROS_PER_USD,
    PersistedRating,
    PricingPolicy,
    QuoteFacts,
    Rating,
    Unpriceable,
    usd_to_micros,
)

#: The two additive estimate keys the pricing-policy seam writes. Named
#: constants because they are a WIRE spelling: they appear on the 402
#: envelope, on the estimate endpoint's payload, in the persisted consent
#: quote, and in the web client's ``RunEstimate``.
BILLED_COST_KEY = "billed_cost"
POLICY_ID_KEY = "policy_id"

# Every model whose dump reaches the mint. ``ser_json_inf_nan="constants"``
# is load-bearing, not tidiness: pydantic's DEFAULT renders a non-finite
# float as JSON ``null``, and these envelopes carry free-form
# ``dict[str, Any]`` payloads (a runner spec, a params dump, a family's
# bindings) that pydantic would quietly flatten. Under the default, a spec
# carrying NaN, one carrying +Inf, and one carrying null all dump to the same
# bytes and mint the SAME consent — three different runs, one approval.
# "constants" leaves the float intact so the ``allow_nan=False`` encode in
# :func:`mint_confirmation_hash` refuses it, which is what the raw
# ``json.dumps`` of the pre-consolidation payloads did.
_HASHABLE_PAYLOAD = ConfigDict(
    extra="forbid", frozen=True, ser_json_inf_nan="constants"
)


class ConsentQuoteRefused(ValueError):
    """An estimate whose money facts cannot be projected — so no confirmation
    hash exists for it, at mint or at dispatch.

    Fails closed and names the knob. The alternatives it replaces all
    identify nothing: pydantic's ``ValidationError`` (which also carries the
    offending VALUE into logs), ``json.dumps``'s "Out of range float values"
    on a non-finite cost, and a silent coercion. This carries the key name
    only.

    A ``ValueError``, which is the spec-validation refusal family both the
    queued path (``server/action_enqueue.py``'s ``prepare_run_error``) and
    direct callers already map to an actionable error — the same seat
    ``ExecutionResolutionRefused`` uses. Dispatch cannot use that seat (its
    only modeled outcome is refuse-and-re-confirm), so
    ``execution/attempt_authority.py`` translates it to ``consent_missing``.
    """

    def __init__(self, key: str, problem: str):
        super().__init__(
            f"this run's cost estimate cannot be confirmed: its {key!r} "
            f"{problem}. There is no quote to approve until the recipe's "
            "estimate() stops emitting it that way."
        )
        self.key = key


def _quote_amount(estimate: Mapping[str, Any], key: str) -> float | None:
    value = estimate.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConsentQuoteRefused(key, "is not a number")
    if not math.isfinite(value):
        # Refused HERE, by name, rather than four frames later inside
        # json.dumps(allow_nan=False) as a bare "Out of range float values"
        # ValueError that names no key and, at dispatch, escapes as an
        # unhandled exception instead of the modeled re-confirm.
        #
        # It also makes the refusal independent of the encoder: pydantic's
        # ser_json_inf_nan default renders non-finite floats as JSON null,
        # so a payload encoded with model_dump_json() rather than
        # model_dump(mode="json") + json.dumps would hash NaN, +Inf, and an
        # honestly-unknown cost identically — three quotes, one consent.
        raise ConsentQuoteRefused(key, "is not a finite number")
    return float(value)


def _quote_count(estimate: Mapping[str, Any], key: str) -> int | None:
    value = estimate.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConsentQuoteRefused(key, "is not a whole count")
    return value


def _quote_name(estimate: Mapping[str, Any], key: str) -> str | None:
    value = estimate.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConsentQuoteRefused(key, "is not a string")
    return value


def _quote_micros(estimate: Mapping[str, Any], key: str) -> int | None:
    """A rated money figure, in integer micro-dollars.

    Stricter than :func:`_quote_amount` on purpose: a rated figure is minted
    by a pricing policy through :func:`usd_to_micros`, never typed by a recipe
    author, so a float or a negative here is a broken policy rather than a
    sloppy estimate — and both would put a number the user never saw inside
    their consent.
    """
    value = estimate.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConsentQuoteRefused(key, "is not a whole number of micro-dollars")
    if value < 0:
        raise ConsentQuoteRefused(key, "is negative")
    return value


def _quote_flag(estimate: Mapping[str, Any], key: str) -> bool:
    value = estimate.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        # Coercing would make the string "false" mean True in the hash.
        raise ConsentQuoteRefused(key, "is not a bool")
    return value


# Every estimate key this codebase produces that is NOT money-bearing, with
# the producer that emits it. Together with ``ConsentQuote``'s fields
# this is a CLOSED classification of the estimate envelope: a key in neither
# set refuses at mint (see ``from_estimate``), which is what makes the
# projection mechanically complete instead of remembered. Adding a key to a
# recipe's estimate() therefore forces one decision — money (a field above)
# or display (a line here) — at the moment the key appears, not at the
# moment somebody notices a mispriced consent.
KNOWN_DISPLAY_KEYS = frozenset(
    {
        "avg_input_tokens",
        "claims",
        "promise_set_hash",
        "billing_label",
        "venue_label",
        "audio_seconds",
        "warning",
    }
)


class ConsentQuote(ContractModel):
    """The money-bearing projection of an estimate that a confirmation binds.

    The estimate envelope carries two kinds of field: what the run will cost
    and who it pays (below), and what a client should render (page counts,
    token samples, character totals, warnings). Only the first kind belongs
    in the hash the user's approval is compared by. Embedding the raw
    estimate dict coupled consent validity to the second kind, so adding a
    display field silently invalidated every persisted consent; and a money
    field can no longer leave the hash by being forgotten, because the hash
    reads THIS shape.

    Absent and null project identically on purpose: today's producers spell
    "no pricing key" both ways (``unknown_cost_estimate`` omits it,
    ``estimate_external_cost`` can carry it as ``None``), and those two must
    not be two different consents.

    Named a QUOTE, not a cost basis, deliberately: this money seam already
    has two other things by that name — ``CostBasis``
    (``execution/promise_compiler.py``), the compiled promise input, and
    ``execution_attempts.cost_basis_json``, the settled figure the hosted
    side bills from. This is neither; it is what the user was shown and
    said yes to.
    """

    model_config = _HASHABLE_PAYLOAD

    # The quote itself: the dollar figure, the quantity it was quoted over,
    # how the figure was derived, and the rate identity it was priced under.
    #
    # ``cost`` is the PROVIDER's number, in float USD, forever. It is the
    # field every existing consumer reads (a downstream composition reads
    # exactly ``cost`` and ``pricing_key`` off this estimate), and a marked-up
    # figure appearing here would be marked up a second time downstream. The
    # billed figure is additive, below.
    cost: float | None = None
    rows: int | None = None
    cost_source: str | None = None
    pricing_key: str | None = None
    # What the user is actually BILLED, in integer micro-dollars, and the
    # identity of the pricing policy that said so
    # (``execution/pricing_policy.py``). Under base's identity policy this is
    # ``cost`` in micros; under a deployment's cost-plus tariff it is the
    # marked-up figure, and it is what the 402 quotes and the modal renders.
    #
    # ``billed_cost is None`` means the policy returned ``Unpriceable`` — an
    # honest cannot-say, which gates exactly as an unknown cost always has.
    # It is NOT "unrated": ``policy_id`` is required, so a quote that no
    # policy ever looked at refuses at the projection below rather than
    # hashing as if it had been priced.
    billed_cost: int | None = None
    policy_id: str = Field(min_length=1)
    # Who gets paid / where the data goes. Both explicit confirmation and
    # standing cost preapproval cover this provider call, including its inputs.
    engine: str | None = None
    requires_confirmation: bool = False
    remote_capability: str | None = None

    @classmethod
    def from_estimate(cls, estimate: Mapping[str, Any]) -> ConsentQuote:
        """The ONE projection site. Both the 402 mint and the dispatch
        recompute reach it through ``confirmation_context_hash``; adding a
        money-bearing fact is a field above plus a line here, never a second
        implementation somewhere a call site could disagree with.

        Refuses rather than guesses. An unclassified key means a producer
        added a fact nobody has decided the money-status of, and the failure
        it prevents is the silent one: a surcharge key added to an estimate
        would otherwise leave a $1.00 consent echo-valid for a $5.00
        dispatch, with nothing red anywhere. A malformed value refuses for
        the same reason — see ``ConsentQuoteRefused``."""

        unclassified = sorted(
            set(estimate) - set(cls.model_fields) - KNOWN_DISPLAY_KEYS
        )
        if unclassified:
            raise ConsentQuoteRefused(
                unclassified[0],
                "is neither a ConsentQuote field nor a known display key, "
                "so nobody has decided whether it changes what this run costs",
            )
        policy_id = _quote_name(estimate, POLICY_ID_KEY)
        if not policy_id:
            # THE fence for the whole pricing-policy seam, and the reason it
            # is a required field rather than an optional one. A money gate
            # that minted its quote without ever asking a pricing policy would
            # hash a provider figure while the deployment bills a different
            # one — the user consents to $0.11 and is charged $0.21, with
            # nothing red anywhere. It fails HERE, at the one projection every
            # family's mint reaches, instead of at each of the mint sites,
            # because a fence at the sites is a list somebody has to keep
            # complete. ``rate_estimate`` is what fills it.
            raise ConsentQuoteRefused(
                POLICY_ID_KEY,
                "is absent, so no pricing policy ever rated this quote and "
                "the billed figure is unknown",
            )
        return cls(
            cost=_quote_amount(estimate, "cost"),
            rows=_quote_count(estimate, "rows"),
            cost_source=_quote_name(estimate, "cost_source"),
            pricing_key=_quote_name(estimate, "pricing_key"),
            billed_cost=_quote_micros(estimate, BILLED_COST_KEY),
            policy_id=policy_id,
            engine=_quote_name(estimate, "engine"),
            # Absent means "no confirmation demanded" to every gate that
            # reads this key, so it projects to False, not to None.
            requires_confirmation=_quote_flag(estimate, "requires_confirmation"),
            remote_capability=_quote_name(estimate, "remote_capability"),
        )


# ---------------------------------------------------------------------------
# Rating: the ONE place a pricing policy is asked, and the ONE place a
# persisted rating is read back.
# ---------------------------------------------------------------------------


def quote_facts(estimate: Mapping[str, Any]) -> QuoteFacts:
    """The neutral provider-side facts a pricing policy rates over.

    Projected through the same by-name refusals the quote itself uses, so a
    malformed cost is named once — here — rather than reaching a deployment's
    tariff function as a string and coming back as a confident number.
    """
    cost = _quote_amount(estimate, "cost")
    return QuoteFacts(
        provider_cost=None if cost is None else usd_to_micros(cost),
        cost_source=_quote_name(estimate, "cost_source"),
        pricing_key=_quote_name(estimate, "pricing_key"),
        engine=_quote_name(estimate, "engine"),
        rows=_quote_count(estimate, "rows"),
    )


def persisted_rating(quote: ConsentQuote) -> PersistedRating:
    """Dispatch's read half: the rated projection of a persisted consent.

    Exactly two fields, and nothing else — every neutral fact beside them is
    recomputed live at dispatch. ``lane`` and ``provider_cost`` are absent by
    design rather than reconstructed: they were never hashed, so they were
    never bound by the user's approval, and inventing them here would be a
    fabricated fact riding inside a money verification.
    """
    return PersistedRating(billed_cost=quote.billed_cost, policy_id=quote.policy_id)


def apply_rating(
    estimate: Mapping[str, Any], rating: Rating | PersistedRating
) -> dict[str, Any]:
    """Fold a rating onto an estimate envelope, additively.

    Two keys, and they are ConsentQuote fields, so they ride into the hash,
    into the 402's ``estimate_details``, and onto the estimate endpoint's
    payload through the paths those facts already travel — no enumeration of
    response builders, and no second representation of one fact.
    """
    rated = dict(estimate)
    rated[BILLED_COST_KEY] = (
        None if isinstance(rating, Unpriceable) else rating.billed_cost
    )
    rated[POLICY_ID_KEY] = rating.policy_id
    return rated


def quoted_usd(estimate: Mapping[str, Any]) -> float | None:
    """The dollar figure a gate is asking the user to agree to, or ``None``
    for a policy's explicit "cannot say".

    Three states, closed at the rating boundary:

    * **a policy rated it** (``policy_id`` present, ``billed_cost`` an int) —
      the billed figure. Under base's identity policy that IS the provider
      cost, so the open edition's thresholds and copy are byte-identical;
      under a deployment's cost-plus tariff the gate compares, and the message
      quotes, what the user will actually be charged. That is the only figure
      a "confirm above $X" threshold can honestly be about.
    * **a policy DECLINED to rate it** (``policy_id`` present,
      ``billed_cost`` null — the ``Unpriceable`` verdict) — ``None``. Falling
      back to the provider cost here would be the seam's worst bug: a policy
      that said "I cannot price this" would have its run admitted as cheap on
      the strength of a number it explicitly refused to bill from. An honest
      cannot-say gates as unknown.
    * **no complete policy verdict** (``policy_id`` or ``billed_cost`` absent,
      or either malformed) — refuse. Every current 402 lane is rated before it
      reaches this selector, so absence is not a compatibility state; it means
      a producer crossed the money boundary with the old shape. Falling back
      to ``cost`` would restore the exact routed defect this seam closes: a
      provider figure shown as if it were what the deployment bills.

    ``policy_id`` plus the PRESENCE of ``billed_cost`` is what distinguishes
    an explicit ``Unpriceable`` verdict from "nobody asked". Without both,
    the two nulls would collapse and malformed/unrated work could be admitted
    on the provider's cheaper figure.
    """
    policy_id = _quote_name(estimate, POLICY_ID_KEY)
    if not policy_id:
        raise ConsentQuoteRefused(
            POLICY_ID_KEY,
            "is absent, so no pricing policy rated this quote",
        )
    if BILLED_COST_KEY not in estimate:
        raise ConsentQuoteRefused(
            BILLED_COST_KEY,
            "is absent, so the policy verdict is incomplete",
        )
    billed = _quote_micros(estimate, BILLED_COST_KEY)
    return None if billed is None else billed / MICROS_PER_USD


def rate_estimate(
    estimate: Mapping[str, Any], *, policy: PricingPolicy
) -> dict[str, Any]:
    """THE rating site: ask the deployment's policy, once, and fold its answer
    onto the estimate.

    Called where the estimate facts are FINAL and before anything is minted or
    shown, so the figure in the 402 message, the figure in the hash, and the
    figure the modal renders are one number that was computed once.
    """
    return apply_rating(estimate, policy.rate(quote_facts(estimate)))


# ---------------------------------------------------------------------------
# Scope: WHICH identity the approval is pinned to.
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = "frisket.confirmation-context.v1"


class RunnerScope(ContractModel):
    """The MapRunner lane's identity: the submitted spec minus its
    retry-flow fields, the exact rows, and the canonical binding to the
    source cells as they stand right now."""

    model_config = _HASHABLE_PAYLOAD

    kind: Literal["runner"] = "runner"
    runner_spec: dict[str, Any]
    row_ids: list[int]
    work_scope: dict[str, Any]


class ActionScope(ContractModel):
    """The identity of a family that holds the whole ``ActionSpec``: the
    spec's confirmed-excluded params hash. Every authored param is inside
    it, so the confirmed retry recomputes the identity the user submitted."""

    model_config = _HASHABLE_PAYLOAD

    kind: Literal["action"] = "action"
    action_hash: str


class ParamsScope(ContractModel):
    """The identity of a deterministic family that receives typed params
    rather than the outer envelope: every authored param, with the
    retry-flow fields removed so the confirmed retry recomputes the token it
    was shown. The action kind that used to ride inside this payload is now
    the envelope's required ``family_kind`` — the same string, hashed once."""

    model_config = _HASHABLE_PAYLOAD

    kind: Literal["params"] = "params"
    params: dict[str, Any]


ConfirmationScope = Annotated[
    RunnerScope | ActionScope | ParamsScope,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# The envelope: money gate or scope gate, never a nulled quote.
# ---------------------------------------------------------------------------


class MoneyConfirmationContext(ContractModel):
    """A gate that costs money: identity, live-resolved scope facts, quote.

    ``family_kind`` is REQUIRED and inside the hash. Two families whose
    scope identity and quote happened to coincide would otherwise mint the
    same token, and an approval shown for one could be echoed to satisfy
    the other's gate — across executor architectures, where the two never
    share a code path to notice.
    """

    model_config = _HASHABLE_PAYLOAD

    gate: Literal["money"] = "money"
    schema_version: Literal[_SCHEMA_VERSION] = _SCHEMA_VERSION
    family_kind: str = Field(min_length=1)
    scope: ConfirmationScope
    # Family-specific facts resolved live at gate time (a parent op's model,
    # a provider id, a resolved prompt hash). Bound, never dropped: binding
    # more can only make a consent stricter.
    bindings: dict[str, Any] = Field(default_factory=dict)
    quote: ConsentQuote


class ScopeConfirmationContext(ContractModel):
    """A gate with no price: a cap on how much WORK a deterministic family
    will do (``derive.join``'s projected fan-out, ``derive.collection_expand``'s
    previewed item count).

    It has no ``quote`` field at all. Giving it one and setting it to a
    null/zero ConsentQuote would put a cost sentinel in front of every
    reader of a gate that never had a cost — the union exists so that
    cannot happen.
    """

    model_config = _HASHABLE_PAYLOAD

    gate: Literal["scope"] = "scope"
    schema_version: Literal[_SCHEMA_VERSION] = _SCHEMA_VERSION
    family_kind: str = Field(min_length=1)
    scope: ConfirmationScope
    bindings: dict[str, Any] = Field(default_factory=dict)


ConfirmationContext = Annotated[
    MoneyConfirmationContext | ScopeConfirmationContext,
    Field(discriminator="gate"),
]

_CONTEXT_ADAPTER: TypeAdapter[MoneyConfirmationContext | ScopeConfirmationContext] = (
    TypeAdapter(ConfirmationContext)
)


def decode_confirmation_context(
    payload: object,
) -> MoneyConfirmationContext | ScopeConfirmationContext | None:
    """Read a confirmation envelope back off a durable row, or ``None``.

    Fails closed by returning ``None`` rather than raising: the caller is
    recovering a PAID effect, and a payload that is not a well-formed
    envelope must make it refuse and reconcile, never guess at what was
    approved. ``extra="forbid"`` all the way down means a payload carrying a
    field this version does not model is rejected too.
    """

    try:
        return _CONTEXT_ADAPTER.validate_python(payload)
    except ValidationError:
        return None


# ---------------------------------------------------------------------------
# The ONE mint.
# ---------------------------------------------------------------------------


def mint_confirmation_hash(
    context: MoneyConfirmationContext | ScopeConfirmationContext,
) -> str:
    """THE quote-hash mint. Every ``needs_confirmation`` envelope in this
    codebase offers a hash that came from here.

    Bare hex: the ONE spelling for every quote hash offered on the 402
    envelope (``promise_set_hash``), matching the routed promise-set content
    hash (``frisket.promise_set.v1``, corpus-pinned bare hex). The echo-gate
    closure test asserts both the spelling and — behaviorally, by recording
    this function's returns — that no family mints its own.
    """

    encoded = json.dumps(
        context.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def recipe_confirmation(
    *,
    family_kind: str,
    scope: RunnerScope,
    estimate: Mapping[str, Any],
) -> MoneyConfirmationContext:
    """The recipe lane's money gate: the estimate projects through
    ``ConsentQuote`` and rostered display keys are DROPPED.

    ``register_recipe`` is an open extension point, so this lane cannot
    enumerate its producers; the roster is what makes the classification
    closed anyway. An unclassified key refuses here (see
    ``ConsentQuote.from_estimate``) rather than silently deciding for its
    author whether it changes what the run costs.
    """

    return MoneyConfirmationContext(
        family_kind=family_kind,
        scope=scope,
        quote=ConsentQuote.from_estimate(estimate),
    )


def action_confirmation(
    *,
    family_kind: str,
    scope: ConfirmationScope,
    estimate: Mapping[str, Any],
) -> MoneyConfirmationContext:
    """An executor family's money gate: every key of ``estimate`` that is not
    a ``ConsentQuote`` field is BOUND as a scope fact.

    These families hash their whole estimate today and this keeps that
    property exactly, with the money-bearing half now reaching the same
    typed projection the recipe lane uses. Nothing is dropped, so a new
    fact — money-bearing or not — added to one of these estimates changes
    the hash and forces a fresh 402. That is why this lane needs no display
    roster: the roster protects keys that would otherwise be dropped, and
    here there are none.
    """

    quote_fields = set(ConsentQuote.model_fields)
    return MoneyConfirmationContext(
        family_kind=family_kind,
        scope=scope,
        bindings={
            key: value for key, value in estimate.items() if key not in quote_fields
        },
        quote=ConsentQuote.from_estimate(
            {key: value for key, value in estimate.items() if key in quote_fields}
        ),
    )


def scope_confirmation(
    *,
    family_kind: str,
    scope: ConfirmationScope,
    bindings: Mapping[str, Any],
) -> ScopeConfirmationContext:
    """A deterministic family's SCOPE gate: no cost, no quote, no sentinel.

    ``bindings`` are the freshly resolved facts the cap was judged against
    (projected row count, previewed item identities). Binding them is the
    whole point: a re-preview that returns the same COUNT over a different
    collection must not satisfy the old approval.
    """

    return ScopeConfirmationContext(
        family_kind=family_kind,
        scope=scope,
        bindings=dict(bindings),
    )
