"""The direct action path composes execution from typed request state."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from frisket.engine.store import Project
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace


class _RecordingDirectSettlement:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def settle_action_receipt(self, **kwargs: Any) -> None:
        project = kwargs["project"]
        row = project.db.execute(
            "SELECT status FROM receipts WHERE id=?",
            (kwargs["receipt_id"],),
        ).fetchone()
        assert not project.db.in_transaction
        assert row is not None and row["status"] == "completed"
        self.calls.append(
            {key: value for key, value in kwargs.items() if key != "project"}
        )
        if self.fail:
            raise RuntimeError("settlement unavailable")


def _direct_request(snapshot: dict[str, Any]) -> SimpleNamespace:
    direct = ExecutionCompositionContext.direct()
    return SimpleNamespace(
        state=SimpleNamespace(
            execution_composition_context=ExecutionCompositionContext(
                storage_key=None,
                run_id=None,
                trusted_job_org_id=direct.trusted_job_org_id,
                edition_snapshot=snapshot,
            )
        )
    )


def test_direct_action_factory_receives_the_opaque_request_context(tmp_path) -> None:
    observed = []

    def factory(project, router, context):
        observed.append(context)
        return open_execution_composition(project, router, context)

    workspace = Workspace(
        tmp_path / "workspace",
        enable_local_model_pull=False,
        execution_composition_factory=factory,
    )
    seeded = Project.create(
        workspace.root / "direct-context.frisket",
        name="direct-context",
    )
    seeded.close()
    project = workspace.get("direct-context")
    sheet_id = project.add_sheet("People")
    snapshot = {"reservation_id": 7, "owner": {"kind": "edition"}}
    request = _direct_request(snapshot)
    context = request.state.execution_composition_context

    response = ActionRunService(workspace).run_action(
        "direct-context",
        {
            "action_id": "row.add",
            "scope": {"kind": "project"},
            "params": {"sheet_id": sheet_id, "cells": {}},
            "idempotency_key": "direct-context@sha256:stable",
        },
        request_context=request,
    )

    assert response.status_code == 200, response.payload
    assert response.payload["status"] == "completed", response.payload
    assert observed
    assert all(item is context for item in observed)
    assert observed[-1].edition_snapshot == snapshot


def test_direct_settlement_failure_is_after_commit_and_replay_retries_it(
    tmp_path,
) -> None:
    settlement = _RecordingDirectSettlement(fail=True)
    workspace = Workspace(
        tmp_path / "workspace",
        enable_local_model_pull=False,
        direct_action_receipt_settlement_port=settlement,
    )
    seeded = Project.create(
        workspace.root / "direct-settlement.frisket",
        name="direct-settlement",
    )
    seeded.close()
    project = workspace.get("direct-settlement")
    sheet_id = project.add_sheet("People")
    body = {
        "action_id": "row.add",
        "scope": {"kind": "project"},
        "params": {"sheet_id": sheet_id, "cells": {}},
        "idempotency_key": "direct-settlement@sha256:stable",
    }
    service = ActionRunService(workspace)

    with pytest.raises(RuntimeError, match="settlement unavailable"):
        service.run_action(
            "direct-settlement",
            body,
            request_context=_direct_request({"reservation_id": 11}),
        )

    assert len(project.visible_row_ids(sheet_id)) == 1
    row = project.db.execute(
        "SELECT id, status FROM receipts WHERE idempotency_key=?",
        (body["idempotency_key"],),
    ).fetchone()
    assert row is not None and row["status"] == "completed"
    assert settlement.calls == [
        {
            "project_id": "direct-settlement",
            "receipt_id": row["id"],
        }
    ]

    settlement.fail = False
    replay = service.run_action(
        "direct-settlement",
        body,
        request_context=_direct_request({"reservation_id": 999}),
    )

    assert replay.status_code == 200
    assert replay.payload["receipt_id"] == row["id"]
    assert len(project.visible_row_ids(sheet_id)) == 1
    assert len(settlement.calls) == 2


def test_create_app_threads_direct_settlement_port_to_workspace(tmp_path) -> None:
    from frisket.server.app import create_app

    settlement = _RecordingDirectSettlement()
    app = create_app(
        workspace_root=tmp_path / "workspace",
        direct_action_receipt_settlement_port=settlement,
    )

    assert app.state.workspace.direct_action_receipt_settlement_port is settlement
