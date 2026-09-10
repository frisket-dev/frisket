"""The cache axis of the routers ``Workspace`` composes.

Two claims live here, both about operator surfaces that exist to describe the
runtime truthfully:

A. ``Workspace.diagnostic_router()`` (what ``GET /api/diagnose`` probes with)
   reports the SAME ``cache_mode`` ``GET /api/config`` reports. Its
   reconstructing branches used to omit ``cache_mode=`` entirely and inherit
   ``ModelRouter``'s ``"replay"`` default, so a workspace running under
   ``FRISKET_CACHE_MODE=replay_strict`` had ``/api/config`` say
   ``replay_strict`` and ``/api/diagnose`` say ``replay`` in the same breath.

B. The closure guard for the workspace-level claim "replay_strict means no
   live calls" (``/api/config``'s replay banner, which reads ``cache_mode``
   ALONE via ``live_calls_possible``). That claim is only true because a
   strict-replay router with a cache is what ``router_for`` hands out --
   ``ModelRouter._complete_transport``'s strict branch is guarded on
   ``self.cache is not None``, so a CACHELESS strict router skips both replay
   branches and calls the live adapter. A cacheless strict router is perfectly
   legal in isolation (tests/ai/test_llm_model_key_request_gate.py and
   tests/authoring/test_diagnostics_wave2.py construct one deliberately, to
   prove the money path treats it as live), so the invariant belongs HERE, at
   the composition boundary, not as a constructor ban.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter, ResponseCache
from frisket.engine.store import Project
from frisket.operability.diagnostics import replay_mode_report
from frisket.server import provider_config
from frisket.server.app import create_app
from frisket.server.workspace import Workspace

PROVIDER_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
)


@pytest.fixture
def strict_env(monkeypatch):
    """A workspace booted under the operator's strict-replay knob, with no
    ambient provider env keys leaking into the branch selection."""
    monkeypatch.setenv("FRISKET_CACHE_MODE", "replay_strict")
    for var in PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _cached_router(tmp_path: Path, name: str, **kwargs) -> ModelRouter:
    return ModelRouter(
        cache=ResponseCache(tmp_path / f"{name}.cache.db"),
        use_env_keys=False,
        **kwargs,
    )


def test_diagnostic_router_pure_local_branch_reports_the_operator_mode(
    tmp_path, strict_env
):
    """The reproduction. ``diagnostic_router``'s pure-local branch built a
    ``ModelRouter`` with no ``cache_mode=``, so ``replay_mode_report`` read
    the constructor default and ``frisket``'s own diagnose surface described a
    runtime that was not the one running."""
    workspace = Workspace(tmp_path / "local")

    assert workspace.cache_mode == "replay_strict"
    assert workspace.diagnostic_router().cache_mode == "replay_strict"
    assert replay_mode_report(workspace.diagnostic_router())["cache_mode"] == (
        "replay_strict"
    )


def test_diagnose_and_config_routes_agree_on_the_cache_mode(tmp_path, strict_env):
    client = TestClient(create_app(tmp_path / "ws"))

    configured = client.get("/api/config")
    diagnosed = client.get("/api/diagnose")
    assert configured.status_code == 200
    assert diagnosed.status_code == 200

    assert configured.json()["cache_mode"] == "replay_strict"
    assert diagnosed.json()["info"]["replay_mode"]["cache_mode"] == "replay_strict"


def test_diagnostic_router_org_branch_carries_the_injected_routers_cache_posture(
    tmp_path, strict_env
):
    """The hosted half of the same bug, and the reason the cache object is
    threaded there: with a ``provider_keys_resolver`` the diagnostic router is
    reconstructed from the org's keys, and it used to drop BOTH axes of the
    injected base router -- reporting ``replay`` / no cache for an org whose
    real execution router was strict-with-cache."""
    base = _cached_router(tmp_path, "org", cache_mode="replay_strict")
    workspace = Workspace(
        tmp_path / "org-ws",
        router=base,
        provider_keys_resolver=lambda: {"openai": "org-value"},
    )

    diagnostic = workspace.diagnostic_router()
    assert diagnostic is not base
    assert diagnostic.cache_mode == "replay_strict"
    assert diagnostic.cache is base.cache

    report = replay_mode_report(diagnostic)
    assert report["cache_configured"] is True
    assert report["live_calls_possible"] is False


def test_diagnostic_router_org_branch_without_a_base_router_uses_the_local_mode(
    tmp_path, strict_env
):
    """No injected base router means no real cache to name, so the probe says
    ``replay_strict`` and ``cache_configured=False`` -- honest about ITSELF.
    The alternative (minting a workspace-level cache file just so the probe
    could claim True) would make the report describe a cache no execution path
    reads."""
    workspace = Workspace(
        tmp_path / "resolver-only",
        provider_keys_resolver=lambda: {"openai": "org-value"},
    )

    diagnostic = workspace.diagnostic_router()
    assert diagnostic.cache_mode == "replay_strict"
    assert diagnostic.cache is None
    assert replay_mode_report(diagnostic)["cache_configured"] is False


def test_diagnostic_router_never_claims_a_cache_it_does_not_have(tmp_path, strict_env):
    """The report matches reality on BOTH axes, in every branch: whatever
    ``cache_configured`` says, the router's ``cache`` attribute agrees."""
    workspaces = {
        "pure_local": Workspace(tmp_path / "d-local"),
        "org_resolver": Workspace(
            tmp_path / "d-org", provider_keys_resolver=lambda: {"openai": "v"}
        ),
        "injected": Workspace(
            tmp_path / "d-inj",
            router=_cached_router(tmp_path, "d-inj", cache_mode="replay_strict"),
        ),
    }
    for label, workspace in workspaces.items():
        router = workspace.diagnostic_router()
        report = replay_mode_report(router)
        assert report["cache_configured"] is (router.cache is not None), label
        assert report["cache_mode"] == router.cache_mode, label


