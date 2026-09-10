from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from frisket.execution.provider import ExecutionCompositionContext


def _find_action() -> dict[str, Any]:
    return {
        "action_id": "map.find",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "sheet_name": "Findings",
        "params": {
            "source": "body",
            "instruction": "Find every mention of Frisket.",
            "fields": [],
            "model": "openai/gpt-5-mini",
        },
        "idempotency_key": "map-find-runless-context@sha256:stable",
    }


def test_map_find_queue_persists_original_edition_context_on_receipt_replay(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.ai.llm import ModelRouter
    from frisket.engine.executor.action_inventory import ExecutorDeps
    from frisket.engine.jobs.queue import open_queue
    from frisket.engine.store import Project
    from frisket.execution.provider import open_execution_composition
    from frisket.server.action_enqueue import (
        QueuedV1ActionRunContext,
        queue_v1_action_run,
    )

    project_id = "runless-context"
    project = Project.create(tmp_path / f"{project_id}.frisket")
    queue = open_queue(workspace=tmp_path)
    router = ModelRouter(cache=None, cache_mode="off")
    # The subject is the receipt's edition context, not Find's scan admission;
    # stub the typed admission seam the reservation pins as evidence.
    monkeypatch.setattr(
        "frisket.engine.executor.find_action.prepare_typed_find_admission",
        lambda *_args, **_kwargs: {"schema_version": "frisket.map_find_admission.v1"},
    )
    ctx = QueuedV1ActionRunContext(
        queue=queue,
        workspace_root=tmp_path,
        router_for=lambda _project: router,
        execution_composition_for=open_execution_composition,
        active_runs={},
        run_jobs={},
        queue_payload_extra={},
        logger=logging.getLogger("test.runless-context"),
        request_executor_deps=ExecutorDeps,
    )
    original_context = {
        "funding_account_id": 17,
        "reservation_id": 29,
        "private_token": "opaque-to-public",
    }
    try:
        first = queue_v1_action_run(
            project,
            project_id,
            _find_action(),
            ctx=ctx,
            execution_context=ExecutionCompositionContext(
                storage_key=None,
                run_id=None,
                trusted_job_org_id=(
                    ExecutionCompositionContext.direct().trusted_job_org_id
                ),
                edition_snapshot=original_context,
            ),
        )
        assert first is not None and first.status == "queued"
        row = project.db.execute(
            "SELECT run_id, body, edition_run_context FROM receipts WHERE id=?",
            (first.receipt_id,),
        ).fetchone()
        assert row is not None
        assert row["run_id"] is None
        assert json.loads(row["edition_run_context"]) == original_context
        assert "edition_run_context" not in json.loads(row["body"])

        replay = queue_v1_action_run(
            project,
            project_id,
            _find_action(),
            ctx=ctx,
            execution_context=ExecutionCompositionContext(
                storage_key=None,
                run_id=None,
                trusted_job_org_id=(
                    ExecutionCompositionContext.direct().trusted_job_org_id
                ),
                edition_snapshot={"reservation_id": 999},
            ),
        )
        assert replay is not None
        assert replay.receipt_id == first.receipt_id
        assert replay.job_id == first.job_id
        preserved = project.db.execute(
            "SELECT edition_run_context FROM receipts WHERE id=?",
            (first.receipt_id,),
        ).fetchone()
        assert json.loads(preserved["edition_run_context"]) == original_context
    finally:
        queue.close()
        project.close()


class _RecordingSettlement:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def settle_run(self, **_kwargs: Any) -> None:
        pass

    def settle_action_receipt(self, **kwargs: Any) -> None:
        project = kwargs["project"]
        row = project.db.execute(
            "SELECT status, body, edition_run_context FROM receipts WHERE id=?",
            (kwargs["receipt_id"],),
        ).fetchone()
        assert row is not None
        body = json.loads(row["body"])
        model_call_ids = [
            str(call_id)
            for use in body["provider_use"]
            for call_id in use.get("model_call_ids", [])
        ]
        model_calls = [
            project.db.execute(
                "SELECT id, run_id FROM model_calls WHERE id=?", (call_id,)
            ).fetchone()
            for call_id in model_call_ids
        ]
        assert all(call is not None for call in model_calls)
        self.calls.append(
            {
                **{key: value for key, value in kwargs.items() if key != "project"},
                "status": row["status"],
                "edition_run_context": json.loads(row["edition_run_context"]),
                "provider_use": body["provider_use"],
                "model_calls": [(call["id"], call["run_id"]) for call in model_calls],
            }
        )


class _RefuseAdmission:
    def execution_denied(self, *, body: dict[str, Any]) -> bool:
        del body
        return True


def _model_call(call_id: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "fact_version": "frisket.model-call-fact.v1",
        "capability": "llm.complete",
        "engine": "gpt-5-mini",
        "provider": "openai",
        "provider_kind": "hosted_api",
        "model_ids": ["gpt-5-mini"],
        "credential_source": "platform_key",
        "provider_reported_cost_usd": 0.001,
        "provider_cost_usd": 0.001,
        "cost_source": "provider_reported",
        "units": {"input_tokens": 10, "output_tokens": 2},
        "cache": {},
        "request_id": None,
        "warnings": [],
        "duration_ms": 4,
    }


def _runless_handler_case(
    tmp_path,
    *,
    terminal_status: str,
    replay: bool = False,
    refuse_before_egress: bool = False,
) -> tuple[_RecordingSettlement, dict[str, Any]]:
    from frisket.contracts.action import ActionError, ActionResult, ActionSpec
    from frisket.engine.executor.action_jobs import (
        ActionJobEnvelope,
        reserve_queued_action_job_receipt,
    )
    from frisket.engine.jobs.ports import JobHandlerContext, WorkerPorts
    from frisket.engine.jobs.queue import (
        ACTION_RUN_KIND,
        CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY,
    )
    from frisket.engine.jobs.runs import register_action_run_handler
    from frisket.engine.jobs.worker import HandlerRegistry
    from frisket.engine.store import Project
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.store.runs import RunResultStore
    from frisket.project_identity import ProjectStorageKey

    trusted_org_id = 42
    trusted_project_id = f"runless-{terminal_status}"
    untrusted_project_id = "payload-project-must-not-fund"
    project = Project.create(tmp_path / f"{trusted_project_id}.frisket")
    action = ActionSpec(
        kind="test.runless_settlement",
        params={},
        idempotency_key=f"runless-settlement-{terminal_status}@sha256:stable",
    )
    context = {"reservation_id": 101, "funding_account_id": trusted_org_id}
    reservation = reserve_queued_action_job_receipt(
        project,
        action,
        project_id=trusted_project_id,
        edition_run_context=context,
    )
    assert isinstance(reservation, dict)
    receipt_id = reservation["receipt_id"]
    call_id = f"model-call-{terminal_status}"

    def write_provider_facts(target: Project, *, status: str) -> None:
        RunResultStore(target).write_unscoped_model_calls(
            [_model_call(call_id)], row_id=None, column_id=None
        )
        receipts = ReceiptStore(target)
        receipt = receipts.parsed_by_id(receipt_id)
        assert receipt is not None
        errors = (
            [
                ActionError(
                    code="provider_failed_after_egress",
                    message="provider failed after egress",
                    action_kind=action.kind,
                )
            ]
            if status == "failed"
            else []
        )
        updated = receipt.model_copy(
            update={
                "status": status,
                "provider_use": [
                    {
                        "capability": "llm.complete",
                        "model": "openai/gpt-5-mini",
                        "model_call_ids": [call_id],
                    }
                ],
                "errors": errors,
            }
        )
        assert receipts.update_body_status(updated, require_status=receipt.status)

    if replay:
        write_provider_facts(project, status=terminal_status)
    project.close()

    envelope = ActionJobEnvelope(
        action_kind=action.kind,
        action_id=reservation["action_id"],
        receipt_id=receipt_id,
        params_hash=reservation["params_hash"],
        idempotency_key=action.idempotency_key or "",
        project_id=untrusted_project_id,
        action=action.model_dump(mode="json", exclude_none=True),
    )
    payload = {
        "project_id": untrusted_project_id,
        "action_job": envelope.to_json(),
        CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY: ProjectStorageKey(
            storage_org_id=trusted_org_id,
            project_slug=trusted_project_id,
        ),
    }
    settlement = _RecordingSettlement()
    registry = HandlerRegistry()

    def executor(target: Project, env: ActionJobEnvelope) -> ActionResult:
        assert not replay
        write_provider_facts(
            target,
            status="running" if terminal_status == "failed" else terminal_status,
        )
        if terminal_status == "failed":
            raise RuntimeError("failed after provider egress")
        return ActionResult(
            action={"kind": env.action_kind, "action_id": env.action_id},
            status=terminal_status,
            project_id=env.project_id,
            receipt_id=env.receipt_id,
        )

    registry.register_action_executor(action.kind, executor)
    register_action_run_handler(
        registry,
        workspace_root=tmp_path,
        control_database_url="sqlite:///trusted-control.db",
        worker_ports=WorkerPorts(
            admission_port=_RefuseAdmission() if refuse_before_egress else None,
            settlement_port=settlement,
        ),
        require_storage_identity=True,
        workspace_root_storage_org_id=trusted_org_id,
    )
    handler = registry.get(ACTION_RUN_KIND)
    assert handler is not None
    result = handler(
        payload,
        JobHandlerContext.from_claimed_job(trusted_org_id=trusted_org_id),
    )
    return settlement, result


@pytest.mark.parametrize("terminal_status", ["completed", "partial", "failed"])
def test_runless_terminal_receipt_settlement_keeps_exact_unscoped_model_links(
    tmp_path, terminal_status: str
) -> None:
    settlement, result = _runless_handler_case(
        tmp_path, terminal_status=terminal_status
    )
    assert result["status"] == terminal_status
    assert len(settlement.calls) == 1
    [call] = settlement.calls
    assert call["project_id"] == f"runless-{terminal_status}"
    assert call["receipt_id"] == result["receipt_id"]
    assert call["trusted_job_org_id"] == 42
    assert call["control_database_url"] == "sqlite:///trusted-control.db"
    assert call["status"] == terminal_status
    assert call["edition_run_context"] == {
        "funding_account_id": 42,
        "reservation_id": 101,
    }
    assert call["provider_use"][0]["model_call_ids"] == [
        f"model-call-{terminal_status}"
    ]
    assert call["model_calls"] == [(f"model-call-{terminal_status}", None)]


def test_runless_terminal_replay_reinvokes_idempotent_receipt_settlement(
    tmp_path,
) -> None:
    settlement, result = _runless_handler_case(
        tmp_path, terminal_status="completed", replay=True
    )
    assert result["status"] == "completed"
    assert len(settlement.calls) == 1
    assert settlement.calls[0]["model_calls"] == [("model-call-completed", None)]


def test_runless_pre_egress_refusal_settles_terminal_receipt_without_model_facts(
    tmp_path,
) -> None:
    settlement, result = _runless_handler_case(
        tmp_path,
        terminal_status="failed",
        refuse_before_egress=True,
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "code_action_disabled"
    assert len(settlement.calls) == 1
    assert settlement.calls[0]["provider_use"] == []
    assert settlement.calls[0]["model_calls"] == []
