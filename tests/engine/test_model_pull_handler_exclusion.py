"""model.pull's worker-handler exclusion from team/managed compositions
(GA acceptance criterion: "negative tests:
pull route AND worker handler absent in a managed composition").

The gate lives OUTSIDE ``register_production_handlers`` -- the one function
every edition (including the external ``cloud`` composition, physically
absent from this open tree) calls -- rather than as a parameter on it, so
an external composition that has no idea this local-only feature exists
still cannot end up with it registered. Local-tier callers opt in
explicitly at the two known call sites: ``Workspace``/``create_app``
(gated by the same flag that gates the provider-config routes) and the
plain local ``frisket worker`` CLI command.
"""

from __future__ import annotations

from pathlib import Path

import frisket.engine.jobs.worker as worker_module
from frisket.engine.jobs.queue import open_queue
from frisket.engine.jobs.worker import (
    HandlerRegistry,
    register_hosted_handlers,
    register_production_handlers,
)
from frisket.server.app import create_app
from frisket.server.workspace import Workspace


def test_register_production_handlers_never_registers_model_pull(tmp_path) -> None:
    """The shared function every edition composes structurally cannot carry
    this local-only feature -- true regardless of what any edition (open
    team, or an external managed composition) does on top of it."""
    queue = open_queue(workspace=tmp_path)
    try:
        registry = HandlerRegistry()
        register_production_handlers(registry, workspace_root=tmp_path, queue=queue)
        assert "model.pull" not in registry.kinds()
    finally:
        queue.close()


def test_hosted_composition_lacks_model_pull_even_if_it_calls_the_open_registrar(
    tmp_path, monkeypatch
) -> None:
    """Stand in for the physically-absent external ``cloud`` edition: a fake
    composer that does exactly what the real one is documented to do (call
    ``register_production_handlers`` plus its own ports). Because
    that shared function never registers ``model.pull``, the hosted registry
    ends up without it even though this fake composer has no idea the
    feature exists -- proving the exclusion holds by construction, not by
    the external package cooperating with a flag it doesn't know about."""
    queue = open_queue(workspace=tmp_path)
    try:

        def fake_cloud_compose(registry, *, workspace_root, queue, **_kwargs):
            register_production_handlers(
                registry, workspace_root=workspace_root, queue=queue
            )

        monkeypatch.setattr(
            worker_module,
            "load_worker_edition",
            lambda name: fake_cloud_compose,
        )

        registry = HandlerRegistry()
        register_hosted_handlers(registry, workspace_root=tmp_path, queue=queue)
        assert "model.pull" not in registry.kinds()
    finally:
        queue.close()


def test_local_create_app_registers_model_pull_in_workspace_registry(tmp_path) -> None:
    app = create_app(tmp_path / "ws")
    registry = app.state.workspace.registry
    assert "model.pull" in registry.kinds()


def test_team_like_composition_enable_provider_config_false_lacks_model_pull(
    tmp_path,
) -> None:
    """``create_app(enable_provider_config=False)`` is exactly team's own
    call shape (src/frisket/team/app.py); the SAME flag that structurally
    removes the provider-config routes there must also remove the worker
    handler, not just the route."""
    app = create_app(tmp_path / "ws", enable_provider_config=False)
    registry = app.state.workspace.registry
    assert "model.pull" not in registry.kinds()
    # not just the handler -- the routes are absent too (team's existing
    # structural guarantee, 2026-07-04 review), including the new pull ones.
    provider_paths = {
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/providers")
    }
    assert provider_paths == set()


def test_workspace_constructed_directly_can_opt_out(tmp_path) -> None:
    workspace = Workspace(Path(tmp_path) / "ws2", enable_local_model_pull=False)
    assert "model.pull" not in workspace.registry.kinds()
