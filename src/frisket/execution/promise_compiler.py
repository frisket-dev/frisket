"""The capability-generic promise compiler: resolved route facts + cost basis
-> compiled promise rows.

The original capability wrapper was already generic:
every row it emits is a fact about the ROUTE (who
operates the venue, where the media goes, what region is pinned, what it
costs), and a route is a route whatever capability chose it. The only
transcription-shaped things about it were its name and the fact that the cost
basis reaching it had been minted by a transcription-only quote function.
The rows now live only in this compiler; capability-specific wrapper
entrypoints and dormant mode tables have been deleted.

Rules:

- ``operator``: an ``eq`` row.
- ``credential_source``: NO ROW. As a run-start promise it
  was tautological where evaluable and unevaluable where meaningful; the
  credential question is now a PRE-EFFECT USE constraint at the adapter
  (``frisket.execution.credential_use``), consented as a credential CLASS via
  the route's funding/``cost_posture`` and checked before the call. The
  observed source stays post-effect evidence on the binding epoch.
- ``egress_class``: ``satisfies_order`` against the pinned ``egress.v1``
  order table, so a strictly-safer rebind satisfies by order implication.
- ``region``: an ``eq`` row ONLY when the region is pinned (not None) —
  honest absence, never ``eq null``.
- ``cost``: an ``le`` row (bound = unit_rate x estimated_quantity, decimal
  strings — no floats in hashed material) when priced; an ``unbounded`` row
  when unpriceable (presence is the claim: "no cost bound can be
  promised", preserving today's unknown-cost gate); NO row when the cost is
  genuinely zero (operator-borne local work).
- Audience: any cost row at all (priced or unbounded) and any egress beyond
  ``none`` are ``user_claim``. A local free run is
  ``OperatorBorneZeroCost`` and compiles NO cost row, so it compiles zero user
  claims, including for local OCR; everything else is ``system_promise``.
  The no-meter guarantee is carried by
  the ABSENCE of a meter, never by a bound that happens to multiply to zero.
Authored ``options`` are accepted for signature completeness (they shape the
route, and later releases may compile over them) but produce no rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Mapping, Union

from frisket.execution.commercial import (
    CeilingMode,
    CommercialQuoteRounding,
    QuoteRoundingMode,
    RowSettlementMode,
)
from frisket.execution.promises import (
    EGRESS_ORDER_REF,
    Promise,
    PromiseSet,
    is_table_ref,
)
from frisket.execution.targets import require_clean as _require_clean

if TYPE_CHECKING:  # annotation only — keeps this compiler resolver-free
    from frisket.execution.resolver import RouteRowFacts


def _parse_quantity(name: str, value: Any) -> Decimal:
    """Quantities are strings/ints by the hash contract — floats refused."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{name} must be an int or decimal string, not {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{name} is not a decimal string: {value!r}") from exc
        if not parsed.is_finite():
            raise ValueError(f"{name} must be finite: {value!r}")
        return parsed
    raise ValueError(f"{name} must be an int or decimal string, not {value!r}")


