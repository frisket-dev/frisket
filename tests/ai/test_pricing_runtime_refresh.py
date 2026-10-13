from __future__ import annotations

import json
import os

import pytest

from frisket.ai.llm import pricing
from frisket.ai.llm import pricing_refresh
from frisket.ai.llm.model_catalog import MODEL_CATALOG, MODEL_ENTRIES
from frisket.ai.llm.pricing_refresh import REFRESH_SECONDS, refresh_pricing_once


def _catalog(rate: float) -> bytes:
    return json.dumps({"text": {"fresh-model": [rate, rate * 2]}, "audio": {}}).encode()


def _models(model: str = "gpt-6.1-sol") -> bytes:
    return json.dumps(
        {
            "schema_version": "frisket.model_catalog.v1",
            "updated": "2026-10-13",
            "providers": {
                "anthropic": [],
                "openai": [{"id": model, "label": model, "vision": True}],
                "gemini": [],
                "openrouter": [],
                "ollama": [],
            },
        }
    ).encode()


@pytest.fixture(autouse=True)
def restore_catalog(monkeypatch):
    monkeypatch.setattr(pricing, "PRICES", pricing.PRICES)
    monkeypatch.setattr(pricing, "AUDIO_PRICES", pricing.AUDIO_PRICES)
    original = {
        "schema_version": "frisket.model_catalog.v1",
        "updated": "2026-10-13",
        "providers": {
            provider: [dict(entry) for entry in entries]
            for provider, entries in MODEL_ENTRIES.items()
        },
    }
    yield
    pricing_refresh.install_model_catalog(original)


def test_refresh_installs_and_caches_catalog_at_most_daily(tmp_path):
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return _models() if url == pricing_refresh.MODEL_CATALOG_URL else _catalog(1.5)

    assert refresh_pricing_once(cache_dir=tmp_path, now=100_000, fetch=fetch)
    assert pricing.PRICES["fresh-model"] == (1.5, 3.0)
    assert json.loads((tmp_path / "pricing_data.json").read_text())["text"]

    os.utime(tmp_path / "last_attempt", (100_000, 100_000))
    assert not refresh_pricing_once(
        cache_dir=tmp_path, now=100_000 + REFRESH_SECONDS - 1, fetch=fetch
    )
    assert len(calls) == 2


def test_refresh_installs_and_caches_model_catalog_with_pricing(tmp_path):
    def fetch(url: str) -> bytes:
        return (
            _models("gpt-6-luna")
            if url == pricing_refresh.MODEL_CATALOG_URL
            else _catalog(1.5)
        )

    assert refresh_pricing_once(cache_dir=tmp_path, now=100_000, fetch=fetch)
    assert MODEL_CATALOG["openai"] == ["gpt-6-luna"]
    assert json.loads((tmp_path / "model_catalog.json").read_text())["providers"]


def test_invalid_model_refresh_keeps_last_good_but_accepts_valid_pricing(tmp_path):
    before = MODEL_CATALOG["openai"]

    def fetch(url: str) -> bytes:
        if url == pricing_refresh.MODEL_CATALOG_URL:
            return b'{"schema_version":"future","providers":{}}'
        return _catalog(7.0)

    assert refresh_pricing_once(cache_dir=tmp_path, now=100_000, fetch=fetch)
    assert MODEL_CATALOG["openai"] == before
    assert pricing.PRICES["fresh-model"] == (7.0, 14.0)
    assert not (tmp_path / "model_catalog.json").exists()


def test_older_cached_model_catalog_does_not_override_newer_installed_data(tmp_path):
    installed = json.loads(_models("gpt-6.1-sol"))
    installed["updated"] = "2026-10-13"
    pricing_refresh.install_model_catalog(installed)
    cached = json.loads(_models("gpt-5"))
    cached["updated"] = "2026-10-12"
    (tmp_path / "model_catalog.json").write_text(json.dumps(cached))

    pricing_refresh._load_cached_catalogs(tmp_path)

    assert MODEL_CATALOG["openai"] == ["gpt-6.1-sol"]


def test_failed_refresh_keeps_current_catalog_and_throttles_retry(tmp_path):
    original = dict(pricing.PRICES)

    def fail(_url: str) -> bytes:
        raise OSError("offline")

    assert not refresh_pricing_once(cache_dir=tmp_path, now=200_000, fetch=fail)
    assert pricing.PRICES == original
    assert (tmp_path / "last_attempt").exists()


def test_invalid_refresh_does_not_replace_or_cache_catalog(tmp_path):
    original = dict(pricing.PRICES)
    assert not refresh_pricing_once(
        cache_dir=tmp_path,
        now=300_000,
        fetch=lambda _url: b'{"text": {"broken": [1]}, "audio": {}}',
    )
    assert pricing.PRICES == original
    assert not (tmp_path / "pricing_data.json").exists()


def test_start_loads_cached_catalog_and_starts_only_once(tmp_path, monkeypatch):
    root = tmp_path / "frisket" / "pricing"
    root.mkdir(parents=True)
    (root / "pricing_data.json").write_bytes(_catalog(2.0))
    starts: list[object] = []

    class Thread:
        def __init__(self, **kwargs):
            starts.append(kwargs)

        def start(self):
            return None

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(pricing_refresh, "_started", False)
    monkeypatch.setattr(pricing_refresh.threading, "Thread", Thread)

    pricing_refresh.start_pricing_refresh()
    pricing_refresh.start_pricing_refresh()

    assert pricing.PRICES["fresh-model"] == (2.0, 4.0)
    assert len(starts) == 1


def test_running_process_adopts_peer_cache_without_another_network_request(
    tmp_path, monkeypatch
):
    (tmp_path / "pricing_data.json").write_bytes(_catalog(2.0))
    attempt = tmp_path / "last_attempt"
    attempt.touch()
    os.utime(attempt, (100_000, 100_000))
    monkeypatch.setattr(pricing_refresh.time, "time", lambda: 100_001)
    monkeypatch.setattr(
        pricing_refresh.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("A peer already refreshed pricing"),
    )
    pauses = []

    class Finished(Exception):
        pass

    def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 1:
            assert pricing.model_pricing("fresh-model").price == (2.0, 4.0)
            (tmp_path / "pricing_data.json").write_bytes(_catalog(3.0))
        else:
            assert pricing.model_pricing("fresh-model").price == (3.0, 6.0)
            raise Finished

    monkeypatch.setattr(pricing_refresh.time, "sleep", pause)
    with pytest.raises(Finished):
        pricing_refresh._refresh_loop(tmp_path)
    assert all(0 < seconds <= 60 for seconds in pauses)
