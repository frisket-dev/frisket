"""The published pricing document updates runnable document models, not just labels."""

import copy
import json
import runpy
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from frisket.ai.llm import pricing
from frisket.ai.llm.pricing_refresh import refresh_pricing_once, _load_cached_pricing
from frisket.opendocrouter_catalog import current_catalog, catalog_document, find_model
from frisket.actions.markdown import ToMarkdownParams
from frisket.actions.media_options import OcrOptions
from frisket.execution.definitions import StaticExecutionTargetProvider
from frisket.execution.price_book import OperatorBorne, quote_ocr, live_cost_fact
from frisket.execution.promise_compiler import ProviderUsageCost


@pytest.fixture
def document():
    original = {
        "text": {k: list(v) for k, v in pricing.PRICES.items()},
        "audio": copy.deepcopy(pricing.AUDIO_PRICES),
        "opendocrouter": catalog_document(current_catalog()),
    }
    doc = copy.deepcopy(original)
    yield doc
    pricing.install_pricing_data(original)


def future_model(document):
    row = copy.deepcopy(document["opendocrouter"]["data"][0])
    row.update(
        id="example/new-parser", name="New parser", max_charge_per_page_usd=0.125
    )
    document["opendocrouter"]["data"].append(row)
    return "opendocrouter/" + row["id"]


def test_daily_refresh_updates_existing_provider_and_cached_restart(document, tmp_path):
    provider = StaticExecutionTargetProvider(env={"OPEN_DOC_ROUTER_API_KEY": "test"})
    engine = future_model(document)
    fetched = []

    def fetch(url):
        fetched.append(url)
        return json.dumps(document).encode()

    assert refresh_pricing_once(cache_dir=tmp_path, now=100000, fetch=fetch)
    assert find_model(engine).name == "New parser"
    assert OcrOptions(searchable_pdf=True).normalize(engine)["searchable_pdf"]
    assert (
        ToMarkdownParams.model_validate(
            {"source": "document", "engine": engine}
        ).engine.root
        == engine
    )
    odr = next(t for t in provider.targets() if t.id == "opendocrouter")
    assert {(s.capability, s.transport) for s in odr.engines if s.engine == engine} == {
        ("ocr", "opendocrouter.parse"),
        ("document.convert", "opendocrouter.parse"),
    }
    quote = quote_ocr(
        target_id="opendocrouter",
        engine=engine,
        funding=OperatorBorne(),
        offering=None,
        pages=3,
    )
    assert quote.bound == Decimal("0.375")
    assert quote.charge_authority == "provider_usage"
    assert not refresh_pricing_once(cache_dir=tmp_path, now=100001, fetch=fetch)
    assert len(fetched) == 1
    _load_cached_pricing(tmp_path)
    assert find_model(engine).name == "New parser"


@pytest.mark.parametrize("fault", ["negative_price", "bad_id", "duplicate", "empty"])
def test_invalid_refresh_preserves_installed_and_disk_catalog(
    document, tmp_path, fault
):
    valid = copy.deepcopy(document)
    assert refresh_pricing_once(
        cache_dir=tmp_path, now=100000, fetch=lambda _: json.dumps(valid).encode()
    )
    old = current_catalog()
    row = document["opendocrouter"]["data"][0]
    if fault == "negative_price":
        row["max_charge_per_page_usd"] = -1
    elif fault == "bad_id":
        row["id"] = "https://bad.example/model"
    elif fault == "duplicate":
        document["opendocrouter"]["data"].append(copy.deepcopy(row))
    else:
        document["opendocrouter"]["data"] = []
    assert not refresh_pricing_once(
        cache_dir=tmp_path, now=200000, fetch=lambda _: json.dumps(document).encode()
    )
    assert current_catalog() == old
    assert json.loads((tmp_path / "pricing_data.json").read_text()) == valid


def test_missing_price_is_unknown_and_catalog_removal_stops_new_admission(document):
    engine = future_model(document)
    document["opendocrouter"]["data"][-1]["max_charge_per_page_usd"] = None
    pricing.install_pricing_data(document)
    assert isinstance(
        quote_ocr(
            target_id="opendocrouter",
            engine=engine,
            funding=OperatorBorne(),
            offering=None,
            pages=1,
        ),
        ProviderUsageCost,
    )
    document["opendocrouter"]["data"].pop()
    pricing.install_pricing_data(document)
    assert find_model(engine) is None
    with pytest.raises(ValueError):
        ToMarkdownParams.model_validate({"source": "document", "engine": engine})


def test_quote_and_live_fence_observe_same_refreshed_rate(document):
    engine = future_model(document)
    pricing.install_pricing_data(document)
    quote = quote_ocr(
        target_id="opendocrouter",
        engine=engine,
        funding=OperatorBorne(),
        offering=None,
        pages=1,
    )

    def live():
        return live_cost_fact(
            capability="ocr",
            target_id="opendocrouter",
            engine=engine,
            funding=OperatorBorne(),
            offering=None,
            hardware_class=None,
        )

    assert live()["unit_rate"] == quote.unit_rate
    document["opendocrouter"]["data"][-1]["max_charge_per_page_usd"] = 0.25
    pricing.install_pricing_data(document)
    assert live()["unit_rate"] == "0.25"
    assert quote.unit_rate == "0.125"


def test_updater_keeps_model_metadata_and_delta_reports_it(
    document, tmp_path, monkeypatch, capsys
):
    from tests.ai.test_update_pricing import _pricing_updater

    updater = _pricing_updater()
    output = updater.build_price_table(
        {},
        {"data": []},
        catalog={"providers": {}},
        opendocrouter=document["opendocrouter"],
    )
    assert output["opendocrouter"] == document["opendocrouter"]
    assert (
        output["sources"]["opendocrouter"] == "https://www.opendocrouter.ai/v1/models"
    )
    changed = copy.deepcopy(output)
    changed["opendocrouter"]["data"][0]["name"] = "Renamed parser"
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    before.write_text(json.dumps(output))
    after.write_text(json.dumps(changed))
    reporter_path = Path(__file__).parents[2] / "scripts/dev/pricing_delta_report.py"
    monkeypatch.setattr(sys, "argv", [str(reporter_path), str(before), str(after)])
    assert runpy.run_path(str(reporter_path))["main"]() == 0
    assert "Renamed parser" in capsys.readouterr().out
