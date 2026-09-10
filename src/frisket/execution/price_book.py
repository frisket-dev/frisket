"""Generic provider-cost quoting and pinned-term settlement.

Base owns estimates for costs paid directly to third-party providers.  It
ships no platform-venue SKU, rate card, or retail rate.  A request-scoped
``CommercialOffering`` may supply those values from a downstream composition;
the quote pins every settlement term and settlement never looks the SKU up.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP
from typing import Any, Literal, Union

from frisket.ai.external_pricing import (
    DATALAB_CONVERT_PAGE,
    DATALAB_OCR_PAGE,
    DEEPL_TRANSLATE_CHAR,
    GEOCODE_OPENCAGE_ROW,
    GOOGLE_TRANSLATE_CHAR,
    external_unit_price_string,
)
from frisket.execution.commercial import CommercialOffering
from frisket.execution.promise_compiler import (
    CostBasis,
    OperatorBorneZeroCost,
    PricedCostBasis,
    UnpriceableCost,
)
from frisket.execution.targets import (
    CAPABILITY_CENSUS,
    CAPABILITY_GEOCODE,
    CAPABILITY_OCR,
    CAPABILITY_TO_MARKDOWN,
    CAPABILITY_TRANSCRIBE,
    CAPABILITY_TRANSLATE,
    DATALAB_TARGET_ID,
    DEEPL_TARGET_ID,
    GOOGLE_TRANSLATE_TARGET_ID,
    OPENCAGE_TARGET_ID,
    REMOTE_API_TARGET_ID_PREFIX,
)

# ---------------------------------------------------------------------------
# funding: who pays (§3.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperatorBorne:
    """The operator runs the venue or pays its provider directly.

    A priced basis in this posture is the operator's own provider estimate,
    retained so "what did it cost" remains answerable.
    """


@dataclass(frozen=True)
class PlatformMetered:
    """The platform account supplies the execution credential.

    This marker controls credential and cost posture only.  It never creates
    a rate, SKU, target, or commercial offer; those exist only when the same
    request composition injects an exact :class:`CommercialOffering`.
    """


@dataclass(frozen=True)
class ByokZero:
    """The user's own provider key; the provider bills them directly."""


Funding = Union[OperatorBorne, PlatformMetered, ByokZero]

# ``cost_posture`` is the route row's projection of the same fact — one
# authority, two spellings, mapped HERE so the prefix-dispatch that used to
# answer "who pays" from a target-id string has no second home.
_FUNDING_TO_COST_POSTURE: dict[type, str] = {
    OperatorBorne: "operator_borne",
    PlatformMetered: "platform_metered",
    ByokZero: "org_key",
}


def cost_posture_for(funding: Funding) -> str:
    """The route row's ``cost_posture`` for a funding class."""
    try:
        return _FUNDING_TO_COST_POSTURE[type(funding)]
    except KeyError:  # pragma: no cover - union is closed
        raise ValueError(f"unknown funding class {funding!r}") from None


def funding_for_cost_posture(cost_posture: str) -> Funding:
    """The inverse, for the dispatch-time fence reading a PERSISTED route.

    Refuses a posture outside the vocabulary rather than picking a funding
    class for it. The map above is the ONLY writer of this column, so an
    unrecognized value is a corrupt row, and the readers downstream are the
    consent fence (``credential_use.consented_credential_classes``) and the
    cost fact — both of which would be WIDENED by guessing operator-borne.
    Symmetric with :func:`cost_posture_for`, which refuses the unknown
    funding class."""
    for funding_type, posture in _FUNDING_TO_COST_POSTURE.items():
        if posture == cost_posture:
            return funding_type()
    raise ValueError(f"unknown cost posture {cost_posture!r}")


# ---------------------------------------------------------------------------
# the SKUs
# ---------------------------------------------------------------------------

# Local execution — "runs on your laptop, free" — is the ABSENCE of a SKU, not
# a zero-rated one: :func:`sku_for` returns ``None`` and the compiler emits no
# cost row at all (absence of a claim, not a claim of zero). A `SKU_LOCAL`
# constant would be a name with no reader.


def byok_remote_sku(engine_id: str) -> str:
    """The BYOK/remote-api SKU for one engine. ENGINE-keyed, which is why
    ``ExecutionTargetProvider.connection`` — asked about a TARGET — never
    could have minted it."""
    return f"{engine_id}.audio_second"


#: Datalab's hosted OCR, billed per page. It is the first SKU in this
#: book whose quantity is not a second of audio; the earlier blocker was
#: "``sku_for`` is target-keyed and transcription-only, with no page unit",
#: and both halves had to go: the quantity unit is a per-QUOTE declaration on
#: the basis (``PricedCostBasis.quantity_unit`` was always free-form; nothing
#: below it assumed seconds), and the selection now takes the capability.
#: The key IS ``external_pricing``'s catalog key, so the rate the book quotes
#: and the rate the catalog displays are one row, not two.
SKU_DATALAB_OCR_PAGE = DATALAB_OCR_PAGE


