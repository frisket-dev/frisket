"""Immutable request-scoped commercial-offering values.

Base defines the carriage and validates its generic terms; it does not ship a
commercial offering.  A downstream execution composition may inject one only
for a target that it also supplies.  Funding is deliberately absent from the
match key: who supplies a credential is not the same question as whether a
priced offering exists for the selected venue.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from frisket.execution.targets import (
    CAPABILITY_CENSUS,
    CAPABILITY_GEOCODE,
    CAPABILITY_OCR,
    CAPABILITY_TO_MARKDOWN,
    CAPABILITY_TRANSCRIBE,
    CAPABILITY_TRANSLATE,
    EXECUTION_CAPABILITIES,
    NOMINATIM_TARGET_ID,
    US_CENSUS_TARGET_ID,
    require_clean,
)

QUOTE_METER_KEY_BY_CAPABILITY: dict[str, str] = {
    CAPABILITY_TRANSCRIBE: "audio_seconds",
    CAPABILITY_OCR: "pages",
    CAPABILITY_TO_MARKDOWN: "pages",
    CAPABILITY_TRANSLATE: "characters",
    CAPABILITY_GEOCODE: "rows",
    CAPABILITY_CENSUS: "rows",
}

QuoteRoundingMode = Literal["exact", "half_even", "half_up"]
CeilingMode = Literal["none", "consented_quantity"]
RowSettlementMode = Literal["all_metered", "quoted_successful_rows"]

# These fields are copied from either an injected offering or base's neutral
# provider-direct terms into both the pinned basis and the run-start live fact.
# Admission compares the complete tuple; changing a meter while retaining the
# same SKU/rate is still a terms change and therefore requires new consent.
LIVE_COST_TERM_KEYS: tuple[str, ...] = (
    "terms_version",
    "quantity_unit",
    "quantity_rounding_mode",
    "quantity_rounding_decimal_places",
    "meter_key",
    "meter_units_per_quantity_unit",
    "ceiling_mode",
    "row_settlement_mode",
    "charge_authority",
)

# These providers publish free public APIs.  Their external egress remains a
# consent fact, but no downstream composition may turn the call into a priced
# commercial offering: nobody owns a per-request bill to pass through.
_NONCOMMERCIAL_EXECUTION_VENUES: frozenset[tuple[str, str]] = frozenset(
    {
        (NOMINATIM_TARGET_ID, CAPABILITY_GEOCODE),
        (US_CENSUS_TARGET_ID, CAPABILITY_CENSUS),
    }
)


def commercial_offering_allowed(
    *, target_id: str, capability: str, engine: str
) -> bool:
    """Whether an execution choice may lawfully carry a commercial offer."""

    del engine  # the free-public classification belongs to the venue/work pair
    return (target_id, capability) not in _NONCOMMERCIAL_EXECUTION_VENUES


def _decimal_string(name: str, value: str, *, positive: bool = False) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a decimal string") from exc
    if not parsed.is_finite() or parsed < 0 or (positive and parsed == 0):
        qualifier = "positive " if positive else "non-negative "
        raise ValueError(f"{name} must be a {qualifier}finite decimal string")


@dataclass(frozen=True)
class CommercialOfferingMatch:
    """The exact execution choice to which an offering applies."""

    target_id: str
    capability: str
    engine: str

    def __post_init__(self) -> None:
        require_clean("commercial offering target_id", self.target_id)
        require_clean("commercial offering capability", self.capability)
        require_clean("commercial offering engine", self.engine)
        if self.capability not in EXECUTION_CAPABILITIES:
            raise ValueError(
                f"unknown commercial offering capability {self.capability!r}"
            )
        if "*" in self.engine:
            raise ValueError("commercial offering matches require an exact engine")


@dataclass(frozen=True)
class CommercialQuoteRounding:
    """How a measured quote quantity is made durable.

    ``exact`` carries the source quantity unchanged and therefore has no
    decimal-place argument.  Rounded quantities name both precision and mode;
    the pair is pinned beside the rate so another deployment cannot silently
    change a consented quote's arithmetic.
    """

    mode: QuoteRoundingMode = "exact"
    decimal_places: int | None = None

    def __post_init__(self) -> None:
        if self.mode == "exact":
            if self.decimal_places is not None:
                raise ValueError("exact quote rounding has no decimal_places")
            return
        if self.mode not in ("half_even", "half_up"):
            raise ValueError(f"unknown quote rounding mode {self.mode!r}")
        if (
            isinstance(self.decimal_places, bool)
            or not isinstance(self.decimal_places, int)
            or self.decimal_places < 0
            or self.decimal_places > 18
        ):
            raise ValueError(
                "rounded quote quantities require decimal_places between 0 and 18"
            )


@dataclass(frozen=True)
class CommercialQuoteTerms:
    """The public quote identity and arithmetic for one offering."""

    pricing_key: str
    terms_version: str
    unit_rate: str
    quantity_unit: str
    rounding: CommercialQuoteRounding

    def __post_init__(self) -> None:
        require_clean("commercial pricing_key", self.pricing_key)
        require_clean("commercial terms_version", self.terms_version)
        require_clean("commercial quantity_unit", self.quantity_unit)
        _decimal_string("commercial unit_rate", self.unit_rate)
        if not isinstance(self.rounding, CommercialQuoteRounding):
            raise TypeError("commercial quote rounding must be CommercialQuoteRounding")


@dataclass(frozen=True)
class CommercialSettlementTerms:
    """Everything settlement needs, pinned rather than inferred from a SKU."""

    meter_key: str
    meter_units_per_quantity_unit: str
    ceiling_mode: CeilingMode
    row_settlement_mode: RowSettlementMode

    def __post_init__(self) -> None:
        require_clean("commercial meter_key", self.meter_key)
        _decimal_string(
            "commercial meter_units_per_quantity_unit",
            self.meter_units_per_quantity_unit,
            positive=True,
        )
        if self.ceiling_mode not in ("none", "consented_quantity"):
            raise ValueError(f"unknown commercial ceiling mode {self.ceiling_mode!r}")
        if self.row_settlement_mode not in (
            "all_metered",
            "quoted_successful_rows",
        ):
            raise ValueError(
                f"unknown commercial row settlement mode {self.row_settlement_mode!r}"
            )


@dataclass(frozen=True)
class CommercialPresentation:
    """Display-only venue and billing copy supplied by the offering owner."""

    venue_label: str
    billing_label: str

    def __post_init__(self) -> None:
        require_clean("commercial venue_label", self.venue_label)
        require_clean("commercial billing_label", self.billing_label)


@dataclass(frozen=True)
class CommercialOffering:
    """One exact target offering injected by a request composition."""

    match: CommercialOfferingMatch
    quote: CommercialQuoteTerms
    settlement: CommercialSettlementTerms
    charge_authority: str
    presentation: CommercialPresentation

    def __post_init__(self) -> None:
        if not isinstance(self.match, CommercialOfferingMatch):
            raise TypeError("commercial offering match must be CommercialOfferingMatch")
        if not isinstance(self.quote, CommercialQuoteTerms):
            raise TypeError("commercial offering quote must be CommercialQuoteTerms")
        if not isinstance(self.settlement, CommercialSettlementTerms):
            raise TypeError(
                "commercial offering settlement must be CommercialSettlementTerms"
            )
        if not commercial_offering_allowed(
            target_id=self.match.target_id,
            capability=self.match.capability,
            engine=self.match.engine,
        ):
            raise ValueError(
                f"free public APIs cannot carry a commercial offering: {self.match!r}"
            )
        expected_meter = QUOTE_METER_KEY_BY_CAPABILITY[self.match.capability]
        if self.settlement.meter_key != expected_meter:
            raise ValueError(
                "commercial offering settlement meter does not match the "
                f"capability's quoted measurement: expected {expected_meter!r}, "
                f"got {self.settlement.meter_key!r}"
            )
        if (
            self.settlement.row_settlement_mode == "quoted_successful_rows"
            and self.match.capability != CAPABILITY_TRANSCRIBE
        ):
            raise ValueError(
                "quoted_successful_rows settlement is supported only for "
                "transcribe offerings, whose quote pins per-row quantities"
            )
        require_clean("commercial charge_authority", self.charge_authority)
        if not isinstance(self.presentation, CommercialPresentation):
            raise TypeError(
                "commercial offering presentation must be CommercialPresentation"
            )