@dataclass(frozen=True)
class _Composition:
    label: str
    base_router_cache: str | None  # "cache" | "none" | None (no injected router)
    org_resolver: bool
    workspace_file_key: bool
    project_key: bool


COMPOSITIONS = (
    _Composition("pure_local_keyless", None, False, False, False),
    _Composition("pure_local_workspace_file_key", None, False, True, False),
    _Composition("pure_local_project_key", None, False, False, True),
    _Composition("org_resolver_only", None, True, False, False),
    _Composition("injected_plus_org_resolver", "cache", True, False, False),
    _Composition("injected_plus_project_key", "cache", False, False, True),
    _Composition("cacheless_injected_plus_org_resolver", "none", True, False, False),
    _Composition("injected_passthrough", "cache", False, False, False),
    _Composition("cacheless_injected_passthrough", "none", False, False, False),
)


def _build(tmp_path: Path, composition: _Composition) -> tuple[Workspace, Project]:
    root = tmp_path / composition.label
    base: ModelRouter | None = None
    if composition.base_router_cache == "cache":
        base = _cached_router(tmp_path, composition.label, cache_mode="replay_strict")
    elif composition.base_router_cache == "none":
        base = ModelRouter(cache=None, cache_mode="replay_strict", use_env_keys=False)

    workspace = Workspace(
        root,
        router=base,
        provider_keys_resolver=(
            (lambda: {"openai": "org-value"}) if composition.org_resolver else None
        ),
    )
    if composition.workspace_file_key:
        provider_config.save_local_provider_key(workspace.root, "openai", "sk-file")

    project = Project.create(root / f"{composition.label}.frisket", name="p")
    if composition.project_key:
        from frisket.team.security.secrets import encrypt_secret, key_hint

        project.set_provider_key(
            provider="anthropic",
            encrypted=encrypt_secret("sk-project"),
            hint=key_hint("sk-project"),
            spend_cap_micro=None,
        )
    return workspace, project


