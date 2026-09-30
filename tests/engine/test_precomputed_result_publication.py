from __future__ import annotations

from pathlib import Path

import pytest

from frisket.contracts.action import Receipt
from frisket.engine.runner.publication import PUBLISH_VALUE
from frisket.engine.runner.result_generations import _compatibility_key
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore


def _seed_precomputed_run(
    tmp_path: Path,
) -> tuple[Project, RunResultStore, int, str, str, list[int], int]:
    project = Project.create(tmp_path / "precomputed.frisket", name="Precomputed")
    sheet_id = project.add_sheet("Findings")
    source_column_id = project.add_column(sheet_id, "source", type="text")
    output_column_id = project.add_column(
        sheet_id, "Match", type="text", ai_generated=True
    )
    row_ids = project.add_rows(
        sheet_id,
        [{"source": "one"}, {"source": "two"}],
        {"source": source_column_id},
    )
    op_id = project.append_op("map", {"recipe": "find"})
    store = RunResultStore(project)
    run_id = store.start_run(
        op_id,
        sheet_id,
        "map.find",
        row_ids=row_ids,
    )
    receipt_id = "receipt_precomputed"
    params_hash = "sha256:precomputed"
    ReceiptStore(project).insert(
        Receipt(
            receipt_id=receipt_id,
            project_id="project",
            action_id="action_precomputed",
            action_kind="map.find",
            run_id=run_id,
            params_hash=params_hash,
            status="running",
        )
    )
    claim_token = f"output-claim:{receipt_id}"
    claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=["Match"],
        action_kind="map.find",
        receipt_id=receipt_id,
        run_id=run_id,
        op_id=op_id,
        claim_token=claim_token,
    )
    assert conflict is None
    assert len(claims) == 1
    ResultGenerationStore(project).declare(
        run_id,
        output_column_id,
        output_role="Match",
        compatibility_key=_compatibility_key(
            field={"name": "Match", "column_type": "text"}
        ),
        write_mode="create",
        claim_token=claim_token,
    )
    return (
        project,
        store,
        run_id,
        receipt_id,
        claim_token,
        row_ids,
        output_column_id,
    )


def _precomputed_batch(row_ids: list[int], column_id: int) -> list[dict[str, object]]:
    return [
        {
            "row_id": row_id,
            "column_id": column_id,
            "value": f"match-{index}",
            "publication_effect": PUBLISH_VALUE,
        }
        for index, row_id in enumerate(row_ids, start=1)
    ]


