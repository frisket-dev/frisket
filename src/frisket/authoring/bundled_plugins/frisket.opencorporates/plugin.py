"""OpenCorporates row enrichment for company-name and exact-key lookups.

The plugin deliberately stays table-shaped: it writes a compact set of match
diagnostics and company fields back onto each source row. It does not create
officer, filing, or relationship tables. Reconciliation scores are provider
ranking signals, not calibrated probabilities; the configurable threshold is
therefore a provisional materialization rule that remains visible in the
output columns.
"""

from __future__ import annotations

import math
import re
from typing import Any, ClassVar
from urllib.parse import quote, unquote

from datetime import date

from pydantic import BaseModel, Field, field_validator

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import ActionParams, ColumnRef, Link, Row, RowResult
from frisket.actions.opencorporates_types import OpenCorporates

from frisket.plugins.sdk import Plugin, PluginUserError


DEFAULT_MINIMUM_MATCH_SCORE = 75.0

_DIAGNOSTIC_OUTPUTS = (
    "oc_match_state",
    "oc_match_method",
    "oc_match_score",
    "oc_match_threshold",
    "oc_upstream_match",
)
_COMPANY_OUTPUTS = (
    "oc_company_id",
    "oc_company_name",
    "oc_company_number",
    "oc_jurisdiction_code",
    "oc_company_type",
    "oc_company_status",
    "oc_inactive",
    "oc_incorporation_date",
    "oc_dissolution_date",
    "oc_registered_address",
    "oc_company_url",
    "oc_registry_url",
    "oc_retrieved_at",
)
OUTPUT_NAMES = _DIAGNOSTIC_OUTPUTS + _COMPANY_OUTPUTS

_CANDIDATE_ID = re.compile(r"^/companies/([^/]+)/([^/]+)$")
_JURISDICTION_CODE = re.compile(r"^[a-z0-9_]{2,16}$")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _jurisdiction(value: Any, *, required: bool = False) -> str | None:
    text = _text(value)
    if text is None:
        if required:
            raise PluginUserError("jurisdiction_code is required")
        return None
    normalized = text.lower()
    if _JURISDICTION_CODE.fullmatch(normalized) is None:
        raise PluginUserError("jurisdiction_code must be an OpenCorporates code")
    return normalized


def _company_number(value: Any) -> str:
    if isinstance(value, bool):
        raise PluginUserError("company_number must be text or an integer")
    text = _text(value)
    if text is None:
        raise PluginUserError("company_number is required")
    if any(ord(char) < 32 for char in text):
        raise PluginUserError("company_number is invalid")
    return text


def _company_id(jurisdiction_code: str, company_number: str) -> str:
    return (
        f"/companies/{quote(jurisdiction_code, safe='')}/"
        f"{quote(company_number, safe='')}"
    )


