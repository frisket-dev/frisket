import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import typed_action_for_request
from frisket.actions.types import ActionRequest
from frisket.contracts.action import ActionResult
from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
from frisket.engine.executor.action_jobs import (
    ActionJobEnvelope,
    mark_action_job_enqueued,
)
from frisket.engine.executor.action_reservations import _reserve_running_action_receipt
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.find_action import reserve_typed_find_action_job
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


@pytest.mark.parametrize(
    "kind",
    [
        "reduce.group_summary",
        "derive.transcript_segments",
        "derive.temporal_segments",
        "temporal.extract_range",
    ],
)
def test_direct_typed_host_running_errors_are_advertised(tmp_path, kind):
    entry = ACTION_REGISTRY.get(kind).catalog_entry()
    request = entry["examples"][0]
    bound = typed_action_for_request(request)
    project = Project.create(tmp_path / "running.frisket")
    try:
        # An admitted request already owns the receipt. The duplicate must stop
        # before resolving inputs, allocating outputs, or starting paid work.
        reservation = _reserve_running_action_receipt(
            project,
            _TypedProjectEnvelope(
                kind, bound.request.idempotency_key, request["params"]
            ),
            params_hash=typed_request_hash(bound),
            project_id="running",
            reservation_kind="test_admitted_action",
        )
        assert isinstance(reservation, dict)
        before = tuple(project.db.iterdump())
        running = run_action_spec(project, request, project_id="running")
        assert running.status == "failed"
        assert [error.code for error in running.errors] == ["idempotency_in_progress"]
        assert tuple(project.db.iterdump()) == before

        project.db.execute(
            "UPDATE receipts SET created_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", reservation["receipt_id"]),
        )
        project.db.commit()
        stale = run_action_spec(project, request, project_id="running")
        assert stale.status == "failed"
        assert [error.code for error in stale.errors] == ["idempotency_stale_running"]
        assert ReceiptStore(project).find_by_id(reservation["receipt_id"]) is None
        advertised = {error["code"] for error in entry["errors"]}
        assert {error.code for error in (*running.errors, *stale.errors)} <= advertised
    finally:
        project.close()


def test_find_queue_duplicates_do_not_advertise_direct_running_errors(tmp_path):
    project = Project.create(tmp_path / "queued.frisket")
    try:
        sheet = project.add_sheet("Reports")
        column = project.add_column(sheet, "body", type="text")
        project.add_rows(sheet, [{"body": "A public report."}], {"body": column})
        request = ActionRequest.model_validate(
            {
                "action_id": "map.find",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {
                    "source": "body",
                    "model": "anthropic/claude-haiku-4-5",
                    "instruction": "Find public reports.",
                },
                "sheet_name": "Findings",
                "idempotency_key": "queued-find",
            }
        )

        def reserve(candidate):
            return reserve_typed_find_action_job(
                project,
                "queued",
                typed_action_for_request(candidate.model_dump(mode="json")),
            )

        envelope = reserve(request)
        if isinstance(envelope, ActionResult):
            assert envelope.status == "needs_confirmation", envelope.errors
            request = request.model_copy(
                update={"confirmation": envelope.errors[0].details["promise_set_hash"]}
            )
            envelope = reserve(request)
        assert isinstance(envelope, ActionJobEnvelope)
        mark_action_job_enqueued(
            project, receipt_id=envelope.receipt_id, job_id=7, job_kind="action.run"
        )
        before = tuple(project.db.iterdump())
        duplicate = reserve(request)
        assert isinstance(duplicate, ActionResult)
        assert duplicate.status == "queued"
        assert duplicate.receipt_id == envelope.receipt_id
        assert duplicate.errors == []
        assert tuple(project.db.iterdump()) == before

        conflict = reserve(
            request.model_copy(
                update={
                    "params": {**request.params, "instruction": "Find something else."}
                }
            )
        )
        assert isinstance(conflict, ActionResult)
        assert [error.code for error in conflict.errors] == ["idempotency_conflict"]
        assert tuple(project.db.iterdump()) == before
        advertised = {
            error["code"]
            for error in ACTION_REGISTRY.get("map.find").catalog_entry()["errors"]
        }
        assert "idempotency_conflict" in advertised
        assert advertised.isdisjoint(
            {"idempotency_in_progress", "idempotency_stale_running"}
        )
    finally:
        project.close()
