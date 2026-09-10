"""Behavioral coverage for funding attribution on queued project runs."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from frisket.execution.provider import ExecutionCompositionContext
from frisket.server.action_enqueue import (
    QueuedV1ActionRunContext,
    _queue_v1_project_run_request,
)
from tests.engine.test_queue_schema_migrations import (
    _assert_actual_hosted_enqueue_producers_persist_storage_identity,
)


class _ReachedRouter(RuntimeError):
    pass


def test_funding_org_id_stays_injectable_queue_metadata() -> None:
    """Hosted composition may inject a funding owner distinct from storage."""

    def router_for(_project):
        raise _ReachedRouter

    ctx = QueuedV1ActionRunContext(
        queue=object(),  # type: ignore[arg-type]
        workspace_root=Path("."),
        router_for=router_for,
        execution_composition_for=lambda _project, _router, _context: None,  # type: ignore[arg-type]
        active_runs={},
        run_jobs={},
        queue_payload_extra={"org_id": 91},
        logger=logging.getLogger("test.queue_funding_attribution"),
    )
    request = SimpleNamespace(entry=SimpleNamespace(payload_codecs=()))

    # The router boundary immediately follows the protected-key merge. If
    # ``org_id`` becomes protected, this raises ValueError before reaching it.
    with pytest.raises(_ReachedRouter):
        _queue_v1_project_run_request(
            object(),  # type: ignore[arg-type]
            "cross-funded-project",
            {},
            ctx=ctx,
            execution_context=ExecutionCompositionContext.direct(),
            request=request,  # type: ignore[arg-type]
        )


def test_actual_hosted_enqueue_producers_persist_storage_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "actual-producers.frisket"
    bundle.mkdir()
    (bundle / "project.db").touch()
    (bundle / "manifest.json").touch()
    _assert_actual_hosted_enqueue_producers_persist_storage_identity(
        monkeypatch=monkeypatch,
        storage_root=tmp_path,
        project_id="actual-producers",
        storage_org_id=23,
    )
