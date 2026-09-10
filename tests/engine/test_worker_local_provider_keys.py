"""worker-local-provider-keys-v1 (live wafflehouses failure, 2026-07-15
night): the user ran the copilot's map.extract proposal and EVERY row failed
with "No API key is configured for 'gemini'" — while the copilot itself had
just used that same gemini key successfully.

Root cause: UI-entered workspace keys (<ws>/.frisket/provider_keys.json) are resolved ONLY in the server process
(server/workspace.py::router_for via provider_config.resolve_effective_keys).
Queued action runs execute in the WORKER process (frisket.cli worker), whose
handler (jobs/runs.py::handle) resolves keys only for the hosted org-BYOK
branch (org_id + control DB) and leaves ``keys=None`` in the local tier — so
the run router is env-only and the per-row calls raise the missing-key
remediation the user saw.

DONE means (white-box jobs/runs.py):
- ``resolve_run_keys(root, org_id=None, ...)`` returns workspace-file hosted
  provider keys (env still wins), labeled ``"local"``; local endpoints travel
  through the separate plural endpoint authority. The hosted branch keeps resolving
  through the credentials port labeled ``"org_byok"``.
- ``_router_for`` labels the injected keys with the caller's source, so a
  local-tier run's receipts say ``local``, never ``org_byok``.
- A router built from the local resolution actually carries the provider
  adapter (the failure the user hit).
"""

from __future__ import annotations

from pathlib import Path

from frisket.ai.llm import ModelRouter
from frisket.engine.jobs import runs as run_jobs
from frisket.server import provider_config


def _seed_workspace(root: Path):
    provider_config.save_local_provider_key(root, "gemini", "AIza-workspace-key")
    return provider_config.create_local_endpoint(
        root,
        name="LAN server",
        url="http://10.0.0.5:11434",
    )


def test_local_tier_resolves_workspace_file_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    root = tmp_path / "ws"
    _seed_workspace(root)

    keys, source = run_jobs.resolve_run_keys(
        root, org_id=None, credentials=None, control_database_url=None
    )

    assert source == "local"
    assert keys is not None
    assert keys["gemini"] == "AIza-workspace-key"
    assert "ollama" not in keys


def test_local_tier_env_still_wins_over_the_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "env-winner")
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    root = tmp_path / "ws"
    _seed_workspace(root)

    keys, _source = run_jobs.resolve_run_keys(
        root, org_id=None, credentials=None, control_database_url=None
    )

    # env-owned provider dropped from the file layer; router falls back to env
    assert keys is None


def test_hosted_branch_keeps_the_credentials_port(tmp_path) -> None:
    class _Port:
        def provider_keys(self, *, org_id: int, control_database_url: str):
            assert org_id == 7
            assert control_database_url == "postgres://x"
            return {"anthropic": "org-key"}

    keys, source = run_jobs.resolve_run_keys(
        tmp_path / "ws",
        org_id=7,
        credentials=_Port(),
        control_database_url="postgres://x",
    )

    assert source == "org_byok"
    assert keys == {"anthropic": "org-key"}


def test_org_without_control_db_never_claims_an_org_key_was_read(
    tmp_path, monkeypatch
) -> None:
    """The ``org_byok`` label is evidence of a control-plane key read.

    A malformed hosted worker with an org id but no control DB can only see
    operator environment infrastructure. Labeling that fallback ``org_byok``
    zero-rated a credential the organization neither supplied nor owned.
    """

    monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434")
    keys, source = run_jobs.resolve_run_keys(
        tmp_path / "ws",
        org_id=7,
        credentials=_OrgCredentialsPort(),
        control_database_url=None,
    )

    assert source == "local"
    assert keys is None


