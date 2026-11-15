"""The queued owner closes cancellation and preserves existing terminal receipts."""

from contextlib import closing

import pytest

from frisket.ai.llm import LLMError, LLMResponse, ModelRouter
from frisket.engine.jobs.worker import Worker
from frisket.engine.runner.map_runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace


@pytest.mark.parametrize("mode", ["stop", "complete", "undo"])
def test_queued_provider_error_does_not_overwrite_run_terminal_state(
    tmp_path, monkeypatch, mode
):
    cancel = mode != "complete"
    root = tmp_path / "workspace"
    root.mkdir()
    pid = "queued-terminal"
    path = root / f"{pid}.frisket"
    with closing(Project.create(path)) as project:
        sheet = project.add_sheet("data")
        source = project.add_column(sheet, "source")
        project.add_rows(
            sheet,
            [{"source": str(i)} for i in range(1 if mode == "undo" else 3)],
            {"source": source},
        )

    class RateLimited:
        async def complete(self, request, client):
            if cancel:
                with closing(Project(path)) as project:
                    rid = project.db.execute(
                        "SELECT id FROM runs WHERE status='running'"
                    ).fetchone()[0]
                    if mode == "undo":
                        project.undo()
                    else:
                        RunResultStore(project).request_cancel(rid)
                if mode == "undo":
                    return LLMResponse(
                        content='{"answer":"returned"}',
                        data={"answer": "returned"},
                        tokens_in=1,
                        tokens_out=1,
                        cost=0.0001,
                        model=request.model,
                        provider="anthropic",
                    )
            raise LLMError("rate limited", status=429, retryable=True)

    router = ModelRouter(
        keys={"anthropic": "test"}, use_env_keys=False, cache_mode="off", max_retries=0
    )
    router._adapters["anthropic"] = RateLimited()
    monkeypatch.setattr(MapRunner, "_row_worker_count", lambda *_: 1)
    workspace = Workspace(root, router=router, enable_local_model_pull=False)
    body = {
        "action_id": "map.ask",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {
            "source": ["source"],
            "question": "describe",
            "model": "anthropic/claude-haiku-4-5",
        },
        "output_names": {"answer": "answer"},
        "idempotency_key": "queue-terminal",
    }
    try:
        response = ActionRunService(workspace).run_action(pid, body)
        if response.status_code == 402:
            body["confirmation"] = response.payload["errors"][0]["details"][
                "promise_set_hash"
            ]
            response = ActionRunService(workspace).run_action(pid, body)
        assert response.status_code == 200, response.payload
        launched = response.payload
        assert launched["status"] == "queued", launched
        worker = Worker(workspace.queue, workspace.registry, worker_id="terminal-test")
        for _ in range(12):
            job = workspace.queue.get(launched["job_id"])
            if job.status in {"done", "failed"}:
                break
            assert worker.run_once()
        assert job.status == "done", job
        project = workspace.get(pid)
        error_codes = [
            tuple(r)
            for r in project.db.execute(
                "SELECT error_code,error FROM results WHERE run_id=?",
                (launched["run_id"],),
            )
        ]
        if mode == "undo":
            assert error_codes == []
            assert (
                project.db.execute("SELECT count(*) FROM model_calls").fetchone()[0]
                == 1
            )
        elif mode == "stop":
            # The caller requested Stop before the provider returned its
            # rate-limit error. No result/usage came back from that request,
            # and the closed Change admits no new result publication.
            assert error_codes == []
            assert project.db.execute(
                "SELECT COUNT(*) FROM model_calls"
            ).fetchone()[0] == 0
        else:
            assert error_codes and all(
                r[0] == "provider_rate_limited" for r in error_codes
            ), error_codes
        row = project.db.execute(
            "SELECT status,current_attempt_id FROM runs WHERE id=?",
            (launched["run_id"],),
        ).fetchone()
        assert row["status"] == ("cancelled" if cancel else "completed"), (
            dict(row),
            job.result,
        )
        assert row["current_attempt_id"] is None
        receipt = project.db.execute(
            "SELECT status,body FROM receipts WHERE id=?", (launched["receipt_id"],)
        ).fetchone()
        assert receipt["status"] == ("cancelled" if cancel else "failed"), dict(receipt)
        assert not project.db.execute(
            "SELECT 1 FROM output_column_claims WHERE run_id=? AND status='active'",
            (launched["run_id"],),
        ).fetchone()
        from frisket.engine.executor.queued_actions import (
            queued_v1_finalize_action_result,
        )

        before = tuple(receipt)
        replay = queued_v1_finalize_action_result(
            project, job.payload, project_id=pid, run_id=launched["run_id"]
        )
        assert replay.status == receipt["status"]
        assert (
            tuple(
                project.db.execute(
                    "SELECT status,body FROM receipts WHERE id=?",
                    (launched["receipt_id"],),
                ).fetchone()
            )
            == before
        )
        assert not project.db.execute(
            "SELECT 1 FROM execution_attempts WHERE run_id=? AND state='dispatching'",
            (launched["run_id"],),
        ).fetchone()
    finally:
        workspace.queue.close()


@pytest.mark.parametrize("status", ["failed", "cancelled", "completed", "partial"])
def test_reserved_finalization_does_not_rewrite_terminal_receipt(tmp_path, status):
    from frisket.contracts.action import Receipt, ActionError
    from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
    from frisket.engine.executor.action_reservations import (
        _finalize_reserved_action_receipt,
    )
    from frisket.engine.store.receipts import ReceiptStore

    with closing(Project.create(tmp_path / "receipt.frisket")) as project:
        stored = Receipt(
            receipt_id="terminal",
            project_id="p",
            action_id="act",
            action_kind="map.template",
            idempotency_key="same",
            params_hash="hash",
            status=status,
            errors=[
                ActionError(
                    code="specific_reason", message="Preserve this exact reason"
                )
            ],
        )
        ReceiptStore(project).insert(stored)

        def forbidden():
            raise AssertionError("terminal replay must not release claims again")

        result = _finalize_reserved_action_receipt(
            project,
            _TypedProjectEnvelope("map.template", "same", {}),
            params_hash="hash",
            project_id="p",
            receipt=stored.model_copy(update={"status": "completed", "errors": []}),
            reservation_lost_message="lost",
            update_failed_message="failed",
            require_running_status=False,
            before_commit=forbidden,
        )
        assert result.status == status
        assert result.errors[0].code == "specific_reason"
        assert ReceiptStore(project).parsed_by_id("terminal") == stored
