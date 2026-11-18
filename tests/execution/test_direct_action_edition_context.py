"""The direct action path composes execution from typed request state."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


from frisket.engine.store import Project
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace


def _direct_request(snapshot: dict[str, Any]) -> SimpleNamespace:
    direct = ExecutionCompositionContext.direct()
    return SimpleNamespace(
        state=SimpleNamespace(
            execution_composition_context=ExecutionCompositionContext(
                storage_key=None,
                run_id=None,
                trusted_job_organization_id=direct.trusted_job_organization_id,
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


def test_create_app_threads_import_child_admission_port_to_workspace(tmp_path) -> None:
    from frisket.server.app import create_app

    class Admission:
        def admit_import_child(self, **kwargs):
            raise AssertionError("ordinary app creation does not admit a child")

    admission = Admission()
    app = create_app(
        workspace_root=tmp_path / "workspace", import_child_admission_port=admission
    )
    assert app.state.workspace.import_child_admission_port is admission
