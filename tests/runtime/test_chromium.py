"""Browser readiness uses Playwright metadata, never a browser launch."""

import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from frisket.runtime import chromium
from frisket.runtime.model_install import ModelInstallCancelled


@pytest.fixture(autouse=True)
def probe(monkeypatch, tmp_path):
    chromium._installation_paths.cache_clear()
    monkeypatch.setattr(chromium, "chromium_library_available", lambda: True)
    paths = [tmp_path / "chromium-current", tmp_path / "headless-current"]
    run = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout="\n".join(f"Install location: {path}" for path in paths),
        )
    )
    monkeypatch.setattr(chromium.subprocess, "run", run)
    yield paths, run
    chromium._installation_paths.cache_clear()


def complete(paths):
    for path in paths:
        path.mkdir(exist_ok=True)
        (path / "INSTALLATION_COMPLETE").touch()


def test_readiness_tracks_markers_without_caching_missing_browsers(probe):
    paths, run = probe
    assert not chromium.chromium_ready()
    complete(paths[:1])
    assert not chromium.chromium_ready()  # Full Chromium alone is insufficient.
    complete(paths[1:])
    assert chromium.chromium_ready()
    (paths[0] / "INSTALLATION_COMPLETE").unlink()
    assert not chromium.chromium_ready()
    assert run.call_count == 1
    assert run.call_args.args[0][-3:] == ["install", "--dry-run", "chromium"]
    assert run.call_args.kwargs["timeout"] == 10


def test_old_revision_does_not_count(probe, tmp_path):
    complete([tmp_path / "chromium-old"])
    assert not chromium.chromium_ready()


def test_cache_location_changes_invalidate_discovery(probe, monkeypatch):
    _, run = probe
    chromium.chromium_ready()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/new-cache")
    chromium.chromium_ready()
    assert run.call_count == 2
    assert run.call_args.kwargs["env"]["PLAYWRIGHT_BROWSERS_PATH"] == "/new-cache"


@pytest.mark.parametrize("failure", [OSError(), subprocess.TimeoutExpired("probe", 10)])
def test_probe_failure_can_be_retried(probe, failure):
    paths, run = probe
    success = run.return_value
    run.side_effect = [failure, success]
    assert not chromium.chromium_ready()
    complete(paths)
    assert chromium.chromium_ready()


def test_unavailable_library_does_not_spawn(probe, monkeypatch):
    _, run = probe
    monkeypatch.setattr(chromium, "chromium_library_available", lambda: False)
    assert not chromium.chromium_ready()
    with pytest.raises(RuntimeError, match="Playwright is unavailable"):
        chromium.install_chromium(should_cancel=lambda: False, progress=Mock())
    run.assert_not_called()


def test_installs_and_checks_completion(probe, monkeypatch):
    paths, _ = probe
    process = Mock(returncode=0)
    process.poll.side_effect = lambda: (complete(paths), 0)[1]
    spawn = Mock(return_value=process)
    stop = Mock()
    monkeypatch.setattr(chromium, "spawn_service", spawn)
    monkeypatch.setattr(chromium, "stop_service", stop)
    monkeypatch.setenv("SECRET_TOKEN", "must-not-leak")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example")
    progress = Mock()
    chromium.install_chromium(should_cancel=lambda: False, progress=progress)
    assert spawn.call_args.args[0][-4:] == ["-m", "playwright", "install", "chromium"]
    assert "SECRET_TOKEN" not in spawn.call_args.kwargs["env"]
    assert spawn.call_args.kwargs["env"]["HTTPS_PROXY"] == "https://proxy.example"
    stop.assert_called_once_with(process)
    assert progress.call_args.args == ("Chromium browser installed",)


@pytest.mark.parametrize("returncode", [0, 1])
def test_failed_or_incomplete_install_can_retry(probe, monkeypatch, returncode):
    process = Mock(returncode=returncode)
    process.poll.return_value = returncode
    monkeypatch.setattr(chromium, "spawn_service", Mock(return_value=process))
    monkeypatch.setattr(chromium, "stop_service", Mock())
    with pytest.raises(RuntimeError, match="Chromium installation"):
        chromium.install_chromium(should_cancel=lambda: False, progress=Mock())
    assert not chromium.chromium_ready()


def test_cancel_stops_installer(probe, monkeypatch):
    process = Mock()
    process.poll.return_value = None
    spawn = Mock(return_value=process)
    stop = Mock()
    monkeypatch.setattr(chromium, "spawn_service", spawn)
    monkeypatch.setattr(chromium, "stop_service", stop)
    checks = iter([False, True])
    with pytest.raises(ModelInstallCancelled):
        chromium.install_chromium(should_cancel=lambda: next(checks), progress=Mock())
    stop.assert_called_once_with(process)


def test_cancel_before_start_does_not_spawn(probe, monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(chromium, "spawn_service", spawn)
    with pytest.raises(ModelInstallCancelled):
        chromium.install_chromium(should_cancel=lambda: True, progress=Mock())
    spawn.assert_not_called()
