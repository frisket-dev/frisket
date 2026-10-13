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
