from __future__ import annotations

from frisket.engine.jobs import (
    HandlerRegistry,
    RUN_PROJECT_KIND,
    SqliteJobQueue,
    STRICT_STORAGE_HANDLER_ORIGIN,
    register_production_handlers,
)
from frisket.server.app import create_app


class _NoSilentReplacementRegistry(HandlerRegistry):
    def register(self, kind, fn, *args, **kwargs) -> None:
        if self.get(kind) is not None:
            raise ValueError(f"raw handler replacement is forbidden: {kind}")
        super().register(kind, fn, *args, **kwargs)


def test_strict_hosted_storage_composes_without_last_wins(tmp_path) -> None:
    queue = SqliteJobQueue(tmp_path / "queue.db", hosted=True)
    registry = _NoSilentReplacementRegistry()
    try:
        register_production_handlers(
            registry,
            workspace_root=tmp_path / "projects",
            queue=queue,
            require_storage_identity=True,
        )
        assert registry.get(RUN_PROJECT_KIND) is not None
    finally:
        queue.close()


def test_create_app_can_request_strict_storage_identity(tmp_path) -> None:
    cases = (
        (False, "frisket.production.project_run"),
        (True, STRICT_STORAGE_HANDLER_ORIGIN),
    )
    for require_storage_identity, expected_origin in cases:
        queue = SqliteJobQueue(
            tmp_path / f"queue-{require_storage_identity}.db", hosted=True
        )
        try:
            app = create_app(
                tmp_path / f"workspace-{require_storage_identity}",
                queue=queue,
                enable_provider_config=False,
                serve_spa=False,
                require_storage_identity=require_storage_identity,
            )
            assert (
                app.state.workspace.registry.registration(RUN_PROJECT_KIND).origin
                == expected_origin
            )
        finally:
            queue.close()
