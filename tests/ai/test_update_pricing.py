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


def test_model_catalog_adds_current_aliases_without_snapshots_or_specialists() -> None:
    updater = _pricing_updater()
    previous = {
        "schema_version": "frisket.model_catalog.v1",
        "updated": "2026-10-12",
        "providers": {
            "openai": [{"id": "gpt-5", "label": "Reviewed GPT label", "vision": True}],
            "anthropic": [],
            "gemini": [],
            "openrouter": [],
            "ollama": [{"id": "qwen3:8b", "label": "Local Qwen", "vision": False}],
        },
    }
    litellm = {
        "gpt-5": {
            "mode": "chat",
            "litellm_provider": "openai",
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
            "supports_vision": True,
        },
        "gpt-6.1-sol": {
            "mode": "chat",
            "litellm_provider": "openai",
            "input_cost_per_token": 2e-6,
            "output_cost_per_token": 4e-6,
            "supported_modalities": ["text", "image"],
        },
        "gpt-6.1-sol-2026-10-01": {
            "mode": "chat",
            "litellm_provider": "openai",
            "input_cost_per_token": 2e-6,
            "output_cost_per_token": 4e-6,
            "supports_vision": True,
        },
        "gpt-6-search-api": {
            "mode": "chat",
            "litellm_provider": "openai",
            "input_cost_per_token": 2e-6,
            "output_cost_per_token": 4e-6,
            "supports_vision": True,
        },
        "gemini-3.9-flash": {
            "mode": "chat",
            "litellm_provider": "vertex_ai-language-models",
            "input_cost_per_token": 2e-6,
            "output_cost_per_token": 4e-6,
            "supports_vision": True,
        },
        "gemini/gemini-3.8-flash": {
            "mode": "chat",
            "litellm_provider": "gemini",
            "input_cost_per_token": 2e-6,
            "output_cost_per_token": 4e-6,
            "supports_vision": True,
        },
    }

    output = updater.build_model_catalog(
        litellm, {"data": []}, previous=previous, updated=date(2026, 10, 13)
    )

    assert output["schema_version"] == "frisket.model_catalog.v1"
    assert output["providers"]["openai"] == [
        {"id": "gpt-5", "label": "Reviewed GPT label", "vision": True},
        {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol", "vision": True},
    ]
    assert output["providers"]["ollama"] == previous["providers"]["ollama"]
    assert [entry["id"] for entry in output["providers"]["gemini"]] == [
        "gemini-3.8-flash"
    ]


def test_model_catalog_uses_openrouter_modality_and_bounded_families() -> None:
    updater = _pricing_updater()
    openrouter = {
        "data": [
            {
                "id": "qwen/qwen3-vl-8b-instruct",
                "name": "Qwen: Qwen3 VL 8B Instruct",
                "architecture": {"input_modalities": ["text", "image"]},
                "pricing": {"prompt": "0.1", "completion": "0.2"},
            },
            {
                "id": "qwen/qwen3-coder-30b",
                "name": "Qwen Coder",
                "architecture": {"input_modalities": ["text"]},
                "pricing": {"prompt": "0.1", "completion": "0.2"},
            },
            {
                "id": "unreviewed-vendor/everything-model",
                "name": "Everything",
                "architecture": {"input_modalities": ["text", "image"]},
                "pricing": {"prompt": "0.1", "completion": "0.2"},
            },
        ]
    }

    output = updater.build_model_catalog(
        {},
        openrouter,
        previous={"providers": {"ollama": []}},
        updated=date(2026, 10, 13),
    )

    assert output["providers"]["openrouter"] == [
        {
            "id": "qwen/qwen3-vl-8b-instruct",
            "label": "Qwen: Qwen3 VL 8B Instruct",
            "vision": True,
        }
    ]


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
    model_output_path = tmp_path / "model_catalog.json"
    model_output_path.write_text(
        json.dumps(
            {
                "schema_version": "frisket.model_catalog.v1",
                "updated": "2026-10-07",
                "providers": {
                    "anthropic": [
                        {
                            "id": "claude-haiku-4-5",
                            "label": "Claude Haiku 4.5",
                            "vision": True,
                        }
                    ],
                    "ollama": [],
                },
            }
        )
    )
    monkeypatch.setattr(updater, "MODEL_OUT", model_output_path)

    def fake_urlopen(target, timeout: int):
        url = target.full_url if hasattr(target, "full_url") else target
        if url == updater.LITELLM_SOURCE:
            return io.BytesIO(
                json.dumps(
                    {
                        "claude-haiku-4-5": {
                            "mode": "chat",
                            "litellm_provider": "anthropic",
                            "input_cost_per_token": 0.000002,
                            "output_cost_per_token": 0.000004,
                            "supports_vision": True,
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

    updater.main([])

    refreshed = json.loads(output_path.read_text())
    assert refreshed["text"]["claude-haiku-4-5"] == [2.0, 4.0]
    assert refreshed["opendocrouter"] == previous_opendocrouter
    warning = capsys.readouterr().err
    assert "WARNING" in warning
    assert "OpenDocRouter" in warning
    assert "preserving the previous complete snapshot" in warning


@pytest.fixture
def refresh_workspace(tmp_path, monkeypatch):
    updater = _pricing_updater()
    upstream_model = {
        "mode": "chat",
        "litellm_provider": "openai",
        "input_cost_per_token": 2e-6,
        "output_cost_per_token": 4e-6,
        "supports_vision": True,
    }
    litellm = {"gpt-5": upstream_model}
    router = {"data": []}
    document = {
        "price_version": "old",
        "data": [_opendocrouter_model("vendor/ocr", rate=1)],
    }
    catalog = updater.build_model_catalog(litellm, router, previous={"providers": {}})
    catalog["updated"] = "2026-01-01"
    prices = updater.build_price_table(
        litellm, router, catalog=catalog, opendocrouter=document
    )
    prices["updated"] = "2026-01-01"
    for name, value in (("MODEL_OUT", catalog), ("OUT", prices)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value))
        monkeypatch.setattr(updater, name, path)
    incoming = {
        "price_version": "new",
        "data": [_opendocrouter_model("vendor/ocr", rate=2)],
    }
    responses = {
        updater.LITELLM_SOURCE: litellm,
        updater.OPENROUTER_SOURCE: router,
        updater.OPENDOCROUTER_SOURCE: incoming,
    }

    def fetch(target, timeout):
        url = target.full_url if hasattr(target, "full_url") else target
        return io.BytesIO(json.dumps(responses[url]).encode())

    monkeypatch.setattr(updater.urllib.request, "urlopen", fetch)
    return updater, litellm, router, incoming


def test_default_price_refresh_keeps_approved_models_and_document_metadata(
    refresh_workspace,
):
    updater, litellm, _, document = refresh_workspace
    before = updater.MODEL_OUT.read_bytes()
    litellm["gpt-6.1-sol"] = dict(litellm["gpt-5"])
    litellm["gpt-5"]["input_cost_per_token"] = 3e-6
    document["data"][0].update(name="Renamed", version="2", max_sync_pages=20)
    document["data"].append(_opendocrouter_model("vendor/new", rate=3))

    updater.main([])

    assert updater.MODEL_OUT.read_bytes() == before
    prices = json.loads(updater.OUT.read_text())
    assert prices["text"] == {"gpt-5": [3, 4]}
    assert prices["opendocrouter"] == {
        "price_version": "new",
        "data": [_opendocrouter_model("vendor/ocr", rate=2)],
    }


def test_model_discovery_does_nothing_for_price_or_date_changes(refresh_workspace):
    updater, litellm, _, _ = refresh_workspace
    before = (updater.MODEL_OUT.read_bytes(), updater.OUT.read_bytes())
    litellm["gpt-5"]["input_cost_per_token"] = 9e-6
    updater.main(["--mode", "models"])
    assert (updater.MODEL_OUT.read_bytes(), updater.OUT.read_bytes()) == before


@pytest.mark.parametrize("change", ["llm", "ocr"])
def test_model_discovery_proposes_catalog_and_matching_prices(
    refresh_workspace, change
):
    updater, litellm, _, document = refresh_workspace
    if change == "llm":
        litellm["gpt-6.1-sol"] = dict(litellm["gpt-5"])
    else:
        document["data"] = [_opendocrouter_model("vendor/new", rate=3)]
    updater.main(["--mode", "models"])
    catalog = json.loads(updater.MODEL_OUT.read_text())
    prices = json.loads(updater.OUT.read_text())
    if change == "llm":
        assert catalog["providers"]["openai"][-1]["id"] == "gpt-6.1-sol"
        assert prices["text"]["gpt-6.1-sol"] == [2, 4]
    else:
        assert prices["opendocrouter"] == document


def test_pricing_preserves_complete_document_snapshot_when_approved_model_disappears(
    refresh_workspace,
):
    updater, _, _, document = refresh_workspace
    previous = json.loads(updater.OUT.read_text())["opendocrouter"]
    document["data"] = [_opendocrouter_model("vendor/new", rate=3)]
    updater.main(["--mode", "pricing"])
    assert json.loads(updater.OUT.read_text())["opendocrouter"] == previous


@pytest.mark.parametrize("rate", [-1, float("nan"), float("inf")])
def test_invalid_upstream_rate_does_not_partially_write_refresh(
    refresh_workspace, rate
):
    updater, litellm, _, _ = refresh_workspace
    before = (updater.MODEL_OUT.read_bytes(), updater.OUT.read_bytes())
    litellm["gpt-6.1-sol"] = {**litellm["gpt-5"], "input_cost_per_token": rate}
    with pytest.raises(ValueError, match="Invalid provider rate"):
        updater.main(["--mode", "models"])
    assert (updater.MODEL_OUT.read_bytes(), updater.OUT.read_bytes()) == before


def test_new_model_names_use_supplied_names_or_provider_version_formatting():
    updater = _pricing_updater()
    model = {
        "mode": "chat",
        "litellm_provider": "anthropic",
        "input_cost_per_token": 1e-6,
        "output_cost_per_token": 2e-6,
    }
    catalog = updater.build_model_catalog(
        {"claude-fable-5-1": model, "claude-haiku-5-5": model},
        {
            "data": [
                {
                    "id": "anthropic/claude-fable-5-1",
                    "name": "Anthropic: Claude Fable 5.1",
                }
            ]
        },
        previous={"providers": {}},
    )
    assert [row["label"] for row in catalog["providers"]["anthropic"]] == [
        "Claude Fable 5.1",
        "Claude Haiku 5.5",
    ]


def test_missing_output_rate_leaves_model_unknown_but_refreshes_other_rates(
    refresh_workspace,
):
    updater, litellm, _, _ = refresh_workspace
    del litellm["gpt-5"]["output_cost_per_token"]
    updater.main([])
    prices = json.loads(updater.OUT.read_text())
    assert "gpt-5" not in prices["text"]
    assert prices["opendocrouter"]["price_version"] == "new"