def byok_remote_ocr_sku(engine_id: str) -> str:
    """The BYOK/remote-api SKU for one VLM OCR engine. Named, and then
    deliberately unpriced: a VLM bills per TOKEN over an image whose token
    count is unknown until the page is rendered and sent, so there is no
    per-page rate to quote. The SKU exists so the promise says *which* meter
    it could not bound; :func:`quote_ocr` returns ``UnpriceableCost`` and the
    run gates on an ``unbounded`` cost claim — today's behavior for remote OCR
    (``unknown_cost_estimate``), now expressed as a claim the user consents
    to."""
    return f"{engine_id}.ocr_page"


#: Third-party provider list prices. Every one of them is somebody else's
#: number under the operator's own key, so they follow the Datalab OCR page
#: rate exactly: the SKU key IS the ``external_pricing`` catalog key (the rate
#: the book quotes and the rate the catalog displays are one row, not two), and
#: the rate is read live at the fence rather than pinned on the card, because
#: the whole point of the fence is to notice when somebody else's price moves.
#:
SKU_DEEPL_TRANSLATE_CHAR = DEEPL_TRANSLATE_CHAR
SKU_GOOGLE_TRANSLATE_CHAR = GOOGLE_TRANSLATE_CHAR
SKU_DATALAB_CONVERT_PAGE = DATALAB_CONVERT_PAGE
SKU_GEOCODE_OPENCAGE_ROW = GEOCODE_OPENCAGE_ROW

#: venue -> (SKU, quantity unit) for the single-engine third-party venues. A table
#: rather than a branch per capability inside :func:`sku_for`, so a venue that
#: gains a SKU cannot gain it in one function and not the other.
_VENUE_SKU: dict[tuple[str, str], str] = {
    (CAPABILITY_TRANSLATE, DEEPL_TARGET_ID): SKU_DEEPL_TRANSLATE_CHAR,
    (CAPABILITY_TRANSLATE, GOOGLE_TRANSLATE_TARGET_ID): SKU_GOOGLE_TRANSLATE_CHAR,
    (CAPABILITY_TO_MARKDOWN, DATALAB_TARGET_ID): SKU_DATALAB_CONVERT_PAGE,
    (CAPABILITY_GEOCODE, OPENCAGE_TARGET_ID): SKU_GEOCODE_OPENCAGE_ROW,
}

#: The capabilities whose venues are ALL in :data:`_VENUE_SKU` or genuinely
#: free-local. Consulted by :func:`sku_for` so an unrecognized venue under one
#: of them returns ``None`` (free) only because the roster says the work runs
#: on the operator's own box — never because a table lookup missed.
_PHASE_4_CAPABILITIES = frozenset(
    {
        CAPABILITY_TRANSLATE,
        CAPABILITY_TO_MARKDOWN,
        CAPABILITY_GEOCODE,
        CAPABILITY_CENSUS,
    }
)

# ---------------------------------------------------------------------------
# the sources (this module is their ONLY reader)
# ---------------------------------------------------------------------------


def _validate_rate(value: Decimal, *, label: str) -> Decimal:
    if not value.is_finite() or value < 0:
        raise ValueError(f"{label} must be a non-negative finite decimal USD value")
    return value


def datalab_ocr_page_rate() -> Decimal | None:
    """Datalab's live per-page list price, or ``None`` when this build does
    not know it.

    A PROVIDER list price, like the Modal GPU-second rate beside it: read live
    at the fence rather than pinned on the card, because it is somebody else's
    number and the whole point of the fence is to notice when it moves.
    ``ai/external_pricing`` owns the row and its ``FRISKET_DATALAB_OCR_USD_PER_PAGE``
    operator override; this is the ONE reader that turns it into a quoted rate
    (the catalog's hint renders the same row as DISPLAY, never as a quote).
    """
    raw = external_unit_price_string(DATALAB_OCR_PAGE)
    if raw is None:
        return None
    try:
        return _validate_rate(Decimal(raw), label=DATALAB_OCR_PAGE)
    except InvalidOperation:  # pragma: no cover - the catalog holds decimals
        return None


def catalog_list_price(sku: str) -> Decimal | None:
    """The live catalog list price for one provider-direct SKU, or ``None``
    when this build does not know it.

    The generalization of :func:`datalab_ocr_page_rate` over these SKUs,
    which are all the same shape: a catalog row whose key IS the SKU, with an
    operator env override the catalog owns. ONE reader, so the rule the module
    docstring states ("one mint reads one source and pins it") survives six
    more rates — six bespoke ``*_rate()`` functions would be six mints again.

    ``None`` means "don't know" and every caller reports exactly that (an
    unpriceable basis, an absent cost fact). Never a substitute rate.
    """
    raw = external_unit_price_string(sku)
    if raw is None:
        return None
    try:
        return _validate_rate(Decimal(raw), label=sku)
    except InvalidOperation:  # pragma: no cover - the catalog holds decimals
        return None


@dataclass(frozen=True)
class AudioSecondRate:
    rate: Decimal | None
    cost_source: Literal["free_local", "pricing_data", "unknown"]
    pricing_key: str | None