def test_run_router_carries_the_locally_keyed_adapter(tmp_path, monkeypatch) -> None:
    """The failure the user hit, pinned end to end at the router seam: a
    local-tier run router must have the gemini adapter and label its
    credential source 'local'."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    root = tmp_path / "ws"
    endpoint = _seed_workspace(root)
    project_dir = root / "p.frisket"
    from frisket.engine.store import Project

    project = Project.create(project_dir, name="p")

    keys, source = run_jobs.resolve_run_keys(
        root, org_id=None, credentials=None, control_database_url=None
    )
    router = run_jobs._router_for(  # noqa: SLF001 - white-box seam
        project,
        None,
        keys=keys,
        keys_source=source,
        use_env_keys=True,
        local_endpoints=run_jobs.resolve_run_local_endpoints(root, org_id=None),
    )

    assert "gemini" in router.configured_keys()
    assert router.credential_source_for("gemini") == "local"
    assert router.local_endpoints == (endpoint,)


def test_run_router_uses_the_cache_mode_frozen_by_enqueue(tmp_path) -> None:
    root = tmp_path / "ws"
    project = _make_project(root)
    try:
        router = run_jobs._router_for(  # noqa: SLF001 - white-box seam
            project,
            None,
            use_env_keys=False,
            cache_mode="replay_strict",
        )
        assert router.cache_mode == "replay_strict"
    finally:
        project.close()


def test_equal_empty_endpoint_authority_preserves_injected_router_runtime(
    tmp_path,
) -> None:
    """The worker's resolved ``()`` is not a reason to rebuild an injected
    router that is already endpoint-empty.

    Returning the same object preserves custom adapters and subclass/runtime
    state that cannot be reconstructed from provider keys alone.
    """
    project = _make_project(tmp_path / "ws")
    injected = ModelRouter(
        keys={"anthropic": "test-key"},
        key_sources={"anthropic": "local"},
        cache=None,
        cache_mode="off",
        max_retries=1,
        use_env_keys=False,
    )
    adapter = object()
    injected._adapters["anthropic"] = adapter  # noqa: SLF001 - injected seam
    try:
        composed = run_jobs._router_for(  # noqa: SLF001 - white-box seam
            project,
            injected,
            use_env_keys=False,
            local_endpoints=(),
        )

        assert composed is injected
        assert composed.adapter_for("anthropic") is adapter
        assert composed.configured_keys() == {"anthropic": "test-key"}
        assert composed.cache is None
        assert composed.cache_mode == "off"
        assert composed.max_retries == 1
    finally:
        project.close()


def test_explicit_empty_endpoint_authority_clears_injected_endpoints(tmp_path) -> None:
    """An empty tuple still means fail closed when the injected router carries
    endpoint authority that the worker can no longer resolve.
    """
    root = tmp_path / "ws"
    project = _make_project(root)
    endpoint = provider_config.create_local_endpoint(
        root,
        name="Removed server",
        url="http://127.0.0.1:11434",
    )
    injected = ModelRouter(
        use_env_keys=False,
        local_endpoints=(endpoint,),
    )
    try:
        composed = run_jobs._router_for(  # noqa: SLF001 - white-box seam
            project,
            injected,
            use_env_keys=False,
            local_endpoints=(),
        )

        assert composed is not injected
        assert composed.local_endpoints == ()
        assert composed.adapter_for("ollama") is None
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Org-run local-server carve-out.
#
# Rationale: the local-server URL is operator DEPLOYMENT INFRASTRUCTURE — the
# same category as FRISKET_MODELS_URL, which every org run already reads
# straight from the process env regardless of use_env_keys — not a tenant
# credential. Org runs build routers with use_env_keys=False specifically to
# keep the WORKER PROCESS's env (ANTHROPIC_API_KEY etc.) out of tenant runs;
# that gate must keep doing exactly that for API keys while still letting the
# compose-preset OLLAMA_URL reach the org run's router, since without this
# carve-out a team operator's `docker compose --profile ollama up` is dead
# config: org runs never see it and the adapter silently targets
# localhost:11434 inside the worker's own container.
# ---------------------------------------------------------------------------


class _OrgCredentialsPort:
    """Fake credentials port standing in for the hosted control-plane
    lookup: returns whatever org BYOK rows the test wants, independent of
    the process env."""

    def __init__(self, provider_keys: dict[str, str] | None = None) -> None:
        self._provider_keys = dict(provider_keys or {})

    def provider_keys(self, *, org_id: int, control_database_url: str):
        return dict(self._provider_keys)


def test_org_run_router_takes_local_server_url_from_env(tmp_path, monkeypatch) -> None:
    """(a) An org run (use_env_keys=False) with env OLLAMA_URL set builds a
    router whose ollama adapter targets that URL — the compose 'ollama'
    profile's OLLAMA_URL must reach team org runs, not just local-tier ones."""
    monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    root = tmp_path / "ws"
    project = _make_project(root)

    keys, source = run_jobs.resolve_run_keys(
        root,
        org_id=7,
        credentials=_OrgCredentialsPort(),
        control_database_url="postgres://x",
    )
    router = run_jobs._router_for(  # noqa: SLF001 - white-box seam
        project,
        None,
        keys=keys,
        keys_source=source,
        use_env_keys=False,
        local_endpoints=run_jobs.resolve_run_local_endpoints(root, org_id=7),
    )

    assert len(router.local_endpoints) == 1
    assert router.local_endpoints[0].origin == "http://ollama:11434"


