"""Regression coverage for the pricing-refresh delta report."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REPORTER = ROOT / "scripts" / "dev" / "pricing_delta_report.py"


def test_reporter_handles_audio_pricing_unit_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    before = {
        "text": {"provider/model": [0.1, 0.2]},
        "audio": {
            "speech-model": {
                "input_per_token": 1.25e-6,
                "output_per_token": 5e-6,
            }
        },
    }
    after = {
        "text": {"provider/model": [0.1, 0.5]},
        "audio": {"speech-model": {"per_second": 5e-5}},
    }
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    before_path.write_text(json.dumps(before), encoding="utf-8")
    after_path.write_text(json.dumps(after), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", [str(REPORTER), str(before_path), str(after_path)])
    reporter = runpy.run_path(str(REPORTER))
    assert reporter["main"]() == 0
    output = capsys.readouterr().out

    assert "audio:speech-model" in output
    assert "'input_per_token': 1.25e-06" in output
    assert "'per_second': 5e-05" in output
    assert "rate[1] moved 0.2 -> 0.5 (>50%)" in output


def test_reporter_includes_model_additions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pricing = {"text": {}, "audio": {}}
    old_models = {"providers": {"openai": []}}
    new_models = {
        "providers": {
            "openai": [{"id": "gpt-6.1-sol", "label": "GPT 6.1 Sol", "vision": True}]
        }
    }
    paths = [tmp_path / name for name in ("p-old", "p-new", "m-old", "m-new")]
    for path, payload in zip(
        paths, (pricing, pricing, old_models, new_models), strict=True
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", [str(REPORTER), *(str(path) for path in paths)])
    reporter = runpy.run_path(str(REPORTER))
    assert reporter["main"]() == 0

    assert "model:openai/gpt-6.1-sol" in capsys.readouterr().out


def test_reporter_separates_document_model_metadata_from_rates(
    tmp_path, monkeypatch, capsys
):
    old = {"opendocrouter": {"price_version": "1", "data": []}}
    new = {
        "opendocrouter": {
            "price_version": "1",
            "data": [
                {
                    "id": "vendor/ocr",
                    "name": "OCR",
                    "version": "2",
                    "max_sync_pages": 10,
                    "max_charge_per_page_usd": 0.01,
                    "avg_charge_per_page_usd": 0.005,
                    "price_per_million_tokens": {
                        "input": 1,
                        "cached_input": 0.5,
                        "output": 2,
                    },
                }
            ],
        }
    }
    paths = [tmp_path / "old", tmp_path / "new"]
    for path, payload in zip(paths, (old, new), strict=True):
        path.write_text(json.dumps(payload))
    monkeypatch.setattr(sys, "argv", [str(REPORTER), *(str(path) for path in paths)])
    assert runpy.run_path(str(REPORTER))["main"]() == 0
    output = capsys.readouterr().out
    assert "model:opendocrouter/vendor/ocr**: NEW" in output
    assert "'max_sync_pages': 10" in output
    assert "opendocrouter:vendor/ocr**: NEW" in output


def test_reporter_shows_reordering_and_retired_price_swings(
    tmp_path, monkeypatch, capsys
):
    model = {
        "id": "vendor/old",
        "name": "Retired OCR",
        "version": "1",
        "max_sync_pages": 10,
        "max_charge_per_page_usd": 0.01,
        "avg_charge_per_page_usd": 0.005,
        "price_per_million_tokens": {"input": 1, "cached_input": 0.5, "output": 2},
    }
    prices = {
        "opendocrouter": {"price_version": "1", "data": [], "retired_data": [model]}
    }
    models = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
    paths = [tmp_path / name for name in ("p-old", "p-new", "m-old", "m-new")]
    paths[0].write_text(json.dumps(prices))
    model["max_charge_per_page_usd"] = 0.03
    paths[1].write_text(json.dumps(prices))
    paths[2].write_text(json.dumps({"providers": {"openai": models}}))
    paths[3].write_text(json.dumps({"providers": {"openai": list(reversed(models))}}))
    monkeypatch.setattr(sys, "argv", [str(REPORTER), *(str(path) for path in paths)])
    assert runpy.run_path(str(REPORTER))["main"]() == 0
    output = capsys.readouterr().out
    assert "model:openai/a" in output
    assert "'position': 1" in output and "'position': 2" in output
    assert "max_per_page moved 0.01 -> 0.03 (>50%)" in output