@pytest.mark.parametrize("composition", COMPOSITIONS, ids=lambda c: c.label)
def test_router_for_strict_replay_implies_a_cache(tmp_path, strict_env, composition):
    """THE invariant ``/api/config``'s replay banner rests on.

    ``live_calls_possible`` reads the mode string alone, so a router that says
    ``replay_strict`` while carrying no cache makes the banner's "no live AI
    calls can be made" a lie -- ``_complete_transport``'s strict branch is
    guarded on ``self.cache is not None`` and a cacheless strict router falls
    straight through to the live adapter.

    THE ONE EXEMPTION: a composition root that injects a cacheless strict
    router has DECLARED that posture, and ``router_for`` must propagate it
    rather than launder it by fabricating a cache the base never had. So the
    invariant is: whenever ``router_for`` itself decides the cache, a strict
    result has one; where an injected base decides, the result mirrors that
    base exactly. Either way ``router_for`` can never keep the strict mode
    while losing the cache, which is the regression this goes red on.
    """
    workspace, project = _build(tmp_path, composition)
    try:
        router = workspace.router_for(project)
        assert router.cache_mode == "replay_strict", composition.label

        base = workspace._router
        if base is not None:
            assert router.cache is base.cache, composition.label
        else:
            assert router.cache is not None, composition.label
    finally:
        project.close()


def test_router_for_branch_closure(tmp_path, strict_env):
    """Mechanical, not a promise (CLAUDE.md: agents undercount call sites).

    The shared router-composition helper has exactly four ``return`` sites,
    and the parametrization above reaches every one. Add a fifth branch and
    this goes red until the new composition is added to ``COMPOSITIONS`` with
    its cache posture decided -- which is the whole point: a branch nobody
    thought about is exactly how a cacheless strict router would reach
    ``/api/config``.
    """
    source = textwrap.dedent(
        inspect.getsource(Workspace._router_for_base)  # noqa: SLF001
    )
    function = ast.parse(source).body[0]
    return_sites = [n for n in ast.walk(function) if isinstance(n, ast.Return)]
    assert len(return_sites) == 4, (
        "_router_for_base grew a branch; give it a _Composition with its cache "
        "posture decided. Return statements now at (function-relative) lines "
        f"{sorted(site.lineno for site in return_sites)}: "
        + " | ".join(ast.unparse(site).splitlines()[0] for site in return_sites)
    )

    reached: set[int] = set()
    for composition in COMPOSITIONS:
        workspace, project = _build(tmp_path, composition)
        try:
            reached.add(_branch_line_reached(workspace, project))
        finally:
            project.close()
    assert reached == {site.lineno for site in return_sites}


def _branch_line_reached(workspace: Workspace, project: Project) -> int:
    """Which return line of the shared composition helper executes.

    A line trace rather than a re-implementation of the branch conditions:
    restating the conditions here would let the test and the code drift in
    exactly the direction the closure guard exists to catch.
    """
    import sys

    source_file = inspect.getsourcefile(Workspace._router_for_base)  # noqa: SLF001
    start = inspect.getsourcelines(Workspace._router_for_base)[1]  # noqa: SLF001
    executed: list[int] = []

    def _trace(frame, event, _arg):
        if frame.f_code.co_filename != source_file:
            return None
        if frame.f_code.co_name != "_router_for_base":
            return None
        if event == "line":
            executed.append(frame.f_lineno)
        return _trace

    previous = sys.gettrace()
    sys.settrace(_trace)
    try:
        workspace.router_for(project)
    finally:
        sys.settrace(previous)

    source = textwrap.dedent(
        inspect.getsource(Workspace._router_for_base)  # noqa: SLF001
    )
    function = ast.parse(source).body[0]
    # ast line numbers are function-relative; executed ones are file-absolute.
    return_lines = {
        site.lineno + start - 1
        for site in ast.walk(function)
        if isinstance(site, ast.Return)
    }
    hit = [line for line in executed if line in return_lines]
    assert hit, f"no return site traced; executed={executed}"
    return hit[-1] - start + 1