def byok_audio_second_rate(engine_id: str) -> AudioSecondRate:
    """A per-second rate and its provenance from the same lookup."""
    from frisket.ai.llm.pricing import audio_price
    from frisket.local_model_ids import parse_local_model_id

    per_second = (audio_price(engine_id) or {}).get("per_second")
    try:
        parse_local_model_id(engine_id)
    except ValueError:
        pricing_key = byok_remote_sku(engine_id)
        cost_source: Literal["free_local", "pricing_data", "unknown"] = (
            "pricing_data" if per_second is not None else "unknown"
        )
    else:
        pricing_key = None
        cost_source = "free_local"
    if per_second is None:
        return AudioSecondRate(None, cost_source, pricing_key)
    try:
        return AudioSecondRate(_to_decimal(per_second), cost_source, pricing_key)
    except (InvalidOperation, ValueError):
        return AudioSecondRate(None, "unknown", pricing_key)


# ---------------------------------------------------------------------------
# decimal rendering (hashed material carries no floats)
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError(f"non-finite quantity: {value!r}")
    return parsed


def decimal_str(value: Any) -> str:
    """Plain decimal string (no exponent, no trailing zeros)."""
    text = format(_to_decimal(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


# ---------------------------------------------------------------------------
# venue classification
# ---------------------------------------------------------------------------


def sku_for(*, capability: str, target_id: str, engine: str) -> str | None:
    """The third-party provider-cost SKU for one selected target, if any.

    This function has no platform-retail branch and does not accept funding.
    Commercial offerings are exact composition values, never inferred from a
    credential/funding marker.  ``None`` is the honest absence of a provider
    cost meter (local compute, Nominatim, and US Census).
    """
    if capability in _PHASE_4_CAPABILITIES:
        # The engine is not part of the key for these: every venue
        # serves exactly one engine for its capability (Datalab's 'datalab'
        # row, DeepL's 'deepl' row, ...), so the venue alone decides the meter.
        # A venue outside the table is a LOCAL one — the local MT builds, the
        # local converters, the gateway's converters — where the operator
        # already owns the compute and no per-request meter exists.
        return _VENUE_SKU.get((capability, target_id))
    if capability == CAPABILITY_OCR:
        if target_id == DATALAB_TARGET_ID:
            return SKU_DATALAB_OCR_PAGE
        if target_id.startswith(REMOTE_API_TARGET_ID_PREFIX):
            return byok_remote_ocr_sku(engine)
        # local / models-gateway OCR: the operator's own box, no meter.
        return None
    if target_id.startswith(REMOTE_API_TARGET_ID_PREFIX):
        return byok_remote_sku(engine)
    return None


PROVIDER_DIRECT_CHARGE_AUTHORITY = "provider_direct"


def _provider_direct_terms(capability: str, pricing_key: str) -> dict[str, Any]:
    """Pinned/live settlement terms for base's own-key provider estimates."""
    if capability == CAPABILITY_TRANSCRIBE:
        quantity_unit, meter_key = "audio_second", "audio_seconds"
        rounding_mode, decimal_places = "half_even", 3
    elif capability == CAPABILITY_OCR:
        quantity_unit, meter_key = "page", "pages"
        rounding_mode, decimal_places = "exact", None
    elif capability == CAPABILITY_TRANSLATE:
        quantity_unit, meter_key = "character", "characters"
        rounding_mode, decimal_places = "exact", None
    elif capability == CAPABILITY_TO_MARKDOWN:
        quantity_unit, meter_key = "page", "pages"
        rounding_mode, decimal_places = "exact", None
    elif capability == CAPABILITY_GEOCODE:
        quantity_unit, meter_key = "row", "rows"
        rounding_mode, decimal_places = "exact", None
    else:
        raise ValueError(
            f"no provider-direct settlement terms for capability {capability!r}"
        )
    return {
        "terms_version": None,
        "quantity_unit": quantity_unit,
        "quantity_rounding_mode": rounding_mode,
        "quantity_rounding_decimal_places": decimal_places,
        "meter_key": meter_key,
        "meter_units_per_quantity_unit": "1",
        "ceiling_mode": "none",
        "row_settlement_mode": "all_metered",
        "charge_authority": PROVIDER_DIRECT_CHARGE_AUTHORITY,
    }


def commercial_offering_basis_terms(
    offering: CommercialOffering,
) -> dict[str, Any]:
    """The exact offering terms copied into a cost basis and live fact."""
    return {
        "terms_version": offering.quote.terms_version,
        "quantity_unit": offering.quote.quantity_unit,
        "quantity_rounding_mode": offering.quote.rounding.mode,
        "quantity_rounding_decimal_places": (offering.quote.rounding.decimal_places),
        "meter_key": offering.settlement.meter_key,
        "meter_units_per_quantity_unit": (
            offering.settlement.meter_units_per_quantity_unit
        ),
        "ceiling_mode": offering.settlement.ceiling_mode,
        "row_settlement_mode": offering.settlement.row_settlement_mode,
        "charge_authority": offering.charge_authority,
    }


def offering_matches_cost_basis(
    offering: CommercialOffering, basis: PricedCostBasis
) -> bool:
    """Whether the current injected offer is exactly the one consent pinned."""
    return (
        basis.pricing_key == offering.quote.pricing_key
        and basis.unit_rate == decimal_str(offering.quote.unit_rate)
        and all(
            getattr(basis, key) == value
            for key, value in commercial_offering_basis_terms(offering).items()
        )
    )


def _rounded_quantity(value: Any, *, mode: str, decimal_places: int | None) -> Decimal:
    quantity = _to_decimal(value)
    if mode == "exact":
        return quantity
    assert decimal_places is not None
    quantum = Decimal(1).scaleb(-decimal_places)
    rounding = ROUND_HALF_EVEN if mode == "half_even" else ROUND_HALF_UP
    return quantity.quantize(quantum, rounding=rounding)


def quote_commercial_offering(
    *,
    offering: CommercialOffering,
    metered_quantity: float | int | None,
    row_metered_quantities: Mapping[int, float] | None = None,
) -> CostBasis:
    """Mint a basis from an injected offer without consulting funding/SKUs."""
    scale = _to_decimal(offering.settlement.meter_units_per_quantity_unit)
    rounding = offering.quote.rounding
    row_quotes: tuple[tuple[int, str], ...] | None = None
    quantity: Decimal | None
    if row_metered_quantities is not None:
        allocations: list[tuple[int, str]] = []
        raw_quantity_total = Decimal(0)
        quoted_quantity_total = Decimal(0)
        for row_id, raw_metered in sorted(row_metered_quantities.items()):
            if isinstance(row_id, bool) or not isinstance(row_id, int) or row_id <= 0:
                raise ValueError("row quote ids must be positive integers")
            raw_quantity = _to_decimal(raw_metered)
            if raw_quantity < 0:
                raise ValueError("row quote quantities must be non-negative")
            raw_quantity_total += raw_quantity
            row_quantity = _rounded_quantity(
                raw_quantity / scale,
                mode=rounding.mode,
                decimal_places=rounding.decimal_places,
            )
            if offering.settlement.row_settlement_mode == "quoted_successful_rows":
                allocations.append((row_id, decimal_str(row_quantity)))
                quoted_quantity_total += row_quantity
        if offering.settlement.row_settlement_mode == "quoted_successful_rows":
            quantity = quoted_quantity_total
            row_quotes = tuple(allocations)
        else:
            # Non-row settlement still benefits from the authoritative row scan,
            # but its quote is one aggregate quantity and carries no row pin.
            quantity = _rounded_quantity(
                raw_quantity_total / scale,
                mode=rounding.mode,
                decimal_places=rounding.decimal_places,
            )
    elif metered_quantity is None:
        quantity = None
    elif offering.settlement.row_settlement_mode == "quoted_successful_rows":
        raise ValueError(
            "a bounded quoted_successful_rows offer requires per-row quantities"
        )
    else:
        quantity = _rounded_quantity(
            _to_decimal(metered_quantity) / scale,
            mode=rounding.mode,
            decimal_places=rounding.decimal_places,
        )
    return PricedCostBasis(
        pricing_key=offering.quote.pricing_key,
        unit_rate=decimal_str(offering.quote.unit_rate),
        estimated_quantity=(None if quantity is None else decimal_str(quantity)),
        **commercial_offering_basis_terms(offering),
        row_quote_quantities=row_quotes,
    )


def _require_offering_match(
    offering: CommercialOffering,
    *,
    target_id: str,
    capability: str,
    engine: str,
) -> None:
    expected = (target_id, capability, engine)
    actual = (
        offering.match.target_id,
        offering.match.capability,
        offering.match.engine,
    )
    if actual != expected:
        raise ValueError(
            "commercial offering does not match the selected execution choice: "
            f"expected {expected!r}, got {actual!r}"
        )


# ---------------------------------------------------------------------------
# the mint: quote (pre-consent)
# ---------------------------------------------------------------------------


def quote_transcription(
    *,
    target_id: str,
    engine: str,
    funding: Funding,
    offering: CommercialOffering | None,
    audio_seconds: float | None,
    hardware_class: str | None,
    row_audio_seconds: Mapping[int, float] | None = None,
) -> CostBasis:
    """THE cost-basis mint for a transcription invocation.

    ``audio_seconds is None`` means the duration metadata is missing, which
    is genuinely unpriceable — never a zero.
    """
    if offering is not None:
        _require_offering_match(
            offering,
            target_id=target_id,
            capability=CAPABILITY_TRANSCRIBE,
            engine=engine,
        )
        return quote_commercial_offering(
            offering=offering,
            metered_quantity=audio_seconds,
            row_metered_quantities=row_audio_seconds,
        )
    if isinstance(funding, PlatformMetered):
        return OperatorBorneZeroCost()
    sku = sku_for(
        capability=CAPABILITY_TRANSCRIBE,
        target_id=target_id,
        engine=engine,
    )
    if sku is None:
        # local / local-onnx / models-gateway: genuinely operator-borne zero.
        return OperatorBorneZeroCost()
    if audio_seconds is None:
        return UnpriceableCost()

    quoted_rate = byok_audio_second_rate(engine)
    if quoted_rate.rate is None:
        return UnpriceableCost()
    try:
        return PricedCostBasis(
            pricing_key=sku,
            unit_rate=decimal_str(quoted_rate.rate),
            estimated_quantity=decimal_str(round(float(audio_seconds), 3)),
            **_provider_direct_terms(CAPABILITY_TRANSCRIBE, sku),
        )
    except (InvalidOperation, ValueError):
        return UnpriceableCost()


def quote_ocr(
    *,
    target_id: str,
    engine: str,
    funding: Funding,
    offering: CommercialOffering | None,
    pages: int | None,
    row_pages: Mapping[int, int] | None = None,
) -> CostBasis:
    """The cost-basis mint for an OCR invocation — the per-page sibling
    of :func:`quote_transcription`.

    ``pages is None`` means the page count is not knowable before the run (a
    PDF whose ingest probe recorded no page count), which is genuinely
    unpriceable — never a zero, and never a guess. That is the same answer
    ``OcrRecipe.estimate`` gave before the seam (``unknown_cost_estimate`` ->
    the confirm gate); what changed is that the unknown is now a compiled
    ``cost unbounded`` CLAIM the user consents to, beside the egress claim.

    ``hardware_class`` has no counterpart here on purpose: no OCR venue in
    this build meters GPU time, so pinning one would be a fabricated fact
    (``PricedCostBasis``'s both-None hardware-invariant sentinel).
    """
    if offering is not None:
        _require_offering_match(
            offering,
            target_id=target_id,
            capability=CAPABILITY_OCR,
            engine=engine,
        )
        return quote_commercial_offering(
            offering=offering,
            metered_quantity=pages,
            row_metered_quantities=row_pages,
        )
    if isinstance(funding, PlatformMetered):
        return OperatorBorneZeroCost()
    sku = sku_for(
        capability=CAPABILITY_OCR,
        target_id=target_id,
        engine=engine,
    )
    if sku is None:
        # local rapidocr/tesseract, gateway dots.mocr/glm-ocr/surya2/
        # pp-ocrv6/paddleocr-vl: operator
        # already owns that compute and no per-request meter exists.
        return OperatorBorneZeroCost()
    if sku != SKU_DATALAB_OCR_PAGE:
        # A router-served VLM: billed per token over an image, with no
        # per-page rate to quote (see :func:`byok_remote_ocr_sku`).
        return UnpriceableCost()
    if pages is None:
        return UnpriceableCost()
    rate = datalab_ocr_page_rate()
    if rate is None:
        return UnpriceableCost()
    return PricedCostBasis(
        pricing_key=sku,
        unit_rate=decimal_str(rate),
        estimated_quantity=decimal_str(int(pages)),
        **_provider_direct_terms(CAPABILITY_OCR, sku),
    )


def _quote_catalog_sku(
    *,
    capability: str,
    target_id: str,
    engine: str,
    funding: Funding,
    offering: CommercialOffering | None,
    quantity: float | int | None,
) -> CostBasis:
    """The shared body of the four capability-specific mints.

    Every one of them is the same three-step answer — no SKU means genuinely
    free local execution, an unknown quantity or an unknown rate means
    genuinely unpriceable, otherwise rate times quantity in the SKU's own unit
    — and writing it four times is how the four would drift. The per-capability
    entry points below are what callers name, because ``quantity`` means a
    different measured thing in each and a shared signature taking a bare
    number would let a page count be quoted as a character count.

    ``quantity is None`` is never a zero and never a guess: it is the honest
    "not knowable before the run", which compiles to an ``unbounded`` cost
    claim the user consents to.
    """
    if offering is not None:
        _require_offering_match(
            offering,
            target_id=target_id,
            capability=capability,
            engine=engine,
        )
        return quote_commercial_offering(
            offering=offering,
            metered_quantity=quantity,
        )
    if isinstance(funding, PlatformMetered):
        return OperatorBorneZeroCost()
    sku = sku_for(capability=capability, target_id=target_id, engine=engine)
    if sku is None:
        return OperatorBorneZeroCost()
    if quantity is None:
        return UnpriceableCost()
    rate = catalog_list_price(sku)
    if rate is None:
        return UnpriceableCost()
    try:
        return PricedCostBasis(
            pricing_key=sku,
            unit_rate=decimal_str(rate),
            estimated_quantity=decimal_str(quantity),
            **_provider_direct_terms(capability, sku),
        )
    except (InvalidOperation, ValueError):
        return UnpriceableCost()


def quote_translate(
    *,
    target_id: str,
    engine: str,
    funding: Funding,
    offering: CommercialOffering | None,
    characters: int | None,
) -> CostBasis:
    """THE cost-basis mint for a translate invocation — the per-CHARACTER
    sibling of :func:`quote_ocr`.

    ``characters`` is the count of SOURCE characters about to egress, which is
    what both hosted providers bill on. DeepL reports its own
    ``billed_characters`` on the response and Google does not, so the two
    settle against slightly different truths — but they QUOTE the same way,
    off the source text the user selected, which is the only number knowable
    before the call.

    ``engine="llm"`` cannot reach here: it is deliberately absent from
    ``TRANSLATE_ENGINE_TABLE`` (its pricing is two units and a
    ``PricedCostBasis`` carries one), so it refuses at resolution.
    """
    return _quote_catalog_sku(
        capability=CAPABILITY_TRANSLATE,
        target_id=target_id,
        engine=engine,
        funding=funding,
        offering=offering,
        quantity=characters,
    )


def quote_to_markdown(
    *,
    target_id: str,
    engine: str,
    funding: Funding,
    offering: CommercialOffering | None,
    pages: int | None,
) -> CostBasis:
    """THE cost-basis mint for a document-conversion invocation.

    Per PAGE, like OCR and against a different SKU: Datalab's /convert bills
    ``datalab.convert.page`` where its json-output OCR path bills
    ``datalab.ocr.page``. Same venue, same endpoint, same engine symbol, two
    published rates — which is exactly why the SKU key carries the capability
    and the venue rather than the venue alone.
    """
    return _quote_catalog_sku(
        capability=CAPABILITY_TO_MARKDOWN,
        target_id=target_id,
        engine=engine,
        funding=funding,
        offering=offering,
        quantity=pages,
    )


def quote_geocode(
    *,
    target_id: str,
    engine: str,
    funding: Funding,
    offering: CommercialOffering | None,
    rows: int | None,
) -> CostBasis:
    """THE cost-basis mint for a geocoding invocation — per ROW, one lookup
    per selected row.

    Nominatim has no price: it is a free public API, so its egress claim stays
    while the cost basis is absent.
    """
    return _quote_catalog_sku(
        capability=CAPABILITY_GEOCODE,
        target_id=target_id,
        engine=engine,
        funding=funding,
        offering=offering,
        quantity=rows,
    )


def quote_census(
    *,
    target_id: str,
    engine: str,
    funding: Funding,
    offering: CommercialOffering | None,
    rows: int | None,
) -> CostBasis:
    """THE cost-basis mint for a census-enrichment invocation — per ROW.

    Base's US Census target is a free public API.  It keeps an egress claim
    and never carries a commercial offering.  A distinct target supplied by a
    downstream composition may carry its own exact offering through this
    generic capability mint.
    """
    return _quote_catalog_sku(
        capability=CAPABILITY_CENSUS,
        target_id=target_id,
        engine=engine,
        funding=funding,
        offering=offering,
        quantity=rows,
    )


# ---------------------------------------------------------------------------
# the observation: the live cost fact (at the run-start fence)
# ---------------------------------------------------------------------------


def live_cost_fact(
    *,
    capability: str,
    target_id: str,
    engine: str,
    funding: Funding,
    offering: CommercialOffering | None,
    hardware_class: str | None,
) -> dict[str, Any]:
    """What the venue prices at RIGHT NOW, for the run-start fence to score
    the consented ``cost`` promise against.

    An absent rate stays absent (``unevaluable``); admission records that
    diagnostic and refuses to execute under the existing confirmation. It
    never fabricates a rate.
    """
    if offering is not None:
        return {
            "pricing_key": offering.quote.pricing_key,
            "unit_rate": decimal_str(offering.quote.unit_rate),
            **commercial_offering_basis_terms(offering),
        }
    if isinstance(funding, PlatformMetered):
        return {"pricing_key": None}
    sku = sku_for(capability=capability, target_id=target_id, engine=engine)
    fact: dict[str, Any] = {"pricing_key": sku}
    hardware = (hardware_class or "").lower() or None
    if hardware is not None:
        fact["hardware_class"] = hardware
    if sku is None:
        return fact
    fact.update(_provider_direct_terms(capability, sku))
    if sku == SKU_DATALAB_OCR_PAGE:
        # The provider's own list price, observed live: an operator who edits
        # FRISKET_DATALAB_OCR_USD_PER_PAGE after consent creates a visible
        # authorization divergence, just as a raised GPU rate does.
        rate = datalab_ocr_page_rate()
        if rate is not None:
            fact["unit_rate"] = decimal_str(rate)
        return fact
    if capability in _PHASE_4_CAPABILITIES:
        # Same posture as the Datalab OCR page rate above: somebody else's
        # published list price, observed live so an operator who edits the
        # override after consent creates visible authorization divergence. The arm
        # is explicit rather than falling through, because the fallthrough
        # below asks the AUDIO price table about an engine — which would put a
        # per-audio-second number on a per-character SKU.
        rate = catalog_list_price(sku)
        if rate is not None:
            fact["unit_rate"] = decimal_str(rate)
        return fact
    if capability == CAPABILITY_OCR:
        # The only remaining OCR SKU is the router-served VLM's, which has no
        # per-page rate to observe (:func:`byok_remote_ocr_sku`). The absence
        # is the honest answer; asking the AUDIO price table about an OCR
        # engine would put a per-audio-second number on a per-page SKU.
        return fact
    rate = byok_audio_second_rate(engine).rate
    if rate is not None:
        fact["unit_rate"] = decimal_str(rate)
    return fact


# ---------------------------------------------------------------------------
# settlement: "what did it cost"
# ---------------------------------------------------------------------------


def settle(
    *,
    cost_basis: Any,
    metered_units: Sequence[Mapping[str, Any]],
    price_card_version: str | None,
    terminal_status: Literal["running", "completed", "failed", "cancelled"],
    all_rows_cancelled: bool,
) -> dict[str, Any]:
    """Rate model-call facts exclusively under the attempt's pinned terms."""
    if not isinstance(terminal_status, str) or terminal_status not in (
        "running",
        "completed",
        "failed",
        "cancelled",
    ):
        raise ValueError(f"unsupported settlement terminal status: {terminal_status}")
    if not isinstance(all_rows_cancelled, bool):
        raise ValueError("all_rows_cancelled must be a bool")
    if all_rows_cancelled and terminal_status != "cancelled":
        raise ValueError(
            "all_rows_cancelled is settlement authority only for a cancelled run"
        )
    if all_rows_cancelled and metered_units:
        raise ValueError(
            "cancelled-row facts must be excluded before all-cancelled settlement"
        )

    kind = (cost_basis or {}).get("kind") if isinstance(cost_basis, Mapping) else None
    if kind == "operator_borne_zero":
        return {
            "price_card_version": price_card_version,
            "terminal_status": terminal_status,
            "pricing_key": None,
            "charge_usd": "0",
            "rated_calls": 0,
            "unmetered_calls": 0,
        }
    if kind != "priced":
        # Unpriceable, or an attempt with no cost basis at all: an honest
        # "cannot say", never a fabricated total.
        return {
            "price_card_version": price_card_version,
            "terminal_status": terminal_status,
            "pricing_key": None,
            "charge_usd": None,
            "rated_charge_usd": None,
            "charged_quantity": None,
            "absorbed_overage_usd": None,
            "rated_calls": 0,
            "unmetered_calls": len(metered_units),
        }

    required_terms = {
        "terms_version",
        "quantity_unit",
        "quantity_rounding_mode",
        "quantity_rounding_decimal_places",
        "meter_key",
        "meter_units_per_quantity_unit",
        "ceiling_mode",
        "row_settlement_mode",
        "charge_authority",
    }
    if not required_terms.issubset(cost_basis):
        return {
            "price_card_version": price_card_version,
            "terminal_status": terminal_status,
            "pricing_key": cost_basis.get("pricing_key"),
            "charge_usd": None,
            "rated_charge_usd": None,
            "charged_quantity": None,
            "absorbed_overage_usd": None,
            "rated_calls": 0,
            "unmetered_calls": len(metered_units),
            "unsettleable": "settlement_terms_missing",
        }
    if cost_basis.get("terms_version") != price_card_version:
        return {
            "price_card_version": price_card_version,
            "terminal_status": terminal_status,
            "pricing_key": cost_basis.get("pricing_key"),
            "charge_usd": None,
            "rated_charge_usd": None,
            "charged_quantity": None,
            "absorbed_overage_usd": None,
            "rated_calls": 0,
            "unmetered_calls": len(metered_units),
            "unsettleable": "terms_version_mismatch",
        }
    pricing_key_value = cost_basis.get("pricing_key")
    unit_rate_value = cost_basis.get("unit_rate")
    estimated_quantity_value = cost_basis.get("estimated_quantity")
    quantity_unit_value = cost_basis.get("quantity_unit")
    terms_version_value = cost_basis.get("terms_version")
    meter_key_value = cost_basis.get("meter_key")
    meter_scale_value = cost_basis.get("meter_units_per_quantity_unit")
    charge_authority_value = cost_basis.get("charge_authority")

    def clean_text(value: Any) -> bool:
        return isinstance(value, str) and bool(value) and value == value.strip()

    malformed_pinned_value = (
        not clean_text(pricing_key_value)
        or not isinstance(unit_rate_value, str)
        # Missing and explicit JSON null are different consent facts.  Null
        # is the intentional unbounded quote sentinel; absence means this is
        # not the complete basis the user pinned.
        or "estimated_quantity" not in cost_basis
        or not clean_text(quantity_unit_value)
        or not clean_text(meter_key_value)
        or not isinstance(meter_scale_value, str)
        or not clean_text(charge_authority_value)
        or (terms_version_value is not None and not clean_text(terms_version_value))
        or (price_card_version is not None and not clean_text(price_card_version))
        or (
            estimated_quantity_value is not None
            and (
                isinstance(estimated_quantity_value, bool)
                or not isinstance(estimated_quantity_value, (int, str))
            )
        )
    )
    try:
        if malformed_pinned_value:
            raise ValueError("malformed pinned settlement value")
        pricing_key = pricing_key_value
        rate = _to_decimal(unit_rate_value)
        consented = (
            None
            if estimated_quantity_value is None
            else _to_decimal(estimated_quantity_value)
        )
        quantity_unit = quantity_unit_value
        meter_key = meter_key_value
        meter_scale = _to_decimal(meter_scale_value)
        charge_authority = charge_authority_value
    except (KeyError, TypeError, InvalidOperation, ValueError):
        return {
            "price_card_version": price_card_version,
            "terminal_status": terminal_status,
            "pricing_key": cost_basis.get("pricing_key"),
            "charge_usd": None,
            "rated_calls": 0,
            "unmetered_calls": len(metered_units),
            "unsettleable": "settlement_terms_invalid",
        }
    ceiling_mode = cost_basis.get("ceiling_mode")
    row_mode = cost_basis.get("row_settlement_mode")
    rounding_mode = cost_basis.get("quantity_rounding_mode")
    rounding_places = cost_basis.get("quantity_rounding_decimal_places")
    if (
        rate < 0
        or meter_scale <= 0
        or not meter_key
        or not quantity_unit
        or not charge_authority
        or (consented is not None and consented < 0)
        or not isinstance(ceiling_mode, str)
        or ceiling_mode not in ("none", "consented_quantity")
        or not isinstance(row_mode, str)
        or row_mode not in ("all_metered", "quoted_successful_rows")
        or not isinstance(rounding_mode, str)
        or rounding_mode not in ("exact", "half_even", "half_up")
        or (rounding_mode == "exact" and rounding_places is not None)
        or (
            rounding_mode != "exact"
            and (
                isinstance(rounding_places, bool)
                or not isinstance(rounding_places, int)
                or not 0 <= rounding_places <= 18
            )
        )
        or (all_rows_cancelled and row_mode != "quoted_successful_rows")
    ):
        return {
            "price_card_version": price_card_version,
            "terminal_status": terminal_status,
            "pricing_key": pricing_key,
            "charge_usd": None,
            "rated_calls": 0,
            "unmetered_calls": len(metered_units),
            "unsettleable": "settlement_terms_invalid",
        }

    if not metered_units and not all_rows_cancelled:
        # NO metering row carries this attempt's id at all — which is not the
        # same fact as "the calls we found metered zero" (that case is below,
        # and still rates to 0). A priced basis says work was authorized to
        # egress and be billed; zero rows means the ledger has nothing to
        # rate, so the honest answer is "cannot say", the same posture
        # ``run_reclaimed`` already takes when compaction removed the rows.
        # Summing an empty set into "0" is the fabricated total ruling 7
        # exists to refuse, and it is worse than an absence because it reads
        # as an answer: a live BYOK transcription that failed after upload
        # would report the user's own key as having cost them nothing.
        return {
            "price_card_version": price_card_version,
            "terminal_status": terminal_status,
            "pricing_key": pricing_key,
            "unit_rate": decimal_str(rate),
            "quantity_unit": quantity_unit,
            "charge_usd": None,
            "rated_charge_usd": None,
            "charged_quantity": None,
            "absorbed_overage_usd": None,
            "rated_calls": 0,
            "unmetered_calls": 0,
            "unsettleable": "unmetered",
        }

    metered = Decimal(0)
    rated = 0
    unmetered = 0
    for units in metered_units:
        raw = units.get(meter_key) if isinstance(units, Mapping) else None
        if raw is None:
            unmetered += 1
            continue
        try:
            quantity = _to_decimal(raw)
        except (InvalidOperation, ValueError):
            unmetered += 1
            continue
        if quantity < 0:
            unmetered += 1
            continue
        metered += quantity
        rated += 1

    billable = metered / meter_scale

    rated_charge = rate * billable
    receipt: dict[str, Any] = {
        "price_card_version": price_card_version,
        "terminal_status": terminal_status,
        "pricing_key": pricing_key,
        "unit_rate": decimal_str(rate),
        "quantity_unit": quantity_unit,
        "charge_authority": charge_authority,
        "ceiling_mode": ceiling_mode,
        "row_settlement_mode": row_mode,
        "metered_quantity": decimal_str(metered),
        "metered_unit": meter_key,
        "billable_quantity": decimal_str(billable),
        "rated_charge_usd": decimal_str(rated_charge),
        "charged_quantity": decimal_str(billable),
        "charge_usd": decimal_str(rated_charge),
        "absorbed_overage_usd": "0",
        "rated_calls": rated,
        "unmetered_calls": unmetered,
    }

    if unmetered:
        # A plausible partial number is more dangerous than no answer: it
        # reads as the total even though at least one authorized provider call
        # could not be rated.  Keep the measured lower-bound evidence, but
        # refuse a settlement amount.
        receipt["charge_usd"] = None
        receipt["rated_charge_usd"] = None
        receipt["charged_quantity"] = None
        receipt["absorbed_overage_usd"] = None
        receipt["unsettleable"] = "unmetered"

    if consented is None:
        receipt["consented_quantity"] = None
        receipt["exceeds_consented"] = None
        if ceiling_mode == "consented_quantity" and not unmetered:
            receipt["charge_usd"] = None
            receipt["charged_quantity"] = None
            receipt["absorbed_overage_usd"] = None
            receipt["unsettleable"] = "consent_ceiling_missing"
        return receipt

    receipt["consented_quantity"] = decimal_str(consented)
    # With incomplete metering, ``billable`` is only a lower bound. It can
    # prove an overrun when already above the ceiling, but a false boolean
    # would incorrectly certify that the unknown remainder stayed within it.
    if billable > consented:
        receipt["exceeds_consented"] = True
    elif unmetered:
        receipt["exceeds_consented"] = None
    else:
        receipt["exceeds_consented"] = False

    if ceiling_mode == "consented_quantity" and not unmetered:
        charged_quantity = min(billable, consented)
        charge = rate * charged_quantity
        receipt["charged_quantity"] = decimal_str(charged_quantity)
        receipt["charge_usd"] = decimal_str(charge)
        receipt["absorbed_overage_usd"] = decimal_str(rated_charge - charge)
    return receipt
