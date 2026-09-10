from __future__ import annotations

from pathlib import Path

from frisket.contracts.action import Receipt
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore


def _seed_project(tmp_path: Path) -> tuple[Project, int, dict[str, int]]:
    project = Project.create(tmp_path / "claims.frisket", name="Claims")
    sheet_id = project.add_sheet("People")
    columns = {
        "name": project.add_column(sheet_id, "name", type="text"),
        "summary": project.add_column(sheet_id, "summary", type="text"),
    }
    return project, sheet_id, columns


def test_output_column_claim_store_preserves_claim_lifecycle(
    tmp_path: Path,
) -> None:
    project, sheet_id, columns = _seed_project(tmp_path)
    ReceiptStore(project).insert(
        Receipt(
            receipt_id="receipt_1",
            project_id="proj_claims",
            action_id="act_1",
            action_kind="map.template",
            idempotency_key="claim-test",
            params_hash="sha256:claim-test",
            status="running",
        )
    )
    store = OutputColumnClaimStore(project)

    claims, conflict = store.acquire(
        sheet_id=sheet_id,
        output_names=["summary"],
        action_kind="map.template",
        receipt_id="receipt_1",
        claim_token="claim:store",
        details={"source": "store-test"},
    )

    assert conflict is None
    assert len(claims) == 1
    assert claims[0]["column_id"] == columns["summary"]
    assert claims[0]["receipt_id"] == "receipt_1"
    assert claims[0]["details"] == '{"source": "store-test"}'

    _other_claims, conflict = store.acquire(
        sheet_id=sheet_id,
        output_names=["summary"],
        action_kind="map.template",
        claim_token="claim:delegate-conflict",
    )
    assert conflict is not None
    assert conflict["output_name"] == "summary"
    assert conflict["receipt_id"] == "receipt_1"

    op_id = project.append_op("fixture", {"kind": "claim-bind"}, label="claim bind")
    run_id = RunResultStore(project).start_run(
        op_id, sheet_id, "map.template", total_rows=0
    )
    store.bind_to_run(claim_token="claim:store", run_id=run_id, job_id=42)
    bound = store.active_for_columns([columns["summary"]])
    assert bound is not None
    assert bound["run_id"] == run_id
    assert bound["job_id"] == 42
    assert bound["op_id"] == op_id

    store.renew(claim_token="claim:store", lease_seconds=5)
    renewed = store.active_for_columns([columns["summary"]])
    assert renewed is not None
    assert renewed["lease_expires_at"] is not None
    assert renewed["renewed_at"] is not None

    store.release(claim_token="claim:store")
    assert store.active_for_columns([columns["summary"]]) is None


def test_output_column_claim_store_releases_by_receipt_and_claim_token(
    tmp_path: Path,
) -> None:
    project, sheet_id, _columns = _seed_project(tmp_path)
    ReceiptStore(project).insert(
        Receipt(
            receipt_id="receipt_release",
            project_id="proj_claims",
            action_id="act_release",
            action_kind="map.template",
            idempotency_key="receipt-release",
            params_hash="sha256:receipt-release",
            status="running",
        )
    )
    ReceiptStore(project).insert(
        Receipt(
            receipt_id="receipt_release_again",
            project_id="proj_claims",
            action_id="act_release_again",
            action_kind="map.template",
            idempotency_key="receipt-release-again",
            params_hash="sha256:receipt-release-again",
            status="running",
        )
    )
    store = OutputColumnClaimStore(project)

    claims, conflict = store.acquire(
        sheet_id=sheet_id,
        output_names=["summary"],
        action_kind="map.template",
        receipt_id="receipt_release",
        claim_token="claim:receipt-release",
    )
    assert conflict is None
    assert claims
    assert (
        store.release_for_claim_and_receipt(
            claim_token="claim:other",
            receipt_id="receipt_release",
            status="failed",
        )
        == 0
    )
    assert (
        store.release_for_claim_and_receipt(
            claim_token="claim:receipt-release",
            receipt_id="receipt_release",
            status="failed",
        )
        == 1
    )
    assert store.active_for_columns([]) is None

    claims, conflict = store.acquire(
        sheet_id=sheet_id,
        output_names=["summary"],
        action_kind="map.template",
        receipt_id="receipt_release_again",
        claim_token="claim:receipt-release-again",
    )
    assert conflict is None
    assert claims
    assert (
        store.release_for_receipt(
            receipt_id="receipt_release_again",
            status="cancelled",
        )
        == 1
    )
