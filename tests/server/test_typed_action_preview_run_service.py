from __future__ import annotations

from pathlib import Path
from typing import Any

import ddgs
import pytest

from frisket.actions.core import RegisteredAction
from frisket.engine.executor import ExecutorDeps
from frisket.engine.runner import MapRunner
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.server.services.action_preview_runs import ActionPreviewRunService
from frisket.server.workspace import Workspace
from tests.deterministic_time import controlled_time


def _seed(
    workspace_root: Path,
    *,
    executor_deps_factory: Any = None,
    records: list[dict[str, str]] | None = None,
) -> tuple[Workspace, int, list[int]]:
    workspace = Workspace(
        workspace_root,
        enable_local_model_pull=False,
        executor_deps_factory=executor_deps_factory,
    )
    workspace.create("Typed preview", project_id="typed-preview")
    project = workspace.get("typed-preview")
    sheet_id = project.add_sheet("People")
    columns = {
        "first": project.add_column(sheet_id, "first"),
        "last": project.add_column(sheet_id, "last"),
    }
    row_ids = project.add_rows(
        sheet_id,
        records
        or [
            {"first": "Ada", "last": "Lovelace"},
            {"first": "Grace", "last": "Hopper"},
        ],
        columns,
    )
    return workspace, sheet_id, row_ids


def test_typed_template_preview_uses_explicit_program_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, sheet_id, row_ids = _seed(tmp_path / "workspace")
    project = workspace.get("typed-preview")
    service = ActionPreviewRunService(workspace)
    validations = 0
    bind_request = RegisteredAction.bind_request

    def count_validation(self, request):
        nonlocal validations
        validations += 1
        return bind_request(self, request)

    monkeypatch.setattr(RegisteredAction, "bind_request", count_validation)
    before = {
        "columns": len(project.columns(sheet_id)),
        "runs": project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        "receipts": project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
    }

    started = service.start_preview(
        "typed-preview",
        {
            "action_id": "map.template",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": row_ids,
            },
            "params": {"template": {"text": "{{last}}, {{first}}"}},
            "output_names": {"rendered": "display_name"},
            "idempotency_key": "typed-preview@1",
        },
    )

    assert started.status_code == 202, started.payload
    assert started.payload["total"] == len(row_ids)
    result = None

    def preview_finished() -> bool:
        nonlocal result
        result = service.get_preview("typed-preview", started.payload["preview_id"])
        return result.payload["status"] != "running"

    with controlled_time(timeout=5) as clock:
        clock.wait_until(preview_finished, message="typed preview did not finish")

    assert result is not None
    assert result.payload["status"] == "done", result.payload
    assert result.payload["result"]["columns"] == [
        {
            "name": "display_name",
            "column_type": "text",
            "format": None,
            "hidden": False,
            "overwrites_column_id": None,
        }
    ]
    rows = result.payload["result"]["rows"]
    assert result.payload["result"]["row_ids"] == row_ids
    assert result.payload["result"]["sampled"] == len(row_ids)
    assert result.payload["result"]["total"] == len(row_ids)
    assert "input_snapshot" not in result.payload["result"]
    assert rows[str(row_ids[0])]["display_name"]["value"] == "Lovelace, Ada"
    assert rows[str(row_ids[1])]["display_name"]["value"] == "Hopper, Grace"
    assert {
        "columns": len(project.columns(sheet_id)),
        "runs": project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        "receipts": project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
    } == before
    assert validations == 1


