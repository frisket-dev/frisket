"""Central catalog for non-LLM external service tariffs.

LLM/model pricing lives in ``frisket.llm.pricing`` because it is generated
from LiteLLM's model table. This module owns non-LLM service prices that are
otherwise easy to duplicate between recipes, frontend copy, and diagnostics.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


def _validate_unit_price(price: Decimal, *, label: str) -> Decimal:
    if not price.is_finite() or price < 0:
        raise ValueError(f"{label} must be a non-negative finite decimal USD value")
    return price


@dataclass(frozen=True)
class ExternalPricingEntry:
    key: str
    label: str
    provider: str
    unit: str
    default_unit_price_usd: Decimal | None
    env_var: str | None
    billable: bool
    external_api: bool
    description: str

    def unit_price_decimal(self) -> Decimal | None:
        if self.env_var:
            raw = os.environ.get(self.env_var)
            if raw not in (None, ""):
                try:
                    return _validate_unit_price(
                        Decimal(str(raw)),
                        label=self.env_var,
                    )
                except InvalidOperation as e:
                    raise ValueError(
                        f"{self.env_var} must be a decimal USD value"
                    ) from e
        if self.default_unit_price_usd is None:
            return None
        return _validate_unit_price(
            self.default_unit_price_usd,
            label=f"default price for {self.key}",
        )

    def as_dict(self) -> dict[str, Any]:
        unit_price = self.unit_price_decimal()
        out: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "provider": self.provider,
            "unit": self.unit,
            "unit_price_usd": float(unit_price) if unit_price is not None else None,
            "unit_price_usd_string": str(unit_price)
            if unit_price is not None
            else None,
            "env_var": self.env_var,
            "billable": self.billable,
            "external_api": self.external_api,
            "cost_source": (
                "configured_catalog"
                if unit_price is not None
                else "free_public_api"
                if self.external_api and not self.billable
                else "unknown"
            ),
            "description": self.description,
        }
        return out


GEOCODE_OPENCAGE_ROW = "geocode.opencage.row"
# Durable provider-fact identifiers only.  They are deliberately absent from
# ``CATALOG``: Nominatim and Census are free public APIs, not pricing rows.
GEOCODE_NOMINATIM_ROW = "geocode.nominatim.row"
# Compatibility alias for the paid/default geocoder path used by older code.
GEOCODE_EXTERNAL_GEOCODER = GEOCODE_OPENCAGE_ROW
CENSUS_US_ACS = "census.us_census_acs"
DATALAB_CONVERT_PAGE = "datalab.convert.page"
DATALAB_OCR_PAGE = "datalab.ocr.page"
DEEPL_TRANSLATE_CHAR = "deepl.translate.char"
GOOGLE_TRANSLATE_CHAR = "google.translate.char"

CATALOG: dict[str, ExternalPricingEntry] = {
    GEOCODE_EXTERNAL_GEOCODER: ExternalPricingEntry(
        key=GEOCODE_EXTERNAL_GEOCODER,
        label="OpenCage geocoder request",
        provider="OpenCage",
        unit="row",
        default_unit_price_usd=Decimal("0.01"),
        env_var="FRISKET_GEOCODE_USD_PER_ROW",
        billable=True,
        external_api=True,
        description=(
            "Conservative per-row tariff for address geocoding through OpenCage."
        ),
    ),
    DATALAB_CONVERT_PAGE: ExternalPricingEntry(
        key=DATALAB_CONVERT_PAGE,
        label="Datalab hosted Marker conversion (per page)",
        provider="Datalab",
        unit="page",
        # Recorded live (tests/fixtures/datalab/convert_complete.json): a
        # 1-page mode=fast /convert call billed
        # cost_breakdown.final_cost_cents == 1.0 on this key's plan. Datalab's
        # published pricing (datalab.to/pricing, documentation.datalab.to/
        # platform/billing.md) states rates vary by processor/mode/plan and
        # are resolved per-team server-side — this is a representative
        # default, not a contractual rate.
        default_unit_price_usd=Decimal("0.01"),
        env_var="FRISKET_DATALAB_CONVERT_USD_PER_PAGE",
        billable=True,
        external_api=True,
        description=(
            "Per-page tariff for Datalab's hosted Marker document-to-"
            "markdown API (mode=fast); balanced/accurate modes likely cost "
            "more per Datalab's rate card — override via the env var if on "
            "a different mode/plan."
        ),
    ),
    DEEPL_TRANSLATE_CHAR: ExternalPricingEntry(
        key=DEEPL_TRANSLATE_CHAR,
        label="DeepL API translation (per character)",
        provider="DeepL",
        unit="character",
        # ESTIMATED representative rate — DeepL API Pro usage is ~$25 per
        # million characters (deepl.com/pro-api), i.e. $0.000025/char; the Free
        # tier bills $0 up to 500k chars/month. Which plan a given key is on is
        # unknowable from the API, so this is a representative default (marked
        # estimated in receipts), overridable via the env var. Billed characters
        # come from DeepL's own `billed_characters` response field
        # (integrations/deepl.py), not a client-side length guess.
        default_unit_price_usd=Decimal("0.000025"),
        env_var="FRISKET_DEEPL_TRANSLATE_USD_PER_CHAR",
        billable=True,
        external_api=True,
        description=(
            "Per-character tariff for the DeepL API. Representative Pro-tier "
            "rate; Free-tier keys bill $0 up to the monthly quota. Recorded "
            "spend is marked estimated — override via the env var for your plan."
        ),
    ),
    GOOGLE_TRANSLATE_CHAR: ExternalPricingEntry(
        key=GOOGLE_TRANSLATE_CHAR,
        label="Google Cloud Translation (per character)",
        provider="Google",
        unit="character",
        # ESTIMATED representative rate — Google Cloud Translation v2 bills
        # ~$20 per million source characters (cloud.google.com/translate/
        # pricing), i.e. $0.00002/char, with a monthly free tier. Google's v2
        # response carries no billed-character field, so the count is the source
        # text length (UTF-8 codepoints), which is what Google bills on.
        default_unit_price_usd=Decimal("0.00002"),
        env_var="FRISKET_GOOGLE_TRANSLATE_USD_PER_CHAR",
        billable=True,
        external_api=True,
        description=(
            "Per-character tariff for Google Cloud Translation (v2). "
            "Representative rate; a monthly free tier may apply. Recorded spend "
            "is marked estimated — override via the env var for your plan."
        ),
    ),
    DATALAB_OCR_PAGE: ExternalPricingEntry(
        key=DATALAB_OCR_PAGE,
        label="Datalab hosted OCR (per page)",
        provider="Datalab",
        unit="page",
        # Recorded live (tests/fixtures/datalab/ocr_complete.json): a
        # 1-page /ocr call billed
        # cost_breakdown.final_cost_cents == 1.0 on this key's plan. Same
        # per-team-rate caveat as DATALAB_CONVERT_PAGE above.
        default_unit_price_usd=Decimal("0.01"),
        env_var="FRISKET_DATALAB_OCR_USD_PER_PAGE",
        billable=True,
        external_api=True,
        description=(
            "Per-page tariff for Datalab's hosted OCR (datalab.to). Served "
            "via /convert(output_format=json) — the documented replacement "
            "for the deprecated /ocr endpoint (Datalab's migration guide) — "
            "see integrations/datalab.py."
        ),
    ),
}


def external_pricing_catalog() -> dict[str, dict[str, Any]]:
    return {key: external_pricing_entry(key) for key in CATALOG}


def external_pricing_entry(key: str) -> dict[str, Any]:
    try:
        return CATALOG[key].as_dict()
    except KeyError as e:
        raise KeyError(f"unknown external pricing key: {key}") from e


def external_unit_price_usd(key: str) -> float | None:
    price = _unit_price(key)
    return float(price) if price is not None else None


def external_unit_price_string(key: str) -> str | None:
    price = _unit_price(key)
    return str(price) if price is not None else None


def _unit_price(key: str) -> Decimal | None:
    """Catalog rate for one key."""
    return CATALOG[key].unit_price_decimal()


def estimate_external_cost(key: str, quantity: float | int) -> dict[str, Any]:
    entry = external_pricing_entry(key)
    unit_price = entry["unit_price_usd"]
    cost = round(float(quantity) * unit_price, 8) if unit_price is not None else None
    return {
        "cost": cost,
        "pricing_key": key,
        "pricing_label": entry["label"],
        "unit": entry["unit"],
        "quantity": quantity,
        "unit_price_usd": unit_price,
        "external_api": entry["external_api"],
        "cost_source": entry["cost_source"],
    }
