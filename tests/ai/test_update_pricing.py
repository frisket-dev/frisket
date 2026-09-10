from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path


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
