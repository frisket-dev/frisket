from __future__ import annotations

import json
import os

import pytest

from frisket.ai.llm import pricing
from frisket.ai.llm import pricing_refresh
from frisket.ai.llm.pricing_refresh import REFRESH_SECONDS, refresh_pricing_once


def _catalog(rate: float) -> bytes:
    return json.dumps({"text": {"fresh-model": [rate, rate * 2]}, "audio": {}}).encode()


@pytest.fixture(autouse=True)
def restore_catalog(monkeypatch):
    monkeypatch.setattr(pricing, "PRICES", pricing.PRICES)
    monkeypatch.setattr(pricing, "AUDIO_PRICES", pricing.AUDIO_PRICES)


def test_refresh_installs_and_caches_catalog_at_most_daily(tmp_path):
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return _catalog(1.5)

    assert refresh_pricing_once(cache_dir=tmp_path, now=100_000, fetch=fetch)
    assert pricing.PRICES["fresh-model"] == (1.5, 3.0)
    assert json.loads((tmp_path / "pricing_data.json").read_text())["text"]

    os.utime(tmp_path / "last_attempt", (100_000, 100_000))
    assert not refresh_pricing_once(
        cache_dir=tmp_path, now=100_000 + REFRESH_SECONDS - 1, fetch=fetch
    )
    assert len(calls) == 1


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