def test_web_search_preview_refuses_before_provider_egress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, sheet_id, _row_ids = _seed(tmp_path / "web-search")
    service = ActionPreviewRunService(workspace)

    def unexpected_provider(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("web-search preview must not reach DDGS")

    def unexpected_start(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("refused preview must not create a background job")

    monkeypatch.setattr(ddgs, "DDGS", unexpected_provider)
    monkeypatch.setattr(service._registry, "start", unexpected_start)

    response = service.start_preview(
        "typed-preview",
        {
            "action_id": "research.web_search",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"query": {"text": "{{first}} {{last}}"}, "max_results": 2},
            "output_names": {"search_results": "search_results"},
            "idempotency_key": "preview-web-search@1",
        },
    )

    assert response.status_code == 402
    error = response.payload["error"]
    assert error["code"] == "cost_gate"
    assert error["action_kind"] == "research.web_search"
    assert error["needs_confirmation"] is True
    assert error["details"]["promise_set_hash"]


@pytest.mark.parametrize("selection_size", [None, 21])
def test_typed_preview_bounds_semantic_scope_without_writes(
    tmp_path: Path,
    selection_size: int | None,
) -> None:
    records = [
        {"first": f"First {index}", "last": f"Last {index}"} for index in range(25)
    ]
    workspace, sheet_id, row_ids = _seed(
        tmp_path / f"scope-{selection_size}",
        records=records,
    )
    project = workspace.get("typed-preview")
    service = ActionPreviewRunService(workspace)
    selected = row_ids[2 : 2 + selection_size] if selection_size is not None else None
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if selected is not None:
        scope["row_ids"] = selected
    expected_scope = selected if selected is not None else row_ids
    before = {
        "columns": len(project.columns(sheet_id)),
        "runs": project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        "receipts": project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
    }

    started = service.start_preview(
        "typed-preview",
        {
            "action_id": "map.template",
            "scope": scope,
            "params": {"template": {"text": "{{last}}, {{first}}"}},
            "output_names": {"rendered": "display_name"},
            "idempotency_key": f"typed-bounded-preview@{selection_size}",
        },
    )

    assert started.status_code == 202, started.payload
    assert started.payload["total"] == len(expected_scope)
    result = None

    def preview_finished() -> bool:
        nonlocal result
        result = service.get_preview("typed-preview", started.payload["preview_id"])
        return result.payload["status"] != "running"

    with controlled_time(timeout=5) as clock:
        clock.wait_until(preview_finished, message="typed preview did not finish")

    assert result is not None
    assert result.payload["status"] == "done", result.payload
    preview = result.payload["result"]
    assert preview["row_ids"] == expected_scope[:20]
    assert preview["sampled"] == 20
    assert preview["total"] == len(expected_scope)
    assert {
        "columns": len(project.columns(sheet_id)),
        "runs": project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        "receipts": project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
    } == before


def test_typed_preview_uses_injected_factory_and_hosted_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runners: list[MapRunner] = []
    contexts: list[Any] = []

    def runner_factory(project: Any, router: Any) -> MapRunner:
        runner = MapRunner(
            project,
            router,
            authority=UnroutedOnlyAuthority(project),
        )
        runners.append(runner)
        return runner

    def deps_factory(_project_id: str, context: Any) -> ExecutorDeps:
        contexts.append(context)
        return ExecutorDeps(map_runner_factory=runner_factory)

    workspace, sheet_id, row_ids = _seed(
        tmp_path / "hosted-workspace",
        executor_deps_factory=deps_factory,
    )
    compositions: list[Any] = []
    original = workspace.execution_composition_for

    def composition_for(*args: Any, **kwargs: Any) -> Any:
        composition = original(*args, **kwargs)
        compositions.append(composition)
        return composition

    monkeypatch.setattr(workspace, "execution_composition_for", composition_for)
    context = object()
    started = ActionPreviewRunService(workspace).start_preview(
        "typed-preview",
        {
            "action_id": "map.template",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": row_ids,
            },
            "params": {"template": {"text": "{{last}}, {{first}}"}},
            "output_names": {"rendered": "display_name"},
            "idempotency_key": "typed-hosted-preview@1",
        },
        request_context=context,
    )

    assert started.status_code == 202, started.payload
    assert contexts == [context]
    assert len(runners) == len(compositions) == 1
    assert runners[0].execution_composition is compositions[0]


@pytest.mark.parametrize(
    ("action_id", "params", "error_code"),
    [
        (
            "resolve.substitute",
            {"source": "missing", "mapping": {"x": "y"}},
            "invalid_input_ref",
        ),
        (
            "resolve.fill_missing",
            {"source": "first", "method": "mean"},
            "invalid_params",
        ),
    ],
)
def test_column_transform_preview_refuses_source_errors_before_starting_a_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_id: str,
    params: dict[str, Any],
    error_code: str,
) -> None:
    workspace, sheet_id, _row_ids = _seed(tmp_path / error_code)
    service = ActionPreviewRunService(workspace)

    def unexpected_start(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("invalid preview must not create a background job")

    monkeypatch.setattr(service._registry, "start", unexpected_start)
    response = service.start_preview(
        "typed-preview",
        {
            "action_id": action_id,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": params,
            "output_names": {"cleaned": "cleaned"},
            "idempotency_key": f"{action_id}-invalid",
        },
    )

    assert response.status_code == 400
    assert response.payload["error"]["code"] == error_code


@pytest.mark.parametrize("gate", ["existing", "claimed"])
def test_column_transform_preview_refuses_output_gate_before_starting_a_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate: str,
) -> None:
    workspace, sheet_id, _row_ids = _seed(tmp_path / gate)
    project = workspace.get("typed-preview")
    if gate == "existing":
        project.add_column(sheet_id, "cleaned")
        expected_code = "output_column_exists"
    else:
        _claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["cleaned"],
            action_kind="test.blocker",
        )
        assert conflict is None
        expected_code = "output_column_busy"
    service = ActionPreviewRunService(workspace)

    def unexpected_start(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("invalid preview must not create a background job")

    monkeypatch.setattr(service._registry, "start", unexpected_start)
    response = service.start_preview(
        "typed-preview",
        {
            "action_id": "resolve.substitute",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"source": "first", "mapping": {"Ada": "Augusta"}},
            "output_names": {"cleaned": "cleaned"},
            "idempotency_key": f"preview-{gate}",
        },
    )

    assert response.status_code == 400
    assert response.payload["error"]["code"] == expected_code


def test_column_transform_preview_ignores_terminal_owner_stale_claim(
    tmp_path: Path,
) -> None:
    workspace, sheet_id, _row_ids = _seed(tmp_path / "stale-claim")
    project = workspace.get("typed-preview")
    op_id = project.append_op("test.seed", {"sheet_id": sheet_id})
    cursor = project.db.execute(
        "INSERT INTO runs (op_id, sheet_id, action_kind, status, model) "
        "VALUES (?, ?, 'test.seed', 'completed', 'local')",
        (op_id, sheet_id),
    )
    project.db.commit()
    _claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=["cleaned"],
        action_kind="test.seed",
        run_id=int(cursor.lastrowid),
    )
    assert conflict is None

    response = ActionPreviewRunService(workspace).start_preview(
        "typed-preview",
        {
            "action_id": "resolve.substitute",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"source": "first", "mapping": {"Ada": "Augusta"}},
            "output_names": {"cleaned": "cleaned"},
            "idempotency_key": "preview-stale-claim",
        },
    )

    assert response.status_code == 202, response.payload
    claim = project.db.execute(
        "SELECT status FROM output_column_claims WHERE output_name='cleaned'"
    ).fetchone()
    assert claim is not None and claim["status"] == "active"
