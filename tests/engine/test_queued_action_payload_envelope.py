from __future__ import annotations

from pathlib import Path

from frisket.contracts.action import Receipt
from frisket.engine.executor.queued_actions import (
    queued_v1_action_request,
    queued_v1_job_payload,
    queued_v1_run_authorizes_action_lifecycle,
)
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _classify_action() -> dict:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "context": "Classify local-government news.",
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": ["accountability", "infrastructure"],
                }
            ],
        },
        "idempotency_key": "v1-queued-envelope@sha256:stable",
    }


def test_action_lifecycle_worker_authorization_binds_run_receipt_marker_and_spec(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "worker-authorization.frisket", name="worker")
    try:
        sheet_id = project.add_sheet("Stories")
        story_id = project.add_column(sheet_id, "story")
        action_body = _classify_action()
        request = queued_v1_action_request(action_body)
        assert request is not None
        reservation = {
            "action_id": "act_worker_auth",
            "receipt_id": "receipt_worker_auth",
            "params_hash": request.expected_params_hash,
            "input_column_ids": {"story": story_id},
            "input_column_types": {"story": "text"},
            "output_names": request.expected_runner_spec["output_names"],
            "output_target_preconditions": {},
        }
        marked_spec = request.entry.mark_runner_spec(
            request.expected_runner_spec,
            receipt_id=reservation["receipt_id"],
            action_id=reservation["action_id"],
            params_hash=reservation["params_hash"],
        )
        op_id = project.append_op("map", marked_spec)
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.classify",
            params=marked_spec,
        )
        ReceiptStore(project).insert_queued(
            Receipt(
                receipt_id=reservation["receipt_id"],
                project_id="worker-auth",
                action_id=reservation["action_id"],
                action_kind="map.classify",
                run_id=run_id,
                idempotency_key="worker-auth@sha256:stable",
                params_hash=reservation["params_hash"],
                status="queued",
            )
        )
        payload = {
            "project_id": "worker-auth",
            "run_id": run_id,
            "spec": marked_spec,
            "v1_cache_mode": "replay",
            **queued_v1_job_payload(request, action_body, reservation, run_id=12),
        }

        assert queued_v1_run_authorizes_action_lifecycle(
            project, payload, run_id=run_id
        )

        forged_payload = {
            **payload,
            "spec": {**marked_spec, "action_kind": "map.extract"},
        }
        assert not queued_v1_run_authorizes_action_lifecycle(
            project, forged_payload, run_id=run_id
        )
    finally:
        project.close()
