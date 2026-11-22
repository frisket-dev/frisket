from __future__ import annotations

from typing import Any

import pytest

from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.writer_fence import WriterFence
from frisket.execution.attempt import StaleAttemptWriter
from helpers import write_claimed_test_results


def test_e2e_result_seeder_uses_one_immutable_claimed_writer(
    make_project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project("seed-writer")
    sheet_id = project.add_sheet("Docs")
    source_column_id = project.add_column(sheet_id, "Source")
    output_column_id = project.add_column(
        sheet_id,
        "Finding",
        generated=True,
    )
    row_id = project.add_rows(
        sheet_id,
        [{"Source": "record.pdf"}],
        {"Source": source_column_id},
    )[0]
    op_id = project.append_change("map.extract")
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.extract",
        row_ids=[row_id],
    )
    batch = [
        {
            "row_id": row_id,
            "column_id": output_column_id,
            "value": "Budget approved",
        }
    ]

    with pytest.raises(StaleAttemptWriter, match="no immutable writer"):
        RunResultStore(project).write_results(run_id, batch)

    observed_authority: dict[str, Any] = {}
    original_write_results = RunResultStore.write_results

    def capture_authority(
        store: RunResultStore,
        captured_run_id: int,
        captured_batch: list[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        observed_authority.update(kwargs)
        original_write_results(
            store,
            captured_run_id,
            captured_batch,
            **kwargs,
        )

    monkeypatch.setattr(RunResultStore, "write_results", capture_authority)
    write_claimed_test_results(project, run_id, batch)

    fence = observed_authority["fence"]
    assert isinstance(fence, WriterFence)
    writer_attempt_id = fence.writer_attempt_id
    assert writer_attempt_id
    assert fence.claim_token
    assert observed_authority == {
        "fence": WriterFence(
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=fence.claim_token,
            claimless_direct_effect=False,
            authorized_attempt_id=writer_attempt_id,
        )
    }
    assert (
        project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?",
            (writer_attempt_id,),
        ).fetchone()["state"]
        == "effected"
    )
    assert (
        project.db.execute(
            "SELECT status FROM generated_column_claims WHERE claim_token=?",
            (fence.claim_token,),
        ).fetchone()["status"]
        == "released"
    )
    stored = project.db.execute(
        "SELECT value_kind,value FROM results "
        "WHERE run_id=? AND row_id=? AND column_id=?",
        (run_id, row_id, output_column_id),
    ).fetchone()
    assert (stored["value_kind"], stored["value"]) == (
        "text",
        "Budget approved",
    )