def test_org_run_api_keys_stay_excluded_from_env(tmp_path, monkeypatch) -> None:
    """(b) The same org run with env ANTHROPIC_API_KEY set does NOT pick up
    an anthropic adapter/key from env — the carve-out is local-server-URL
    only; API-key gating for org runs is unchanged."""
    monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-worker-process-secret")
    root = tmp_path / "ws"
    project = _make_project(root)

    keys, source = run_jobs.resolve_run_keys(
        root,
        org_id=7,
        credentials=_OrgCredentialsPort(),  # no anthropic BYOK row configured
        control_database_url="postgres://x",
    )
    router = run_jobs._router_for(  # noqa: SLF001 - white-box seam
        project,
        None,
        keys=keys,
        keys_source=source,
        use_env_keys=False,
        local_endpoints=run_jobs.resolve_run_local_endpoints(root, org_id=7),
    )

    assert "anthropic" not in router.configured_keys()
    assert keys is None or "anthropic" not in keys


def test_org_run_rejects_invalid_env_local_server_url_without_fallback(
    tmp_path, monkeypatch
) -> None:
    """(c) An env OLLAMA_URL that fails origin-only validation (path-bearing,
    per normalize_ollama_url) is rejected for org runs without inventing a
    fallback endpoint."""
    monkeypatch.setenv("OLLAMA_URL", "http://evil.example/llm/path")
    root = tmp_path / "ws"
    project = _make_project(root)

    keys, source = run_jobs.resolve_run_keys(
        root,
        org_id=7,
        credentials=_OrgCredentialsPort(),
        control_database_url="postgres://x",
    )
    router = run_jobs._router_for(  # noqa: SLF001 - white-box seam
        project,
        None,
        keys=keys,
        keys_source=source,
        use_env_keys=False,
        local_endpoints=run_jobs.resolve_run_local_endpoints(root, org_id=7),
    )

    assert router.local_endpoints == ()


def _make_project(root: Path):
    from frisket.engine.store import Project

    project_dir = root / "p.frisket"
    return Project.create(project_dir, name="p")


# ---------------------------------------------------------------------------
# Org env-bundle carve-out threading: org runs get the whole validated env
# bundle -- origin AND tokens -- not a lone URL.
# ---------------------------------------------------------------------------