def _publication_state(project: Project, run_id: int) -> tuple[int, int, int, int]:
    run = project.db.execute(
        "SELECT completed_rows, failed_rows FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert run is not None
    return (
        int(run["completed_rows"]),
        int(run["failed_rows"]),
        int(
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        ),
        int(
            project.db.execute(
                "SELECT COUNT(*) FROM cell_result_heads WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        ),
    )


def _model_call(call_id: str) -> dict[str, object]:
    return {
        "id": call_id,
        "fact_version": "frisket.model-call-fact.v1",
        "capability": "llm.complete",
        "engine": "fixture",
        "provider": "fixture",
        "provider_kind": "test",
        "model_ids": ["fixture-model"],
        "credential_source": "platform_key",
        "provider_reported_cost_usd": 0.01,
        "provider_cost_usd": 0.01,
        "cost_source": "provider_reported",
        "units": {"input_tokens": 3, "output_tokens": 1},
        "cache": {},
        "warnings": [],
        "duration_ms": 1,
    }


def test_precomputed_results_refuse_lost_receipt_or_claim_without_publication(
    tmp_path: Path,
) -> None:
    (
        project,
        store,
        run_id,
        receipt_id,
        claim_token,
        row_ids,
        column_id,
    ) = _seed_precomputed_run(tmp_path)
    try:
        batch = _precomputed_batch(row_ids, column_id)
        before = _publication_state(project, run_id)

        ReceiptStore(project).insert(
            Receipt(
                receipt_id="receipt_wrong_running",
                project_id="project",
                action_id="action_wrong_running",
                action_kind="map.find",
                run_id=run_id,
                params_hash="sha256:precomputed",
                status="running",
            )
        )
        with pytest.raises(RuntimeError, match="outside their output claim"):
            store.write_precomputed_results(
                run_id,
                batch,
                receipt_id="receipt_wrong_running",
                action_kind="map.find",
                params_hash="sha256:precomputed",
                claim_token=claim_token,
            )
        assert _publication_state(project, run_id) == before

        with pytest.raises(RuntimeError, match="outside their output claim"):
            store.write_precomputed_results(
                run_id,
                batch,
                receipt_id=receipt_id,
                action_kind="map.find",
                params_hash="sha256:precomputed",
                claim_token="output-claim:wrong",
            )
        assert _publication_state(project, run_id) == before

        ReceiptStore(project).update_body_status(
            ReceiptStore(project)
            .parsed_by_id(receipt_id)
            .model_copy(update={"status": "completed"}),
            require_status="running",
        )
        with pytest.raises(RuntimeError, match="lost their running action authority"):
            store.write_precomputed_results(
                run_id,
                batch,
                receipt_id=receipt_id,
                action_kind="map.find",
                params_hash="sha256:precomputed",
                claim_token=claim_token,
            )
        assert _publication_state(project, run_id) == before
    finally:
        project.close()


def test_attaching_neutral_model_call_facts_is_idempotent_and_atomic(
    tmp_path: Path,
) -> None:
    (
        project,
        store,
        run_id,
        receipt_id,
        _claim_token,
        row_ids,
        _column_id,
    ) = _seed_precomputed_run(tmp_path)
    try:
        store.write_unscoped_model_calls(
            [_model_call("call-owned"), _model_call("call-unattached")],
            row_id=None,
            column_id=None,
        )
        other_op_id = project.append_op("map", {"recipe": "other-find"})
        other_run_id = store.start_run(
            other_op_id,
                store.get_run(run_id)["sheet_id"],
            "map.find",
            row_ids=row_ids,
        )
        other_receipt_id = "receipt_other"
        ReceiptStore(project).insert(
            Receipt(
                receipt_id=other_receipt_id,
                project_id="project",
                action_id="action_other",
                action_kind="map.find",
                run_id=other_run_id,
                params_hash="sha256:other",
                status="running",
            )
        )
        store.write_unscoped_model_calls(
            [_model_call("call-foreign")], row_id=None, column_id=None
        )
        store.attach_unscoped_model_calls(
            other_run_id,
            ["call-foreign"],
            receipt_id=other_receipt_id,
            action_kind="map.find",
            params_hash="sha256:other",
        )

        with pytest.raises(RuntimeError, match="cannot move model-call facts"):
            store.attach_unscoped_model_calls(
                run_id,
                ["call-unattached", "call-foreign"],
                receipt_id=receipt_id,
                action_kind="map.find",
                params_hash="sha256:precomputed",
            )
        assert project.db.execute(
            "SELECT run_id FROM model_calls WHERE id='call-unattached'"
        ).fetchone()[0] is None
        assert store.model_calls(run_id) == []

        store.attach_unscoped_model_calls(
            run_id,
            ["call-owned"],
            receipt_id=receipt_id,
            action_kind="map.find",
            params_hash="sha256:precomputed",
        )
        store.attach_unscoped_model_calls(
            run_id,
            ["call-owned"],
            receipt_id=receipt_id,
            action_kind="map.find",
            params_hash="sha256:precomputed",
        )
        assert [call["id"] for call in store.model_calls(run_id)] == ["call-owned"]
        assert store.get_run(run_id)["cost_actual"] == pytest.approx(0.01)

        with pytest.raises(RuntimeError, match="cannot attach missing model-call"):
            store.attach_unscoped_model_calls(
                run_id,
                ["call-missing", "call-unattached"],
                receipt_id=receipt_id,
                action_kind="map.find",
                params_hash="sha256:precomputed",
            )
        assert project.db.execute(
            "SELECT run_id FROM model_calls WHERE id='call-unattached'"
        ).fetchone()[0] is None
        assert [call["id"] for call in store.model_calls(run_id)] == ["call-owned"]
        assert store.get_run(run_id)["cost_actual"] == pytest.approx(0.01)
    finally:
        project.close()
