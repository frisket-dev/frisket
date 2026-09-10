"""Typed bundled OpenCorporates declarations and preserved company mapping."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from frisket.actions.types import Row

PLUGIN_DIR = (
    Path(__file__).resolve().parents[2]
    / "src/frisket/authoring/bundled_plugins/frisket.opencorporates"
)
PLUGIN_ID = "frisket.opencorporates"
MATCH_KIND = PLUGIN_ID + ".match_company"
LOOKUP_KIND = PLUGIN_ID + ".lookup_company"


@pytest.fixture
def module():
    spec = importlib.util.spec_from_file_location(
        "_test_opencorporates", PLUGIN_DIR / "plugin.py"
    )
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def _company(
    *,
    jurisdiction: str = "gb",
    number: str = "07444723",
) -> dict[str, object]:
    return {
        "name": "OPENCORPORATES LTD",
        "company_number": number,
        "jurisdiction_code": jurisdiction,
        "company_type": "Private Limited Company",
        "current_status": "Active",
        "inactive": False,
        "incorporation_date": "2010-11-10",
        "dissolution_date": None,
        "registered_address_in_full": "Aldgate Tower, London, E1 8FA",
        "opencorporates_url": (
            f"https://opencorporates.com/companies/{jurisdiction}/{number}"
        ),
        "registry_url": "https://find-and-update.company-information.service.gov.uk/",
        "retrieved_at": "2026-07-01T12:00:00Z",
    }


def _candidate(*, score: float = 88, upstream_match: bool = True):
    return {
        "id": "/companies/gb/07444723",
        "name": "OPENCORPORATES LTD",
        "score": score,
        "match": upstream_match,
        "uri": "http://opencorporates.com/companies/gb/07444723",
        "type": [{"id": "/organization/organization", "name": "Organization"}],
    }


def _run(awaitable):
    return asyncio.run(awaitable)


def test_manifest_has_only_typed_authority_and_derived_requirements(module):
    from frisket.plugins.manifest_generate import generate_manifest_text

    # rule19: compare the installed manifest with the independently generated contract.
    manifest = (PLUGIN_DIR / "plugin.json").read_text()
    raw = json.loads(manifest)
    assert manifest == generate_manifest_text(PLUGIN_DIR)
    assert raw["auto_enable"] is False
    assert raw["requires"] == {
        "capabilities": ["plugin:trusted_local_backend", "external:opencorporates"],
        "secrets": ["OPENCORPORATES_API_TOKEN"],
    }
    assert {item["kind"] for item in raw["runtime"]["actions"]} == {
        MATCH_KIND,
        LOOKUP_KIND,
    }
    for item in raw["runtime"]["actions"]:
        assert item["handler_api"] == "typed_action"
        assert (
            not {"writes", "inputs", "params", "rate_limit", "cost", "execution"}
            & item.keys()
        )
        catalog = item["catalog_entry"]
        assert catalog["async_mode"] == "queued"
        assert catalog["cost_policy"] == {
            "kind": "external_metered",
            "requires_confirmation": True,
        }
        assert set(catalog["output_schema"]["properties"]) == set(module.OUTPUT_NAMES)
    assert [field.key for field in module.MATCH_COMPANY.run.output_fields] == list(
        module.OUTPUT_NAMES
    )


class Provider:
    def __init__(self, candidates=None, company=None):
        self.candidates = candidates or []
        self.company = company
        self.calls = []

    async def reconcile(self, company_name, *, jurisdiction_code=None):
        self.calls.append(("reconcile", (company_name, jurisdiction_code)))
        return self.candidates

    async def fetch(self, jurisdiction_code, company_number):
        self.calls.append(("fetch", (jurisdiction_code, company_number)))
        return self.company


@pytest.mark.parametrize(
    "score,state,count", [(79, "matched", 2), (74.9, "below_threshold", 1)]
)
def test_match_keeps_threshold_diagnostics_and_company_mapping(
    module, score, state, count
):
    provider = Provider([_candidate(score=score, upstream_match=False)], _company())
    result = _run(
        module.match_company(
            module.MatchParams(company_name="name", jurisdiction_code="GB"),
            Row({"name": " OpenCorporates Ltd "}),
            provider,
        )
    ).output.model_dump(mode="json")
    assert set(result) == set(module.OUTPUT_NAMES)
    assert result["oc_match_state"] == state
    assert result["oc_match_method"] == "reconciliation"
    assert result["oc_match_score"] == score
    assert result["oc_match_threshold"] == 75
    assert result["oc_upstream_match"] is False
    assert provider.calls[0] == ("reconcile", ("OpenCorporates Ltd", "gb"))
    assert len(provider.calls) == count
    if state == "matched":
        assert provider.calls[1] == ("fetch", ("gb", "07444723"))
        assert result["oc_company_id"] == "/companies/gb/07444723"
        assert result["oc_company_name"] == "OPENCORPORATES LTD"
        assert result["oc_company_number"] == "07444723"
        assert result["oc_registered_address"] == "Aldgate Tower, London, E1 8FA"
        assert result["oc_incorporation_date"] == "2010-11-10"
    else:
        assert all(result[name] is None for name in module._COMPANY_OUTPUTS)


@pytest.mark.parametrize(
    "name,state,calls", [("  ", "no_input", 0), ("Missing Co", "no_match", 1)]
)
def test_empty_name_and_no_candidates_return_null_safe_schema(
    module, name, state, calls
):
    provider = Provider()
    result = _run(
        module.match_company(
            module.MatchParams(company_name="name"),
            Row({"name": name}),
            provider,
        )
    ).output.model_dump(mode="json")
    assert set(result) == set(module.OUTPUT_NAMES)
    assert result["oc_match_state"] == state
    assert result["oc_match_threshold"] == 75
    assert all(result[key] is None for key in module._COMPANY_OUTPUTS)
    assert len(provider.calls) == calls


@pytest.mark.parametrize(
    "jurisdiction,number,state",
    [
        ("US_DE", "0012345", "not_found"),
        ("", None, "no_input"),
    ],
)
def test_exact_key_bypasses_scores_and_handles_blank_or_missing(
    module, jurisdiction, number, state
):
    provider = Provider()
    result = _run(
        module.lookup_company(
            module.LookupParams(jurisdiction_code="region", company_number="key"),
            Row({"region": jurisdiction, "key": number}),
            provider,
        )
    ).output.model_dump(mode="json")
    assert result["oc_match_state"] == state
    assert result["oc_match_method"] == "exact_key"
    assert result["oc_match_score"] is result["oc_match_threshold"] is None
    assert all(result[key] is None for key in module._COMPANY_OUTPUTS)
    assert provider.calls == (
        [] if state == "no_input" else [("fetch", ("us_de", "0012345"))]
    )


def test_company_numbers_remain_strings_and_url_encoded(module):
    assert module._company_number("0012/34") == "0012/34"
    assert module._company_id("cz", "0012/34") == "/companies/cz/0012%2F34"


@pytest.mark.parametrize(
    "value",
    [True, False, None, "not-a-number", -0.01, 100.01, float("inf"), float("nan")],
)
def test_threshold_rejects_non_numeric_non_finite_and_out_of_range(
    module, value
) -> None:
    from frisket.plugins.sdk import PluginUserError

    with pytest.raises(PluginUserError, match="0 to 100"):
        module._minimum_score(value)


@pytest.mark.parametrize("value, expected", [(0, 0.0), (75, 75.0), ("100", 100.0)])
def test_threshold_accepts_boundaries_and_numeric_text(module, value, expected) -> None:
    assert module._minimum_score(value) == expected


def test_structured_address_is_used_when_full_address_is_missing(module) -> None:
    company = _company()
    company["registered_address_in_full"] = None
    company["registered_address"] = {
        "street_address": "1 Main Street",
        "locality": "Dover",
        "region": "Delaware",
        "postal_code": "19901",
        "country": "United States",
    }
    result = module._company_result(
        company,
        method="exact_key",
        score=None,
        threshold=None,
        upstream_match=None,
    )
    assert result["oc_registered_address"] == (
        "1 Main Street, Dover, Delaware, 19901, United States"
    )
    assert set(result) == set(module.OUTPUT_NAMES)
