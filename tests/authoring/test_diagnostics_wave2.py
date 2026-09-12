"""Diagnostics-in-Settings contract: probes added to
frisket.diagnostics for per-provider key validation, replay-vs-live mode,
worker heartbeat / queue staleness, free disk, and plugin runtime health,
including the stable "N installed, M active, K failed (names)" summary.

Each probe follows the existing idiom pinned by tests/test_error_diagnose.py
and tests/test_diagnose_live_router.py: returns a small structured dict,
never raises, and is wired into run_diagnostics()'s ``info`` section via
``_info_probe`` so one broken probe can never 500 the whole report.

This file also covers fixes to the first cut:
provider key validation now sends the real per-provider auth header (it
used to send none), replay-mode reporting now tells the truth about a
cache MISS falling through to a live call, and plugin_health now counts
truthfully (excludes uninstalled entries, uses the runtime registry signal
for "active") and keeps its stable 6-field shape even when the index read
fails.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket.operability import diagnostics
from frisket.engine.jobs import open_queue
from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter, ResponseCache
from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.authoring.workbench.plugin_runtime import (
    ensure_workspace_plugin_packages,
    uninstall_workbench_plugin,
)
from frisket.authoring.workbench.plugin_runtime_status import (
    workbench_plugin_runtime_index,
)


# ---------------------------------------------------------------------------
# replay_mode_report


def test_replay_mode_report_replay_allows_live_calls_on_a_miss():
    """'replay' does NOT mean 'no live calls' -- a
    cache miss falls through to a live call (frisket.llm.cache's own module
    docstring: 'replay -- hit returns cached; miss does live call then
    stores')."""
    cache_mode_router = ModelRouter(cache_mode="replay")  # no cache attached
    r = diagnostics.replay_mode_report(cache_mode_router)
    assert r["cache_mode"] == "replay"
    assert r["live_calls_possible"] is True


def test_replay_mode_report_replay_with_cache_describes_hit_vs_miss(tmp_path):
    cache = ResponseCache(tmp_path / "c.db")
    router = ModelRouter(cache=cache, cache_mode="replay")
    r = diagnostics.replay_mode_report(router)
    assert r["cache_configured"] is True
    assert r["live_calls_possible"] is True
    assert "miss" in r["summary"].lower()
    assert "falls through" in r["summary"]


def test_replay_mode_report_replay_strict_with_cache_blocks_live_calls(tmp_path):
    """The ONE case that genuinely blocks a live call: replay_strict with a
    real cache attached raises CacheMiss instead of calling out."""
    cache = ResponseCache(tmp_path / "c.db")
    router = ModelRouter(cache=cache, cache_mode="replay_strict")
    r = diagnostics.replay_mode_report(router)
    assert r["live_calls_possible"] is False
    assert "replay_strict" in r["summary"]


def test_replay_mode_report_replay_strict_without_cache_allows_live_calls():
    """Edge case: an unconfigured cache skips the mode branch entirely in
    _complete_transport, so even replay_strict falls through to a live call
    when there's no ResponseCache to check."""
    router = ModelRouter(cache=None, cache_mode="replay_strict")
    r = diagnostics.replay_mode_report(router)
    assert r["cache_configured"] is False
    assert r["live_calls_possible"] is True


def test_replay_mode_report_fresh_allows_live_calls():
    r = diagnostics.replay_mode_report(ModelRouter(cache_mode="fresh"))
    assert r["cache_mode"] == "fresh"
    assert r["live_calls_possible"] is True


class _FakeLiveAdapter:
    """Same test-double pattern as tests/test_action_confirmation_contract.py:
    an injected adapter standing in for a real provider call."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.calls += 1
        return LLMResponse(
            content="live-response",
            data=None,
            tokens_in=1,
            tokens_out=1,
            cost=0.0,
            model=req.model,
        )


def test_replay_mode_report_a_cache_miss_actually_reaches_a_live_provider(tmp_path):
    """Proves the router really does what the corrected report claims --
    not just that the report SAYS live calls are possible."""
    cache = ResponseCache(tmp_path / "c.db")
    router = ModelRouter(keys={"anthropic": "k"}, cache=cache, cache_mode="replay")
    adapter = _FakeLiveAdapter()
    router._adapters["anthropic"] = adapter  # noqa: SLF001

    report = diagnostics.replay_mode_report(router)
    assert report["live_calls_possible"] is True

    req = LLMRequest(
        model="anthropic/claude-haiku-4-5",
        messages=[{"role": "user", "content": "hi"}],
    )
    resp = asyncio.run(router.complete(req))
    assert resp.content == "live-response"
    assert adapter.calls == 1  # the miss really did fall through to a live call


# ---------------------------------------------------------------------------
# provider_key_validation_report -- authenticated


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_provider_key_validation_report_no_keys_is_a_quiet_skip():
    r = diagnostics.provider_key_validation_report(ModelRouter(use_env_keys=False))
    assert r["providers"] == {}
    assert "no keyed providers" in r["summary"]


def test_provider_key_validation_report_authenticated_accept_sends_the_real_key(
    monkeypatch,
):
    """The pre-fix probe sent a bare
    GET with no Authorization header at all, so a VALID key reported 'key
    rejected'. This proves the real key is actually sent, AND that a 200
    response is correctly read as valid."""
    captured: list[tuple[str, dict | None]] = []

    def fake_get(self, url, *, headers=None, timeout=None):  # noqa: ANN001
        captured.append((url, headers))
        return _FakeResponse(200)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    sentinel_key = "sk-SENTINEL-DO-NOT-LEAK"
    router = ModelRouter(keys={"openai": sentinel_key}, use_env_keys=False)

    r = diagnostics.provider_key_validation_report(router)

    assert r["providers"]["openai"] == {
        "valid": True,
        "reachable": True,
        "status": 200,
    }
    assert "openai" in r["summary"] and "accepted" in r["summary"]
    assert captured, "the probe never actually sent a request"
    _url, headers = captured[0]
    assert headers is not None
    assert headers.get("Authorization") == f"Bearer {sentinel_key}"
    # sentinel-key non-echo: the key must never appear anywhere in the report
    assert sentinel_key not in json.dumps(r)


def test_provider_key_validation_report_anthropic_uses_x_api_key_header(monkeypatch):
    captured: list[dict | None] = []

    def fake_get(self, url, *, headers=None, timeout=None):  # noqa: ANN001
        captured.append(headers)
        return _FakeResponse(200)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    sentinel_key = "sk-ant-SENTINEL-DO-NOT-LEAK"
    router = ModelRouter(keys={"anthropic": sentinel_key}, use_env_keys=False)

    r = diagnostics.provider_key_validation_report(router)

    assert captured[0] is not None
    assert captured[0]["x-api-key"] == sentinel_key
    assert "Authorization" not in captured[0]
    assert sentinel_key not in json.dumps(r)


def test_provider_key_validation_report_authenticated_reject(monkeypatch):
    def fake_get(self, url, *, headers=None, timeout=None):  # noqa: ANN001
        return _FakeResponse(401)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    sentinel_key = "sk-SENTINEL-BAD-KEY"
    router = ModelRouter(keys={"openai": sentinel_key}, use_env_keys=False)

    r = diagnostics.provider_key_validation_report(router)

    assert r["providers"]["openai"] == {
        "valid": False,
        "reachable": True,
        "status": 401,
    }
    assert "key rejected: openai" in r["summary"]
    assert sentinel_key not in json.dumps(r)


def test_provider_key_validation_report_rate_limit_is_indeterminate_not_accepted(
    monkeypatch,
):
    """The pre-fix logic marked every
    >=400 status invalid ('rejected') OR let a non-401/403 >=400 summarize
    as accepted, depending on which branch ran -- either way wrong. A 429
    means the host answered but says NOTHING about key validity."""

    def fake_get(self, url, *, headers=None, timeout=None):  # noqa: ANN001
        return _FakeResponse(429)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    router = ModelRouter(keys={"openai": "sk-whatever"}, use_env_keys=False)

    r = diagnostics.provider_key_validation_report(router)

    assert r["providers"]["openai"]["valid"] is None
    assert "accepted" not in r["summary"]
    assert "rejected" not in r["summary"]
    assert "indeterminate" in r["summary"]


def test_provider_key_validation_report_network_failure_is_indeterminate_never_raises(
    monkeypatch,
):
    def fake_get(self, url, *, headers=None, timeout=None):  # noqa: ANN001
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    router = ModelRouter(keys={"openai": "sk-whatever"}, use_env_keys=False)

    r = diagnostics.provider_key_validation_report(router)

    assert r["providers"]["openai"]["valid"] is None
    assert r["providers"]["openai"]["reachable"] is False
    assert "indeterminate" in r["summary"]


def test_provider_key_validation_report_never_raises_on_unexpected_error(monkeypatch):
    """INFO probes never crash the caller, even for a bug in the probe
    itself, not just a network failure."""

    def _boom(provider, key, *, client=None, timeout=6.0):  # noqa: ANN001
        raise RuntimeError("unexpected probe bug")

    monkeypatch.setattr("frisket.server.provider_config.probe_provider", _boom)
    router = ModelRouter(keys={"openai": "sk-x"}, use_env_keys=False)

    r = diagnostics.provider_key_validation_report(router)
    assert r["providers"] == {}
    assert "unavailable" in r["summary"]


# ---------------------------------------------------------------------------
# queue_health_report


def test_queue_health_report_no_queue_is_a_quiet_skip():
    r = diagnostics.queue_health_report(None)
    assert r["configured"] is False
    assert "no run queue" in r["summary"]


def test_queue_health_report_wraps_queue_health_payload(tmp_path):
    queue = open_queue(workspace=tmp_path)
    r = diagnostics.queue_health_report(queue, liveness_window_seconds=90.0)
    assert r["configured"] is True
    assert r["schema_version"] == "frisket.queue_health.v1"
    # same shape queue_health_payload publishes at /api/health
    assert "workers" in r and "queued" in r and "jobs" in r


def test_queue_health_report_flags_queued_with_no_live_worker(tmp_path):
    queue = open_queue(workspace=tmp_path)
    queue.enqueue("echo", {"x": 1})
    r = diagnostics.queue_health_report(queue, liveness_window_seconds=90.0)
    assert r["queued"]["count"] == 1
    assert r["queued"]["no_live_worker"] is True
    assert "NO live worker" in r["summary"]


# ---------------------------------------------------------------------------
# disk_free_report


def test_disk_free_report_reports_real_free_space(tmp_path):
    r = diagnostics.disk_free_report(tmp_path)
    assert r["free_gb"] >= 0
    assert r["total_gb"] > 0
    assert str(tmp_path) in r["path"]
    assert "GB free" in r["summary"]


def test_disk_free_report_defaults_to_cwd_when_no_path_given():
    r = diagnostics.disk_free_report(None)
    assert r["path"] == str(Path.cwd())


# ---------------------------------------------------------------------------
# plugin_health_report -- corrected counting + stable shape, per cross-model
# review
#
# tests/conftest.py's autouse `hermetic_bundled_plugins_root` fixture pins
# the bundled-plugins root to an EMPTY dir for the whole suite (so a fresh
# project has no plugins in every other test); the tests below need the
# REAL shipped tree to prove bootstrap/count behavior, so they opt out with
# `@pytest.mark.real_bundled_plugins` (same marker
# tests/test_workbench_plugin_geo_bundled.py uses) and reset the
# process-global plugin registry so `registryActivated` reflects THIS test's
# own bootstrap, not stale state left by an earlier real-bundled-plugins
# test.


@pytest.fixture()
def _real_bundled_plugin_registry():
    from frisket.authoring.plugin_registry import _reset_default_registry_for_tests

    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


def test_plugin_health_report_no_project_is_a_quiet_skip():
    r = diagnostics.plugin_health_report(None, project_id=None)
    assert r["available"] is False
    assert r["installed"] == 0 and r["active"] == 0 and r["failed"] == 0
    assert r["failed_names"] == []
    assert "no project in scope" in r["summary"]


@pytest.mark.real_bundled_plugins
def test_plugin_health_report_bootstraps_bundled_plugins_on_a_fresh_project(
    tmp_path, _real_bundled_plugin_registry
):
    """The probe must go through the SAME bootstrap seam
    server/services/workbench.py's WorkbenchService.plugin_index uses, not
    read the (possibly never-bootstrapped) index directly."""
    ensure_workspace_plugin_packages(tmp_path)
    project = Project.create(tmp_path / "p.frisket")
    try:
        r = diagnostics.plugin_health_report(project, project_id="p")
        assert r["available"] is True
        assert r["installed"] > 0  # bundled first-party plugins auto-install
        assert r["failed"] == 0
        assert r["summary"] == (
            f"{r['installed']} installed, {r['active']} active, 0 failed"
        )
    finally:
        project.close()


@pytest.mark.real_bundled_plugins
def test_plugin_health_report_installed_excludes_uninstalled_plugins(
    tmp_path, _real_bundled_plugin_registry
):
    """Uninstalling 1 of N plugins must
    actually decrement 'installed' -- the runtime index keeps a ledger row
    for the uninstalled plugin (installState == 'uninstalled'), and the
    pre-fix probe counted every row via len(plugins)."""
    ensure_workspace_plugin_packages(tmp_path)
    project = Project.create(tmp_path / "p.frisket")
    try:
        before = diagnostics.plugin_health_report(project, project_id="p")
        assert before["installed"] > 0
        first_plugin_id = workbench_plugin_runtime_index(project, project_id="p")[
            "plugins"
        ][0]["pluginId"]

        uninstall_workbench_plugin(
            project,
            project_id="p",
            plugin_id=first_plugin_id,
            workspace_projects=(project,),
        )

        after = diagnostics.plugin_health_report(project, project_id="p")
        assert after["installed"] == before["installed"] - 1
        assert first_plugin_id not in after["failed_names"]
    finally:
        project.close()


def test_plugin_health_report_active_uses_registry_activated_not_install_state(
    monkeypatch, tmp_path
):
    """Post-restart, a plugin persisted as
    installState 'enabled' has NO in-memory registration yet
    (registryActivated False) until it's re-activated -- 'active' must
    reflect the runtime signal, not the persisted field."""
    project = Project.create(tmp_path / "p.frisket")
    try:
        fake_index = {
            "plugins": [
                {
                    "pluginId": "persisted-enabled-not-registered",
                    "installState": "enabled",
                    "registryActivated": False,
                },
                {
                    "pluginId": "really-active",
                    "installState": "enabled",
                    "registryActivated": True,
                },
                {
                    "pluginId": "gone",
                    "installState": "uninstalled",
                    "registryActivated": False,
                },
            ]
        }
        monkeypatch.setattr(
            "frisket.authoring.workbench.plugin_runtime.bootstrap_project_bundled_plugins",
            lambda *a, **k: [],
        )
        monkeypatch.setattr(
            "frisket.authoring.workbench.plugin_runtime_status.workbench_plugin_runtime_index",
            lambda *a, **k: fake_index,
        )
        r = diagnostics.plugin_health_report(project, project_id="p")
        assert r["installed"] == 2  # "gone" (uninstalled) excluded
        assert r["active"] == 1  # only "really-active" has registryActivated
    finally:
        project.close()


def test_plugin_health_report_failed_names_formatting(monkeypatch, tmp_path):
    project = Project.create(tmp_path / "p.frisket")
    try:
        fake_index = {
            "plugins": [
                {
                    "pluginId": "broken-one",
                    "installState": "failed",
                    "registryActivated": False,
                },
                {
                    "pluginId": "ok-one",
                    "installState": "enabled",
                    "registryActivated": True,
                },
            ]
        }
        monkeypatch.setattr(
            "frisket.authoring.workbench.plugin_runtime.bootstrap_project_bundled_plugins",
            lambda *a, **k: [],
        )
        monkeypatch.setattr(
            "frisket.authoring.workbench.plugin_runtime_status.workbench_plugin_runtime_index",
            lambda *a, **k: fake_index,
        )
        r = diagnostics.plugin_health_report(project, project_id="p")
        assert r["failed"] == 1
        assert r["failed_names"] == ["broken-one"]
        assert r["summary"] == "2 installed, 1 active, 1 failed (broken-one)"
    finally:
        project.close()


def test_plugin_health_report_keeps_stable_shape_on_index_exception(
    monkeypatch, tmp_path
):
    """_info_probe's generic wrapper collapses a raised
    exception to {ok, error, summary}, dropping 'available'/counts/
    failed_names -- consumers depend on the SAME 6-field shape in the error
    case as the success case, so plugin_health_report must guarantee it
    itself, not rely on the generic wrapper."""
    project = Project.create(tmp_path / "p.frisket")
    try:
        monkeypatch.setattr(
            "frisket.authoring.workbench.plugin_runtime.bootstrap_project_bundled_plugins",
            lambda *a, **k: [],
        )

        def _boom(*a, **k):
            raise RuntimeError("index blew up")

        monkeypatch.setattr(
            "frisket.authoring.workbench.plugin_runtime_status.workbench_plugin_runtime_index",
            _boom,
        )
        r = diagnostics.plugin_health_report(project, project_id="p")
        assert r == {
            "available": False,
            "installed": 0,
            "active": 0,
            "failed": 0,
            "failed_names": [],
            "summary": "plugin health unavailable (index blew up)",
        }
    finally:
        project.close()


def test_plugin_health_report_keeps_stable_shape_when_bootstrap_raises(
    monkeypatch, tmp_path
):
    project = Project.create(tmp_path / "p.frisket")
    try:

        def _boom(*a, **k):
            raise RuntimeError("bootstrap blew up")

        monkeypatch.setattr(
            "frisket.authoring.workbench.plugin_runtime.bootstrap_project_bundled_plugins",
            _boom,
        )
        r = diagnostics.plugin_health_report(project, project_id="p")
        assert r["available"] is False
        assert r["installed"] == 0 and r["active"] == 0 and r["failed"] == 0
        assert r["failed_names"] == []
        assert "bootstrap blew up" in r["summary"]
    finally:
        project.close()


# ---------------------------------------------------------------------------
# entities_report: PyICU install preflight


def test_entities_report_unavailable_includes_the_pyicu_preflight(monkeypatch):
    monkeypatch.setattr(
        diagnostics,
        "pyicu_install_preflight",
        lambda: {"summary": "source-build prerequisite report", "ready": False},
    )

    def _boom():
        raise ImportError("FollowTheMoney entity support is not installed.")

    import frisket.features.followthemoney as ftm

    monkeypatch.setattr(ftm, "entities_available", _boom, raising=False)
    r = diagnostics.entities_report()
    assert r["installed"] is False
    assert "no official wheels on any platform" in r["summary"]
    assert "source-build prerequisite report" in r["summary"]
    assert r["pyicu_install_preflight"]["ready"] is False


def test_entities_report_installed_has_no_hint_noise(monkeypatch):
    import frisket.features.followthemoney as ftm

    monkeypatch.setattr(ftm, "entities_available", lambda: (True, None), raising=False)
    monkeypatch.setattr(
        diagnostics,
        "pyicu_install_preflight",
        lambda: {"summary": "PyICU 2.16.2 is installed", "ready": True},
    )
    r = diagnostics.entities_report()
    assert r["installed"] is True
    assert r["summary"] == "followthemoney (entities extra) installed"
    assert r["pyicu_install_preflight"]["ready"] is True


def test_pyicu_preflight_reports_each_missing_source_build_prerequisite(
    monkeypatch, tmp_path
):
    def _missing_distribution(_name):
        raise diagnostics.importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(
        diagnostics.importlib.metadata, "version", _missing_distribution
    )
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    monkeypatch.delenv("CONDA_DEFAULT_ENV", raising=False)
    monkeypatch.setattr(diagnostics.sys, "platform", "linux")
    monkeypatch.setattr(
        diagnostics.shutil,
        "which",
        lambda name: "/usr/bin/apt-get" if name == "apt-get" else None,
    )
    monkeypatch.setattr(
        diagnostics.sysconfig, "get_path", lambda _name: str(tmp_path / "include")
    )

    report = diagnostics.pyicu_install_preflight()

    assert report["strategy"] == "source-build"
    assert report["official_pypi_artifacts"] == "sdist-only"
    assert report["ready"] is False
    assert report["missing"] == [
        "C++ compiler",
        "Python development headers",
        "pkg-config",
        "ICU development headers/libraries",
    ]
    assert (
        report["remediation"]
        == "sudo apt install build-essential python3-dev pkg-config libicu-dev"
    )


def test_pyicu_preflight_proves_a_complete_source_build_toolchain(
    monkeypatch, tmp_path
):
    def _missing_distribution(_name):
        raise diagnostics.importlib.metadata.PackageNotFoundError

    include_dir = tmp_path / "include"
    include_dir.mkdir()
    (include_dir / "Python.h").touch()
    paths = {"c++": "/usr/bin/c++", "pkg-config": "/usr/bin/pkg-config"}
    monkeypatch.setattr(
        diagnostics.importlib.metadata, "version", _missing_distribution
    )
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    monkeypatch.delenv("CONDA_DEFAULT_ENV", raising=False)
    monkeypatch.setattr(diagnostics.sys, "platform", "linux")
    monkeypatch.setattr(diagnostics.shutil, "which", paths.get)
    monkeypatch.setattr(
        diagnostics.sysconfig, "get_path", lambda _name: str(include_dir)
    )
    monkeypatch.setattr(
        diagnostics, "_command_succeeds", lambda argv: argv[-1] == "icu-i18n"
    )

    report = diagnostics.pyicu_install_preflight()

    assert report["strategy"] == "source-build"
    assert report["ready"] is True
    assert report["missing"] == []
    assert report["icu_development"] is True
    assert report["remediation"] is None


def test_pyicu_preflight_routes_conda_and_native_windows_away_from_source_build(
    monkeypatch,
):
    def _missing_distribution(_name):
        raise diagnostics.importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(
        diagnostics.importlib.metadata, "version", _missing_distribution
    )
    monkeypatch.setenv("CONDA_PREFIX", "/conda/envs/frisket")
    conda = diagnostics.pyicu_install_preflight()
    assert conda["strategy"] == "conda-binary"
    assert conda["remediation"] == "conda install -c conda-forge pyicu"

    monkeypatch.delenv("CONDA_PREFIX")
    monkeypatch.setattr(diagnostics.sys, "platform", "win32")
    windows = diagnostics.pyicu_install_preflight()
    assert windows["strategy"] == "conda-required"
    assert "Miniforge" in windows["remediation"]


# ---------------------------------------------------------------------------
# run_diagnostics: every new probe is wired into the aggregate report


def test_run_diagnostics_exposes_all_new_info_keys(tmp_path):
    queue = open_queue(workspace=tmp_path)
    project = Project.create(tmp_path / "p.frisket")
    try:
        report = diagnostics.run_diagnostics(
            queue=queue,
            workspace_root=tmp_path,
            project=project,
            project_id="p",
        )
    finally:
        project.close()
    for key in (
        "provider_key_validation",
        "replay_mode",
        "queue_health",
        "disk_free",
        "plugin_health",
    ):
        assert key in report["info"], f"missing info.{key}"
        assert "summary" in report["info"][key]


# ---------------------------------------------------------------------------
# Diagnose routes: the new probes reach the JSON surface on the bare
# workspace probe, and the project-scoped probe resolves plugin health.
# The project rides on the PATH (diagnose-project-on-the-path-v1).


def test_diagnose_route_reports_new_probes(tmp_path):
    client = TestClient(create_app(tmp_path / "ws"))
    resp = client.get("/api/diagnose")
    assert resp.status_code == 200
    info = resp.json()["info"]
    assert "queue_health" in info and info["queue_health"]["configured"] is True
    assert "disk_free" in info and "free_gb" in info["disk_free"]
    assert "plugin_health" in info and info["plugin_health"]["available"] is False
    assert "replay_mode" in info
    assert "provider_key_validation" in info


@pytest.mark.real_bundled_plugins
def test_diagnose_route_resolves_plugin_health_for_a_real_project(
    tmp_path, _real_bundled_plugin_registry
):
    ws_root = tmp_path / "ws"
    app = create_app(ws_root)
    client = TestClient(app)
    create_resp = client.post("/api/projects", json={"name": "Diag Probe"})
    assert create_resp.status_code == 200, create_resp.text
    project_id = create_resp.json()["id"]
    resp = client.get(f"/api/projects/{project_id}/diagnose")
    assert resp.status_code == 200
    plugin_health = resp.json()["info"]["plugin_health"]
    assert plugin_health["available"] is True
    # bootstrap runs through the route too -- a real project has real
    # bundled first-party plugins, not zero.
    assert plugin_health["installed"] > 0


def test_diagnose_route_bad_project_id_refuses_instead_of_degrading(tmp_path):
    """The degrade-to-200 was a project-existence oracle; it now 404s.

    On an edition with a role ladder the ladder refuses first (403). On the
    core app there is no ladder, so the workspace's own lookup answers.
    """

    client = TestClient(create_app(tmp_path / "ws"))
    resp = client.get("/api/projects/does-not-exist/diagnose")
    assert resp.status_code == 404, resp.text


def test_media_toolbelt_reports_deno_so_a_jsless_fallback_is_visible(monkeypatch):
    from frisket.operability import diagnostics as diag

    monkeypatch.setattr(
        diag.shutil,
        "which",
        lambda tool: None if tool == "deno" else f"/usr/bin/{tool}",
    )
    report = diag.media_toolbelt_report()

    # yt-dlp degrades to jsless clients and still exits zero when deno is
    # absent, and the child's --no-warnings hides its own notice, so doctor is
    # the only place the operator can learn about it.
    assert report["missing"] == ["deno"]
    assert "deno" in report["summary"]
    assert report["present"] == ["ffmpeg", "ffprobe"]


def test_media_toolbelt_summary_is_clean_when_every_tool_resolves(monkeypatch):
    from frisket.operability import diagnostics as diag

    monkeypatch.setattr(diag.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    report = diag.media_toolbelt_report()

    assert report["missing"] == []
    assert "missing" not in report["summary"]


def _pypi_ytdlp_response(version: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"info": {"version": version}},
        request=httpx.Request("GET", "https://pypi.org/pypi/yt-dlp/json"),
    )


def test_ytdlp_update_report_names_the_installation_as_the_remedy(monkeypatch):
    # The on-demand doctor probe replaced the launcher's ambient PyPI
    # phone-home: the network call happens only here, and the remediation is
    # the honest one (the package changes only when the installation does).
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: _pypi_ytdlp_response("2099.1.1")
    )
    report = diagnostics.ytdlp_update_report()

    assert report["update_available"] is True
    assert report["latest"] == "2099.1.1"
    assert "update this Frisket installation" in report["summary"]


def test_ytdlp_update_report_current_and_unreachable_never_raise(monkeypatch):
    import importlib.metadata

    installed = importlib.metadata.version("yt-dlp")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _pypi_ytdlp_response(installed))
    current = diagnostics.ytdlp_update_report()
    assert current["update_available"] is False
    assert current["summary"] == f"yt-dlp {installed} is current"

    def _offline(url, **kw):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", _offline)
    offline = diagnostics.ytdlp_update_report()
    assert offline["update_available"] is None
    assert offline["installed"] == installed
    assert "PyPI unreachable" in offline["summary"]