def test_resolve_run_local_endpoints_org_carries_tokens_atomically(
    tmp_path, monkeypatch
) -> None:
    """An org run's resolved local_endpoint bundle carries the inference
    token, not just the origin. Threading only a lone URL string would
    mean a real split-host deployment's token never
    reached org-run routers even though the origin did."""
    monkeypatch.setenv("OLLAMA_URL", "https://llm.heavy.internal")
    monkeypatch.setenv("FRISKET_LLM_TOKEN", "org-inference-secret")
    root = tmp_path / "ws"

    endpoints = run_jobs.resolve_run_local_endpoints(root, org_id=7)

    assert len(endpoints) == 1
    endpoint = endpoints[0]
    assert endpoint.origin == "https://llm.heavy.internal"
    assert endpoint.inference_token == "org-inference-secret"
    assert endpoint.source == "env"


def test_router_for_org_run_threads_the_resolved_local_endpoint(
    tmp_path, monkeypatch
) -> None:
    """End-to-end at the router seam: a router built through _router_for with
    the resolved org bundle actually carries the token as its bearer."""
    monkeypatch.setenv("OLLAMA_URL", "https://llm.heavy.internal")
    monkeypatch.setenv("FRISKET_LLM_TOKEN", "org-inference-secret")
    root = tmp_path / "ws"
    project = _make_project(root)

    keys, source = run_jobs.resolve_run_keys(
        root,
        org_id=7,
        credentials=_OrgCredentialsPort(),
        control_database_url="postgres://x",
    )
    endpoints = run_jobs.resolve_run_local_endpoints(root, org_id=7)
    router = run_jobs._router_for(  # noqa: SLF001 - white-box seam
        project,
        None,
        keys=keys,
        keys_source=source,
        use_env_keys=False,
        local_endpoints=endpoints,
    )

    endpoint = router.local_endpoints[0]
    assert endpoint.origin == "https://llm.heavy.internal"
    assert endpoint.inference_token == "org-inference-secret"
    _, adapter, _ = router.resolve_local_model(
        f"ollama/@{endpoint.endpoint_id}/qwen3:8b"
    )
    assert adapter.api_key == "org-inference-secret"


def test_resolve_run_local_endpoints_org_rejects_token_with_invalid_origin(
    tmp_path, monkeypatch
) -> None:
    """The atomic-bundle rule applies to org runs too: a token configured
    alongside an invalid (path-bearing) origin must not ride a fallback
    origin -- the whole env bundle is rejected, tokenless default origin."""
    monkeypatch.setenv("OLLAMA_URL", "http://evil.example/llm/path")
    monkeypatch.setenv("FRISKET_LLM_TOKEN", "should-never-be-used")
    root = tmp_path / "ws"

    assert run_jobs.resolve_run_local_endpoints(root, org_id=7) == ()


def test_resolve_run_local_endpoints_local_tier_uses_full_resolution(
    tmp_path, monkeypatch
) -> None:
    """Local-tier (org_id=None) runs resolve through the SAME atomic bundle
    logic Workspace.router_for uses (env, then file, then default) -- not
    the org-only env carve-out."""
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    from frisket.server import provider_config

    root = tmp_path / "ws"
    # The token-bearing-origin rule allows plain HTTP only for loopback
    # addresses -- a real LAN origin would need
    # https, exercised separately by the provider_config-level tests.
    origin = "http://127.0.0.1:11434"
    created = provider_config.create_local_endpoint(
        root,
        name="Loopback",
        url=origin,
        inference_token="local-file-token",
        provisioning_token=None,
        edge_auth=False,
    )

    endpoints = run_jobs.resolve_run_local_endpoints(root, org_id=None)

    assert len(endpoints) == 1
    endpoint = endpoints[0]
    assert endpoint.endpoint_id == created.endpoint_id
    assert endpoint.origin == origin
    assert endpoint.inference_token == "local-file-token"
    assert endpoint.source == "local_file"


def test_base_compose_does_not_synthesize_a_local_endpoint() -> None:
    # rule19: verifies the independently executed deployment artifact's env wiring.
    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    assert "OLLAMA_URL: ${OLLAMA_URL:-}" in compose
    assert "OLLAMA_URL: ${OLLAMA_URL:-http://ollama:11434}" not in compose
