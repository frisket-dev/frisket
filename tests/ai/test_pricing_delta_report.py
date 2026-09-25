"""Regression coverage for the pricing-refresh delta report."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPORTER = ROOT / "scripts" / "dev" / "pricing_delta_report.py"


def test_reporter_handles_audio_pricing_unit_transition(tmp_path: Path) -> None:
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

    result = subprocess.run(
        [sys.executable, str(REPORTER), str(before_path), str(after_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "audio:speech-model" in result.stdout
    assert "'input_per_token': 1.25e-06" in result.stdout
    assert "'per_second': 5e-05" in result.stdout
    assert "rate[1] moved 0.2 -> 0.5 (>50%)" in result.stdout
