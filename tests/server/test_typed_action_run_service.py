from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from frisket.actions.core import RegisteredAction
from frisket.engine.executor import ExecutorDeps
from frisket.engine.runner import MapRunner
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace


class _RecordingSettlement:
    def __init__(self) -> None:
        self.receipt_ids: list[str] = []

    def settle_action_receipt(self, **kwargs: Any) -> None:
        self.receipt_ids.append(str(kwargs["receipt_id"]))


def _seed_workspace(
    root: Path,
    *,
    executor_deps_factory: Any = None,
    settlement: Any = None,
) -> tuple[Workspace, int, list[int]]:
    workspace = Workspace(
        root,
        enable_local_model_pull=False,
        executor_deps_factory=executor_deps_factory,
        direct_action_receipt_settlement_port=settlement,
    )
    workspace.create("Typed actions", project_id="typed-actions")
    project = workspace.get("typed-actions")
    sheet_id = project.add_sheet("People")
    columns = {
        "first": project.add_column(sheet_id, "first"),
        "last": project.add_column(sheet_id, "last"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {"first": "Ada", "last": "Lovelace"},
            {"first": "Grace", "last": "Hopper"},
        ],
        columns,
    )
    return workspace, sheet_id, row_ids


def _template_request(sheet_id: int, row_ids: list[int]) -> dict[str, Any]:
    return {
        "action_id": "map.template",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            "row_ids": row_ids,
        },
        "params": {"template": {"text": "{{last}}, {{first}}"}},
        "output_names": {"rendered": "display_name"},
        "idempotency_key": "typed-template@1",
    }


def test_typed_action_uses_request_deps_and_root_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_context = object()
    deps_contexts: list[Any] = []
    runner_routers: list[Any] = []
    settlement = _RecordingSettlement()
    validations = 0
    bind_request = RegisteredAction.bind_request

    def count_validation(self, request):
        nonlocal validations
        validations += 1
        return bind_request(self, request)

    def runner_factory(project: Any, router: Any) -> MapRunner:
        runner_routers.append(router)
        return MapRunner(
            project,
            router,
            authority=UnroutedOnlyAuthority(project),
        )

    def deps_factory(_project_id: str, context: Any) -> ExecutorDeps:
        deps_contexts.append(context)
        return ExecutorDeps(map_runner_factory=runner_factory)

    workspace, sheet_id, row_ids = _seed_workspace(
        tmp_path / "workspace",
        executor_deps_factory=deps_factory,
        settlement=settlement,
    )

    monkeypatch.setattr(RegisteredAction, "bind_request", count_validation)

    service = ActionRunService(workspace)
    body = _template_request(sheet_id, row_ids)
    first = service.run_action(
        "typed-actions",
        body,
        request_context=request_context,
    )
    replay = service.run_action(
        "typed-actions",
        body,
        request_context=request_context,
    )

    assert first.status_code == 200, first.payload
    assert first.payload["status"] == "completed"
    assert first.payload["action"]["kind"] == "map.template"
    assert replay.payload == first.payload
    assert deps_contexts == [request_context]
    assert len(runner_routers) == 1
    assert settlement.receipt_ids == [
        first.payload["receipt_id"],
        first.payload["receipt_id"],
    ]
    assert validations == 2

    project = workspace.get("typed-actions")
    output = next(
        column
        for column in project.columns(sheet_id)
        if column["name"] == "display_name"
    )
    assert project.get_values(sheet_id, int(output["id"]), row_ids=row_ids) == {
        row_ids[0]: "Lovelace, Ada",
        row_ids[1]: "Hopper, Grace",
    }
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_invalid_typed_request_is_a_400_from_root_dispatch(
    tmp_path: Path,
) -> None:
    workspace, sheet_id, row_ids = _seed_workspace(tmp_path / "workspace")
    body = _template_request(sheet_id, row_ids)
    body["params"] = {}

    response = ActionRunService(workspace).run_action("typed-actions", body)

    assert response.status_code == 400
    assert response.payload["status"] == "failed"
    assert response.payload["action"]["kind"] == "map.template"
    assert response.payload["errors"][0]["code"] == "invalid_action_request"
    project = workspace.get("typed-actions")
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_legacy_envelope_is_invalid_from_root_dispatch(tmp_path: Path) -> None:
    workspace, _sheet_id, _row_ids = _seed_workspace(tmp_path / "workspace")

    response = ActionRunService(workspace).run_action(
        "typed-actions",
        {
            "schema_version": "frisket.action.v2",
            "kind": "import.files",
            "params": {},
        },
    )

    assert response.status_code == 400
    assert response.payload["status"] == "failed"
    assert response.payload["errors"][0]["code"] == "invalid_action_request"
    project = workspace.get("typed-actions")
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