def _minimum_score(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise PluginUserError("minimum_match_score must be a number from 0 to 100")
    try:
        score = float(value)
    except (TypeError, ValueError):
        raise PluginUserError(
            "minimum_match_score must be a number from 0 to 100"
        ) from None
    if not math.isfinite(score) or not 0 <= score <= 100:
        raise PluginUserError("minimum_match_score must be a number from 0 to 100")
    return score


def _empty_result(
    state: str,
    *,
    method: str,
    score: float | None = None,
    threshold: float | None = None,
    upstream_match: bool | None = None,
) -> dict[str, Any]:
    result = {name: None for name in OUTPUT_NAMES}
    result.update(
        {
            "oc_match_state": state,
            "oc_match_method": method,
            "oc_match_score": score,
            "oc_match_threshold": threshold,
            "oc_upstream_match": upstream_match,
        }
    )
    return result


def _candidate_key(candidate: dict[str, Any]) -> tuple[str, str, str]:
    candidate_id = candidate.get("id")
    if not isinstance(candidate_id, str):
        raise ValueError("OpenCorporates reconciliation candidate has no id")
    match = _CANDIDATE_ID.fullmatch(candidate_id)
    if match is None:
        raise ValueError("OpenCorporates reconciliation candidate id is malformed")
    jurisdiction = _jurisdiction(unquote(match.group(1)), required=True)
    assert jurisdiction is not None
    number = _company_number(unquote(match.group(2)))
    canonical_id = _company_id(jurisdiction, number)
    return jurisdiction, number, canonical_id


def _candidate_score(candidate: dict[str, Any]) -> float:
    value = candidate.get("score")
    if isinstance(value, bool):
        raise ValueError("OpenCorporates reconciliation score is malformed")
    try:
        score = float(value)
    except (TypeError, ValueError):
        raise ValueError("OpenCorporates reconciliation score is malformed") from None
    if not math.isfinite(score):
        raise ValueError("OpenCorporates reconciliation score is malformed")
    return score


def _registered_address(company: dict[str, Any]) -> str | None:
    full = _text(company.get("registered_address_in_full"))
    if full is not None:
        return full
    structured = company.get("registered_address")
    if not isinstance(structured, dict):
        return None
    parts = [
        _text(structured.get(name))
        for name in (
            "street_address",
            "locality",
            "region",
            "postal_code",
            "country",
        )
    ]
    values = [part for part in parts if part is not None]
    return ", ".join(values) or None


def _company_result(
    company: dict[str, Any],
    *,
    method: str,
    score: float | None,
    threshold: float | None,
    upstream_match: bool | None,
) -> dict[str, Any]:
    jurisdiction = _jurisdiction(company.get("jurisdiction_code"), required=True)
    assert jurisdiction is not None
    number = _company_number(company.get("company_number"))
    inactive = company.get("inactive")
    if not isinstance(inactive, bool):
        inactive = None
    result = _empty_result(
        "matched",
        method=method,
        score=score,
        threshold=threshold,
        upstream_match=upstream_match,
    )
    result.update(
        {
            "oc_company_id": _company_id(jurisdiction, number),
            "oc_company_name": _text(company.get("name")),
            "oc_company_number": number,
            "oc_jurisdiction_code": jurisdiction,
            "oc_company_type": _text(company.get("company_type")),
            "oc_company_status": _text(company.get("current_status")),
            "oc_inactive": inactive,
            "oc_incorporation_date": _text(company.get("incorporation_date")),
            "oc_dissolution_date": _text(company.get("dissolution_date")),
            "oc_registered_address": _registered_address(company),
            "oc_company_url": _text(company.get("opencorporates_url")),
            "oc_registry_url": _text(company.get("registry_url")),
            "oc_retrieved_at": _text(company.get("retrieved_at")),
        }
    )
    return result


class NameColumn(ColumnRef[str]):
    accepted_column_types: ClassVar = ("text", "category")


class NumberColumn(ColumnRef[str | int]):
    accepted_column_types: ClassVar = ("text", "category", "integer")


class MatchParams(ActionParams):
    company_name: NameColumn
    minimum_match_score: float = Field(default=75.0, ge=0, le=100)
    jurisdiction_code: str | None = None

    @field_validator("jurisdiction_code", mode="before")
    @classmethod
    def jurisdiction(cls, value):
        try:
            return _jurisdiction(value)
        except PluginUserError as error:
            raise ValueError(str(error)) from None

    @field_validator("minimum_match_score", mode="before")
    @classmethod
    def threshold(cls, value):
        try:
            return _minimum_score(value)
        except PluginUserError as error:
            raise ValueError(str(error)) from None


class LookupParams(ActionParams):
    jurisdiction_code: NameColumn
    company_number: NumberColumn


class CompanyOutput(BaseModel):
    oc_match_state: str
    oc_match_method: str
    oc_match_score: float | None
    oc_match_threshold: float | None
    oc_upstream_match: bool | None
    oc_company_id: str | None
    oc_company_name: str | None
    oc_company_number: str | None
    oc_jurisdiction_code: str | None
    oc_company_type: str | None
    oc_company_status: str | None
    oc_inactive: bool | None
    oc_incorporation_date: date | None
    oc_dissolution_date: date | None
    oc_registered_address: str | None
    oc_company_url: Link | None
    oc_registry_url: Link | None
    oc_retrieved_at: str | None


async def match_company(
    params: MatchParams, row: Row, companies: OpenCorporates
) -> RowResult[CompanyOutput]:
    threshold = _minimum_score(params.minimum_match_score)
    jurisdiction = params.jurisdiction_code
    name = _text(params.company_name.read(row))
    if name is None:
        result = _empty_result("no_input", method="reconciliation", threshold=threshold)
    else:
        result = await _match(companies, name, jurisdiction, threshold)
    return RowResult(output=CompanyOutput.model_validate(result))


async def _match(companies, name, jurisdiction, threshold):
    candidates = await companies.reconcile(name, jurisdiction_code=jurisdiction)
    if not candidates:
        return _empty_result("no_match", method="reconciliation", threshold=threshold)
    candidate = candidates[0]
    candidate_jurisdiction, candidate_number, _candidate_id = _candidate_key(candidate)
    score = _candidate_score(candidate)
    upstream_match = (
        candidate.get("match") if isinstance(candidate.get("match"), bool) else None
    )
    if score < threshold:
        return _empty_result(
            "below_threshold",
            method="reconciliation",
            score=score,
            threshold=threshold,
            upstream_match=upstream_match,
        )
    company = await companies.fetch(candidate_jurisdiction, candidate_number)
    if company is None:
        return _empty_result(
            "not_found",
            method="reconciliation",
            score=score,
            threshold=threshold,
            upstream_match=upstream_match,
        )
    return _company_result(
        company,
        method="reconciliation",
        score=score,
        threshold=threshold,
        upstream_match=upstream_match,
    )


async def lookup_company(
    params: LookupParams, row: Row, companies: OpenCorporates
) -> RowResult[CompanyOutput]:
    jurisdiction = params.jurisdiction_code.read(row)
    number = params.company_number.read(row)
    if _text(jurisdiction) is None or _text(number) is None:
        result = _empty_result("no_input", method="exact_key")
    else:
        company = await companies.fetch(
            _jurisdiction(jurisdiction, required=True), _company_number(number)
        )
        result = (
            _empty_result("not_found", method="exact_key")
            if company is None
            else _company_result(
                company,
                method="exact_key",
                score=None,
                threshold=None,
                upstream_match=None,
            )
        )
    return RowResult(output=CompanyOutput.model_validate(result))


MATCH_COMPANY = action(
    name="match_company",
    title="Match company with OpenCorporates",
    description=(
        "Reconciles each company name, optionally within one jurisdiction, then "
        "writes company fields only when the provider score meets the configured "
        "threshold. The score is a ranking signal, not a calibrated probability."
    ),
    category=ActionCategory.EXTRACT,
    run=map_rows(match_company),
)

LOOKUP_COMPANY = action(
    name="lookup_company",
    title="Look up OpenCorporates company key",
    description=(
        "Looks up the exact OpenCorporates company key formed by jurisdiction "
        "code plus company number. Exact keys bypass reconciliation scoring."
    ),
    category=ActionCategory.EXTRACT,
    run=map_rows(lookup_company),
)

plugin = Plugin(
    id="frisket.opencorporates",
    version="1.0.0",
    capabilities=["plugin:trusted_local_backend"],
    auto_enable=False,
    actions=(MATCH_COMPANY, LOOKUP_COMPANY),
)
