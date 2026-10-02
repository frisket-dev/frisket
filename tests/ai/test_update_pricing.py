from __future__ import annotations

import importlib.util
import io
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _pricing_updater():
    path = ROOT / "scripts" / "dev" / "update_pricing.py"
    spec = importlib.util.spec_from_file_location("frisket_update_pricing", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openrouter_catalog_rate_comes_from_openrouter_source() -> None:
    updater = _pricing_updater()
    catalog = {
        "providers": {
            "openrouter": [{"id": "qwen/qwen3-8b", "label": "Qwen"}],
            "ollama": [{"id": "qwen3:8b", "label": "Qwen local"}],
        }
    }

    output = updater.build_price_table(
        {},
        {
            "data": [
                {
                    "id": "qwen/qwen3-8b",
                    "pricing": {"prompt": "0.000000117", "completion": "0.000000455"},
                }
            ]
        },
        catalog=catalog,
        updated=date(2026, 8, 29),
    )

    assert output["text"] == {"qwen/qwen3-8b": [0.117, 0.455]}
    assert output["updated"] == "2026-08-29"
    assert output["sources"]["openrouter"] == "https://openrouter.ai/api/v1/models"


def test_maintained_mai_audio_rate_survives_an_openrouter_refresh() -> None:
    updater = _pricing_updater()

    output = updater.build_price_table(
        {}, {"data": []}, catalog={"providers": {}}, updated=date(2026, 9, 6)
    )

    assert output["audio"]["microsoft/mai-transcribe-2"] == {"per_second": 0.1 / 3600}


def test_token_billed_audio_model_prefers_returned_usage_units() -> None:
    updater = _pricing_updater()
    litellm = {
        "gpt-4o-transcribe": {
            "mode": "audio_transcription",
            "input_cost_per_second": 0.0001,
            "input_cost_per_token": 2.5e-6,
            "output_cost_per_token": 1e-5,
        }
    }

    output = updater.build_price_table(
        litellm, {"data": []}, catalog={"providers": {}}, updated=date(2026, 9, 25)
    )

    assert output["audio"]["gpt-4o-transcribe"] == {
        "input_per_token": 2.5e-6,
        "output_per_token": 1e-5,
    }


@pytest.mark.parametrize("rate", [0, 0.000001, 999999999])
def test_price_validation_accepts_any_nonnegative_magnitude(rate):
    _pricing_updater().validate_price_table(
        {"text": {"model": [rate, rate]}, "audio": {"speech": {"per_second": rate}}}
    )


@pytest.mark.parametrize("rate", [-1, float("nan"), float("inf"), True, "1.2"])
@pytest.mark.parametrize("section", ["text", "audio"])
def test_price_validation_rejects_invalid_rates(rate, section):
    data = {"text": {"model": [1, 2]}, "audio": {"speech": {"per_second": 0.01}}}
    if section == "text":
        data["text"]["model"] = [rate, 2]
    else:
        data["audio"]["speech"] = {"per_second": rate}
    with pytest.raises(ValueError):
        _pricing_updater().validate_price_table(data)


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"text": {}, "audio": {"speech": {"per_second": 0.01}}},
        {"text": {"model": [1, 2]}},
        {"text": {"model": [1]}, "audio": {}},
        {"text": {"model": [1, 2]}, "audio": {"speech": {"unknown_unit": 2}}},
    ],
)
def test_price_validation_rejects_broken_or_empty_catalog(data):
    with pytest.raises(ValueError):
        _pricing_updater().validate_price_table(data)


def test_failed_validation_does_not_overwrite_price_table(tmp_path, monkeypatch):
    updater = _pricing_updater()
    target = tmp_path / "prices.json"
    target.write_text("existing prices")
    monkeypatch.setattr(updater, "OUT", target)
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"{}")
    )
    monkeypatch.setattr(
        updater, "build_price_table", lambda *a: {"text": {}, "audio": {}}
    )
    with pytest.raises(ValueError, match="nonempty"):
        updater.main()
    assert target.read_text() == "existing prices"
