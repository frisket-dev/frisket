from __future__ import annotations

import importlib.util
import io
import json
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


def _opendocrouter_model(model_id: str, *, rate: float) -> dict:
    return {
        "id": model_id,
        "name": model_id,
        "version": "1",
        "max_sync_pages": 50,
        "max_charge_per_page_usd": None,
        "avg_charge_per_page_usd": rate,
        "price_per_million_tokens": {
            "input": rate,
            "cached_input": rate,
            "output": rate,
        },
    }


@pytest.mark.parametrize(
    "opendocrouter_result",
    [
        OSError("provider unavailable"),
        json.dumps(
            {
                "price_version": "new-but-invalid",
                "data": [
                    _opendocrouter_model("vendor/new-model", rate=2.0),
                    {"id": "vendor/incomplete-model"},
                ],
            }
        ).encode(),
    ],
    ids=["fetch-failure", "invalid-whole-snapshot"],
)
def test_refresh_preserves_last_good_opendocrouter_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    opendocrouter_result: bytes | OSError,
) -> None:
    updater = _pricing_updater()
    previous_opendocrouter = {
        "price_version": "last-good",
        "data": [_opendocrouter_model("vendor/previous-model", rate=1.0)],
    }
    output_path = tmp_path / "pricing_data.json"
    output_path.write_text(
        json.dumps(
            {
                "sources": {"opendocrouter": updater.OPENDOCROUTER_SOURCE},
                "updated": "2026-10-07",
                "text": {"model-a": [1.0, 1.0]},
                "audio": {},
                "opendocrouter": previous_opendocrouter,
            }
        )
    )
    monkeypatch.setattr(updater, "OUT", output_path)

    def fake_urlopen(target, timeout: int):
        url = target.full_url if hasattr(target, "full_url") else target
        if url == updater.LITELLM_SOURCE:
            return io.BytesIO(
                json.dumps(
                    {
                        "claude-haiku-4-5": {
                            "input_cost_per_token": 0.000002,
                            "output_cost_per_token": 0.000004,
                        }
                    }
                ).encode()
            )
        if url == updater.OPENROUTER_SOURCE:
            return io.BytesIO(b'{"data": []}')
        assert url == updater.OPENDOCROUTER_SOURCE
        if isinstance(opendocrouter_result, OSError):
            raise opendocrouter_result
        return io.BytesIO(opendocrouter_result)

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)

    updater.main()

    refreshed = json.loads(output_path.read_text())
    assert refreshed["text"]["claude-haiku-4-5"] == [2.0, 4.0]
    assert refreshed["opendocrouter"] == previous_opendocrouter
    warning = capsys.readouterr().err
    assert "WARNING" in warning
    assert "OpenDocRouter" in warning
    assert "preserving the previous complete snapshot" in warning