def _decimal_string(value: Decimal) -> str:
    """Deterministic plain-decimal rendering (no exponent, no trailing
    zeros) for hashed material."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True)
class PricedCostBasis:
    """An estimable cost: the promise bound is unit_rate x
    estimated_quantity under ``pricing_key``. ``hardware_class`` +
    ``throughput_ref`` pin hardware re-projection; both-``None`` is the
    explicit hardware-invariant-pricing sentinel.

    ``quantity_unit`` is free-form on purpose (``audio_minute``,
    ``gpu_second``, ``audio_second``, ``page``): the unit is the price book's
    declaration, and this type only has to carry it into hashed material
    unchanged.
    """

    pricing_key: str
    unit_rate: str  # decimal string (e.g. USD per unit) — never a float
    # ``None`` means the offering and all settlement terms are known but the
    # pre-effect quantity is not.  The compiler emits an unbounded user claim
    # while still pinning the offer by value for admission and settlement.
    estimated_quantity: Union[int, str, None]
    quantity_unit: str
    terms_version: str | None
    quantity_rounding_mode: QuoteRoundingMode
    quantity_rounding_decimal_places: int | None
    meter_key: str
    meter_units_per_quantity_unit: str
    ceiling_mode: CeilingMode
    row_settlement_mode: RowSettlementMode
    charge_authority: str
    hardware_class: str | None = None
    throughput_ref: str | None = None
    # Row-grained quoted settlement needs the exact allocation.  It is part of
    # the hashed cost promise, so settlement can charge completed rows without
    # re-deriving shares from an aggregate later.
    # ``None`` is retained for provider-list and pre-row-allocation bases.
    row_quote_quantities: tuple[tuple[int, str], ...] | None = None

    def __post_init__(self) -> None:
        _require_clean("pricing_key", self.pricing_key)
        _require_clean("quantity_unit", self.quantity_unit)
        if _parse_quantity("unit_rate", self.unit_rate) < 0:
            raise ValueError("unit_rate must be non-negative")
        if (
            self.estimated_quantity is not None
            and _parse_quantity("estimated_quantity", self.estimated_quantity) < 0
        ):
            raise ValueError("estimated_quantity must be non-negative")
        _require_clean("meter_key", self.meter_key)
        meter_scale = _parse_quantity(
            "meter_units_per_quantity_unit", self.meter_units_per_quantity_unit
        )
        if meter_scale <= 0:
            raise ValueError("meter_units_per_quantity_unit must be positive")
        _require_clean("charge_authority", self.charge_authority)
        if self.terms_version is not None:
            _require_clean("terms_version", self.terms_version)
        CommercialQuoteRounding(
            mode=self.quantity_rounding_mode,
            decimal_places=self.quantity_rounding_decimal_places,
        )
        if self.ceiling_mode not in ("none", "consented_quantity"):
            raise ValueError(f"unknown ceiling mode {self.ceiling_mode!r}")
        if self.row_settlement_mode not in (
            "all_metered",
            "quoted_successful_rows",
        ):
            raise ValueError(
                f"unknown row settlement mode {self.row_settlement_mode!r}"
            )
        row_allocation_required = (
            self.row_settlement_mode == "quoted_successful_rows"
            and self.estimated_quantity is not None
        )
        if row_allocation_required != (self.row_quote_quantities is not None):
            raise ValueError(
                "a bounded quoted_successful_rows basis requires row quote "
                "quantities, and other bases forbid them"
            )
        if not isinstance(self.unit_rate, str):
            raise ValueError("unit_rate must be a decimal string")
        if not isinstance(self.meter_units_per_quantity_unit, str):
            raise ValueError("meter_units_per_quantity_unit must be a decimal string")
        if (self.hardware_class is None) != (self.throughput_ref is None):
            raise ValueError(
                "hardware_class and throughput_ref pin together: both set "
                "(hardware-projected pricing) or both None (the "
                "hardware-invariant sentinel)"
            )
        if self.hardware_class is not None:
            _require_clean("hardware_class", self.hardware_class)
        if self.throughput_ref is not None and not is_table_ref(self.throughput_ref):
            raise ValueError(
                f"throughput_ref must look like 'name.vN': {self.throughput_ref!r}"
            )
        if self.row_quote_quantities is not None:
            seen: set[int] = set()
            total = Decimal(0)
            previous = -1
            for row_id, raw_quantity in self.row_quote_quantities:
                if (
                    isinstance(row_id, bool)
                    or not isinstance(row_id, int)
                    or row_id <= 0
                ):
                    raise ValueError("row quote ids must be positive integers")
                if row_id in seen or row_id <= previous:
                    raise ValueError(
                        "row quote quantities must have unique ascending row ids"
                    )
                if not isinstance(raw_quantity, str):
                    raise ValueError("row quote quantities must be decimal strings")
                quantity = _parse_quantity("row quote quantity", raw_quantity)
                if quantity < 0:
                    raise ValueError("row quote quantities must be non-negative")
                seen.add(row_id)
                previous = row_id
                total += quantity
            if total != _parse_quantity("estimated_quantity", self.estimated_quantity):
                raise ValueError(
                    "estimated_quantity must equal the exact sum of row quote "
                    "quantities"
                )

    @property
    def bound(self) -> Decimal | None:
        if self.estimated_quantity is None:
            return None
        return _parse_quantity("unit_rate", self.unit_rate) * _parse_quantity(
            "estimated_quantity", self.estimated_quantity
        )


@dataclass(frozen=True)
class UnpriceableCost:
    """No cost bound can be promised (today's unknown-cost estimate). The
    compiled ``unbounded`` row is the explicit user claim; the gate renders
    it as "cost cannot be estimated"."""


@dataclass(frozen=True)
class OperatorBorneZeroCost:
    """Genuinely zero user cost (operator-borne local work): compiles NO
    cost row — absence of a claim, not a claim of zero."""


CostBasis = Union[PricedCostBasis, UnpriceableCost, OperatorBorneZeroCost]


def compile_route_promises(
    route: "RouteRowFacts",
    cost: CostBasis,
    *,
    options: Mapping[str, Any] | None = None,
    work_scope: Mapping[str, Any] | None = None,
) -> PromiseSet:
    """Compile the promise set for one resolved route, for ANY capability.

    ``route`` is the ONE route-facts type
    (``frisket.execution.resolver.RouteRowFacts``): this reads only its
    config-phase fields (operator, egress_class, region — cost basis arrives
    separately, and ``credential_source`` left the compiled set), and the
    former 5-field ``ResolvedRouteFacts`` subset was removed
    rather than being re-projected at every call site. Its field validation
    moved onto ``RouteRowFacts.__post_init__``, so an unusable route can no
    longer compile promises.
    """
    if options is not None and not isinstance(options, Mapping):
        raise ValueError("options must be a mapping or None")

    scope_requires_consent = work_scope is not None and (
        route.egress_class != "none" or not isinstance(cost, OperatorBorneZeroCost)
    )
    rows: list[Promise] = [
        Promise.make(
            "operator",
            "eq",
            route.operator,
            audience="system_promise",
        ),
        Promise.make(
            "egress_class",
            "satisfies_order",
            route.egress_class,
            # Scope is the basis of the user's egress/cost claim: the same
            # venue and quote do not authorize a different row or a later
            # revision of the row's source value. Keeping it on the existing
            # categorical claim makes exact-match and historical coverage
            # move together without pretending scope is action identity.
            basis=(
                {"work_scope": dict(work_scope)} if scope_requires_consent else None
            ),
            order_ref=EGRESS_ORDER_REF,
            audience=(
                "user_claim"
                if route.egress_class != "none" or scope_requires_consent
                else "system_promise"
            ),
        ),
    ]
    if route.region is not None:  # honest absence: never eq null
        rows.append(
            Promise.make(
                "region",
                "eq",
                route.region,
                audience="system_promise",
            )
        )
    if isinstance(cost, PricedCostBasis):
        bound = cost.bound
        rows.append(
            Promise.make(
                "cost",
                "unbounded" if bound is None else "le",
                None if bound is None else _decimal_string(bound),
                basis={
                    "pricing_key": cost.pricing_key,
                    "unit_rate": cost.unit_rate,
                    "estimated_quantity": cost.estimated_quantity,
                    "quantity_unit": cost.quantity_unit,
                    "terms_version": cost.terms_version,
                    "quantity_rounding_mode": cost.quantity_rounding_mode,
                    "quantity_rounding_decimal_places": (
                        cost.quantity_rounding_decimal_places
                    ),
                    "meter_key": cost.meter_key,
                    "meter_units_per_quantity_unit": (
                        cost.meter_units_per_quantity_unit
                    ),
                    "ceiling_mode": cost.ceiling_mode,
                    "row_settlement_mode": cost.row_settlement_mode,
                    "charge_authority": cost.charge_authority,
                    "hardware_class": cost.hardware_class,
                    "throughput_ref": cost.throughput_ref,
                    **(
                        {
                            "row_quote_quantities": [
                                {"row_id": row_id, "quantity": quantity}
                                for row_id, quantity in cost.row_quote_quantities
                            ]
                        }
                        if cost.row_quote_quantities is not None
                        else {}
                    ),
                },
                # A PRICED basis means a meter exists and somebody will be
                # billed by it, so the row is always the user's to consent to.
                # It used to read ``"user_claim" if bound > 0``, which decided
                # a CONSENT-VISIBILITY question from arithmetic the input
                # controls: a truncated container header or a zero-page PDF
                # quotes zero, the cost row silently left the user-visible
                # claim set, the confirm gate showed no cost line at all, and
                # ``cost le 0`` scored SATISFIED as a system detail while
                # settlement metered the real work. A ceiling of zero is the
                # STRONGEST claim in the set, not an absent one.
                audience="user_claim",
            )
        )
    elif isinstance(cost, UnpriceableCost):
        rows.append(
            Promise.make(
                "cost",
                "unbounded",
                None,
                audience="user_claim",
            )
        )
    elif isinstance(cost, OperatorBorneZeroCost):
        pass  # genuinely zero: no cost row at all
    else:
        raise ValueError(f"unknown cost basis: {cost!r}")

    return PromiseSet.make(rows)
