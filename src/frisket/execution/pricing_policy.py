"""The generic pricing-policy port for confirmation-time rating.

**The invariant, one sentence:** *base never computes a markup.* This
edition rates at what the provider costs (:class:`IdentityPricingPolicy`);
a deployment that wants cost-plus billing supplies its own tariff function
from outside this tree (its own docstring should say "no open module may
compute a markup"). This module is the seam between those two
sentences, and it exists so that the figure a journalist consents to and the
figure they are billed can be the SAME number under either policy — quoted
once, hashed into the consent, and displayed.

**What crosses the seam is a VALUE, not a policy object.** A
:class:`RatedQuote` (or an :class:`Unpriceable`) is minted once, where the
estimate facts are finalized, and everything downstream — the 402 message,
the estimate payload, the consent hash, the persisted consent row, the
dispatch-time reconstruction — reads that one rating. Dispatch is
deliberately never handed a policy: it re-derives the NEUTRAL provider facts
live (that is its job — catching scope and price drift) and reads the RATED
projection back off the consent it is verifying. A deployment's tariff
function therefore runs exactly once per confirmation, at quote time, where
the user can see its answer.

**Two return types, no sentinel.** :class:`Unpriceable` is a distinct type
rather than a ``None`` or a zero, for the reason ``UnpriceableCost`` is one
in ``execution/promise_compiler.py``: a run whose cost cannot be determined
is not a free run, and the whole product rule for this seam is "an honest
cannot-say, never a fabricated total". A policy that cannot rate something
says so and names why; the gate then demands the explicit confirm that an
unknown cost has always demanded.

**Deploy ordering (a constraint, not a preference).** The billed figure is
served by the API and rendered by the web bundle, and a bundle that predates
this seam renders ``cost`` — the PROVIDER's number — while the server hashes
the BILLED one. A deployment whose pinned frontend bundle is older
than its wheel would therefore show a journalist $0.11, take their
confirmation, and dispatch a run consented at $0.21. So the wheel and the
bundle advance ATOMICALLY, in one release, before any cost-plus policy is
installed. A matching note belongs beside any external tariff implementation
with the injection wave that first installs a policy there; until then this
is the only place the constraint is written down, which is why it is written
down here.

**Micros, not floats.** ``billed_cost`` and ``provider_cost`` are integer
micro-dollars. The estimate envelope's legacy ``cost`` field stays a float
USD and stays the PROVIDER's number forever (fenced by
``tests/execution/test_billed_cost_consent_seam.py``'s
``test_a_markup_never_lands_on_the_legacy_cost_field``) — the rated figures are
additive, so a consumer reading the old field cannot be handed a marked-up
number it would mark up again.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal, Protocol, Union, runtime_checkable

MICROS_PER_USD = 1_000_000


def usd_to_micros(value: float | int | str | Decimal) -> int:
    """USD to integer micro-dollars, through ``Decimal``, never binary float
    arithmetic.

    ``Decimal(str(0.0075))`` is exactly ``0.0075``; ``0.0075 * 1_000_000`` in
    binary floating point is not exactly ``7500``. Money that is going to be
    compared for equality inside a consent hash cannot be rounded by whichever
    way the last bit fell.

    ``ROUND_HALF_UP`` at the micro is the identity mapping's one rounding
    authority. A quote below half a micro-dollar ($0.0000005) rounds to zero
    billed micros; that is the honest limit of the unit, and the precise
    provider figure remains on the estimate's own ``cost`` field.
    """
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:  # pragma: no cover - callers pre-validate
        raise ValueError(f"not a decimal USD value: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"non-finite USD value: {value!r}")
    return int((parsed * MICROS_PER_USD).quantize(Decimal(1), rounding=ROUND_HALF_UP))


#: Which price list a rated quote came off. Descriptive, and each value names
#: a different ANSWER to "who is being paid, and by whose price":
#:
#: * ``published_sku`` — a price the deployment publishes and can absorb
#:   variance against. Base ships no published SKU membership or commercial
#:   price list; a downstream policy that owns offerings may classify it.
#: * ``cost_plus`` — a tariff function over the provider's cost. **Base never
#:   returns this**; it is the hosted lane, and
#:   ``test_identity_policy_never_returns_the_cost_plus_lane`` is the fence.
#: * ``byok`` — the operator's own credential pays the provider directly and
#:   the deployment charges nothing on top. The open edition's normal answer
#:   for a billable run.
#: * ``free`` — nobody is billed at all (local compute the operator already
#:   owns). Distinct from a $0.00 cost-plus rating, which would still be a
#:   charge that happened to round to nothing.
PricingLane = Literal["published_sku", "cost_plus", "byok", "free"]


@dataclass(frozen=True)
class QuoteFacts:
    """The neutral, provider-side facts a policy rates over.

    Bundled rather than passed as five arguments because they only ever occur
    together, and because the bundle is what a deployment's tariff function
    signature binds to: adding a fact here is a visible break at every
    implementation rather than a silently-ignored keyword.

    ``provider_cost`` is ``None`` when the run's cost is genuinely unknowable
    before it runs (``cost_source == "unknown"``) — never a zero. A policy
    that cannot rate an unknown says :class:`Unpriceable`.
    """

    provider_cost: int | None
    cost_source: str | None
    pricing_key: str | None
    engine: str | None
    rows: int | None


@dataclass(frozen=True)
class RatedQuote:
    """What the user will be billed, and by whose price list.

    ``billed_cost`` is the figure a 402 quotes and a receipt must agree with.
    ``provider_cost`` rides along by value so the rating is self-contained:
    every reader of a rated quote can see both halves of a cost-plus markup
    without re-deriving the input the policy was handed.
    """

    billed_cost: int
    provider_cost: int | None
    lane: PricingLane
    policy_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.billed_cost, int) or isinstance(self.billed_cost, bool):
            raise TypeError("billed_cost must be integer micro-dollars")
        if self.billed_cost < 0:
            raise ValueError("billed_cost must not be negative")
        if not self.policy_id:
            raise ValueError("a rated quote must name the policy that rated it")


@dataclass(frozen=True)
class Unpriceable:
    """The policy cannot put a number on this run, and says which fact stopped
    it.

    A distinct type, never ``None`` and never a zero: the gate treats an
    unpriceable quote exactly as it has always treated an unknown cost —
    explicit confirmation, no fabricated figure — and a settlement that
    cannot prove what was owed refuses rather than charging a confident
    partial amount.
    """

    reason: str
    policy_id: str

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("an unpriceable quote must name why")
        if not self.policy_id:
            raise ValueError("an unpriceable quote must name the policy")


Rating = Union[RatedQuote, Unpriceable]


@dataclass(frozen=True)
class PersistedRating:
    """The rated projection read back off a persisted consent at dispatch —
    and the whole of what dispatch is allowed to take from that record.

    A separate type from :class:`RatedQuote` because it is a genuinely smaller
    fact. ``lane`` and ``provider_cost`` are not recoverable from a consent
    row: they were never hashed, so they were never bound, so reconstructing
    them would mean inventing them. Naming the pair keeps that honest and
    keeps the read narrow — a future reader reaching for a third field off the
    persisted quote has to widen a type to do it, which is a visible change
    rather than a quiet one.
    """

    billed_cost: int | None
    policy_id: str


@runtime_checkable
class PricingPolicy(Protocol):
    """Rate one quote's provider facts into the figure the user is billed.

    One method, deliberately. The port's whole job is the arithmetic a
    deployment owns and base must not contain; everything else about the quote
    — which rows, which engine, which scope — is already decided by the time a
    policy is asked, and handing a policy more would invite it to change
    something other than the price.

    Implementations must be PURE and total: ``rate`` is called inside the
    confirmation mint, whose only modeled outcomes are a quote and a named
    refusal. A policy that cannot price something returns
    :class:`Unpriceable`; it does not raise, and it does not read the clock or
    the database (a quote must be reproducible from the facts it was minted
    over, which is what lets the consent hash mean anything).
    """

    @property
    def policy_id(self) -> str:
        """Stable identity of this policy AND its rate table.

        It rides inside the consent hash, so changing the tariff changes this
        string and every consent minted under the old one stops matching —
        which is the point: a user consented to a price, not to a policy name.
        """
        ...

    def rate(self, facts: QuoteFacts) -> Rating:
        """The one rating call. See :class:`RatedQuote` / :class:`Unpriceable`."""
        ...


@dataclass(frozen=True)
class IdentityPricingPolicy:
    """Base's only policy: billed IS the provider's cost.

    The open edition bills nobody. A priced run here is the operator's own
    provider invoice, shown so "what will this cost me" has an answer, so the
    honest rating is the identity function — and the lane says which price
    list the number came off, so a reader can tell an operator's BYOK provider
    bill from a price this deployment publishes.

    It never returns ``cost_plus``. Adding a markup here would put the tariff
    in the open tree, which is the one thing this seam exists to prevent.
    """

    policy_id: str = "frisket.pricing.identity.v1"

    def rate(self, facts: QuoteFacts) -> Rating:
        if facts.provider_cost is None:
            # ``cost_source == "unknown"``: a billable engine whose pre-run
            # cost is genuinely unknowable. The identity of "no number" is
            # still no number.
            return Unpriceable(
                reason="provider cost is unknown before the run",
                policy_id=self.policy_id,
            )
        return RatedQuote(
            billed_cost=facts.provider_cost,
            provider_cost=facts.provider_cost,
            lane=self._lane(facts),
            policy_id=self.policy_id,
        )

    def _lane(self, facts: QuoteFacts) -> PricingLane:
        if facts.provider_cost == 0:
            # Zero is not a rounded-down charge here: the estimate
            # constructors that emit it (``free_local_estimate``,
            # ``invalid_engine_estimate``) mean "the operator already owns
            # this compute", which is the absence of a meter.
            return "free"
        # Everything else billable is somebody else's invoice against the
        # operator's own credential.
        return "byok"


#: THE policy base ships. One instance: it is frozen and stateless, so a
#: deployment that overrides pricing replaces the object rather than mutating
#: this one.
IDENTITY_PRICING_POLICY = IdentityPricingPolicy()


# ---------------------------------------------------------------------------
# Installing a deployment's policy: process-scoped, once.
# ---------------------------------------------------------------------------
#
# **Why process-scoped rather than an injected dependency.** Every other
# edition port in this codebase rides an object: ``WorkerPorts`` reaches the
# worker handlers, ``ExecutorDeps`` reaches the executor families, the
# execution-target provider is a parameter on the admission call. Rating
# cannot use any of them, because the fourteen sites that mint a money
# confirmation share NO object:
#
#   * the recipe lane rates inside ``validate_spec``, reached from
#     ``MapRunner`` — nine production constructions, five of which are built
#     inside base with no deployment-supplied parameter;
#   * the five executor-family mints (``entities``, ``action_lifecycle``,
#     ``sheet_refresh``, ``research``) are reached
#     through builder callbacks that rebuild ``ExecutorDeps`` router-only, so
#     a deps field would be silently dropped on the way — which is exactly the
#     optional-wiring shape this seam exists to avoid;
#   * the estimate endpoint holds a ``Workspace`` and no deps at all.
#
# A price list is also genuinely a property of the DEPLOYMENT, not of a
# request, a project, or a worker: one process bills one way. So it is
# installed once at the composition root and read by name, and the
# install-once refusal below is what keeps "one process, one tariff" true
# instead of merely intended.

_INSTALLED_POLICY: PricingPolicy | None = None
_INSTALL_LOCK = threading.Lock()


class PricingPolicyConflict(RuntimeError):
    """Two components tried to install different pricing policies.

    Fails closed and names both, because the alternative is a process whose
    billed figures depend on import order: the quote a user is shown and the
    quote a later gate mints would come off different price lists, and the
    consent hash would silently stop matching. Re-installing a policy with the
    SAME ``policy_id`` is fine — a host that builds several apps in one
    process, or an app and a worker that each construct their own instance of
    one tariff, is not a conflict.
    """

    def __init__(self, installed: str, attempted: str) -> None:
        super().__init__(
            f"pricing policy {installed!r} is already installed in this "
            f"process and {attempted!r} tried to replace it; a deployment "
            "installs exactly one policy at its composition root, because a "
            "process that rates two ways cannot say what a user consented to"
        )
        self.installed = installed
        self.attempted = attempted


def install_pricing_policy(policy: PricingPolicy) -> None:
    """Install THE pricing policy for this process. Called once, at a
    deployment's composition root.

    ``create_app(pricing_policy=...)`` and
    ``register_production_handlers(pricing_policy=...)`` are the two seats
    that call this; a downstream composition's ``app.py`` and ``worker.py``
    are the intended callers, and until one of them passes a policy this
    process rates at the identity policy and bills exactly provider cost.

    Refuses a policy that does not satisfy the port, by name: a mistyped
    injection must fail at startup rather than at the first 402.
    """
    if not isinstance(policy, PricingPolicy):
        raise TypeError(
            "pricing_policy must implement PricingPolicy (a `rate(QuoteFacts)"
            " -> RatedQuote | Unpriceable` method and a `policy_id`); got "
            f"{type(policy).__name__}"
        )
    policy_id = getattr(policy, "policy_id", "")
    if not isinstance(policy_id, str) or not policy_id:
        raise ValueError(
            "pricing_policy.policy_id must be a non-empty string: it rides "
            "inside every consent hash, so an unnamed policy makes approvals "
            "impossible to tell apart"
        )
    global _INSTALLED_POLICY
    with _INSTALL_LOCK:
        # Compared by ``policy_id``, not by object identity or equality.
        # ``policy_id`` IS the identity of a price list — it is what rides
        # inside the consent hash, so two objects claiming the same id claim
        # to rate identically, and a deployment whose app and worker each
        # construct their own instance is not a conflict. (It must not be: an
        # app and a worker that installed "different" policies with the same
        # tariff would refuse to start for no reason, and the worker installing
        # a genuinely different one is exactly what this catches.)
        installed_id = (
            None
            if _INSTALLED_POLICY is None
            else getattr(_INSTALLED_POLICY, "policy_id", "?")
        )
        if installed_id is not None and installed_id != policy_id:
            raise PricingPolicyConflict(installed_id, policy_id)
        _INSTALLED_POLICY = policy


def default_pricing_policy() -> PricingPolicy:
    """THE policy this process rates at: the installed one, or identity.

    The seat, and the shape, of ``resolve_for_action.default_provider``: base
    names the port, ships the open-edition implementation, and resolves it at
    ONE function so a deployment has a single place to answer differently.
    Base's answer is the identity policy, which is not a hole — it is the open
    edition's correct published behavior, and it is still a RATING, so every
    quote names the policy that produced it.
    """
    return IDENTITY_PRICING_POLICY if _INSTALLED_POLICY is None else _INSTALLED_POLICY


def _reset_pricing_policy_for_tests() -> None:
    """Test-only reset (the ``_reset_lease_state_for_tests`` convention).

    Never call it in a live process: a run quoted under one policy and
    dispatched under another refuses its own consent."""

    global _INSTALLED_POLICY
    with _INSTALL_LOCK:
        _INSTALLED_POLICY = None
