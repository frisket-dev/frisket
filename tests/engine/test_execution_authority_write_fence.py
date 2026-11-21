from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest

from frisket.contracts.action import ActionError, Receipt
from frisket.engine.jobs.queue import SqliteJobQueue
from frisket.engine.runner.finalization import finalize_dispatched_run
from frisket.engine.store import Project
from frisket.engine.store.effect_checkpoints import (
    EffectCheckpointRef,
    EffectCheckpointStore,
    row_effect_ref,
)
from frisket.engine.store.output_claims import (
    OutputColumnClaimStore,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.writer_fence import WriterFence
from frisket.execution.attempt import (
    StaleAttemptWriter,
    abandon_stale_dispatching_attempts,
)
from frisket.engine.store.transactions import write_transaction


def _seed_claimed_run(
    tmp_path: Path,
    *,
    name: str = "execution-authority",
) -> tuple[Project, int, list[int], int, str]:
    project = Project.create(tmp_path / f"{name}.frisket")
    sheet_id = project.add_sheet("data")
    source_column_id = project.add_column(sheet_id, "source")
    output_column_id = project.add_column(
        sheet_id,
        "python_result",
        ai_generated=True,
    )
    row_ids = project.add_rows(
        sheet_id,
        [{"source": "one"}, {"source": "two"}, {"source": "three"}],
        {"source": source_column_id},
    )
    op_id = project.append_change("map", {"recipe": "python"})
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.python",
        row_ids=row_ids,
    )
    receipt_id = f"receipt_{name.replace('-', '_')}"
    claim_token = f"output-claim:{receipt_id}"
    ReceiptStore(project).insert(
        Receipt(
            receipt_id=receipt_id,
            project_id="project",
            action_id=f"action_{name}",
            action_kind="map.python",
            status="running",
            run_id=run_id,
        )
    )
    with write_transaction(project.db):
        claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["python_result"],
            action_kind="map.python",
            receipt_id=receipt_id,
            run_id=run_id,
            claim_token=claim_token,
            lease_seconds=int(timedelta(hours=6).total_seconds()),
        )
    assert conflict is None
    assert len(claims) == 1
    with write_transaction(project.db):
        OutputColumnClaimStore(project).bind_to_run(
            claim_token=claim_token,
            run_id=run_id,
        )
    return project, run_id, row_ids, output_column_id, claim_token


def _insert_attempt(
    project: Project,
    *,
    attempt_id: str,
    run_id: int,
    seq: int,
    state: str,
    age_hours: int = 0,
) -> None:
    project.db.execute(
        "INSERT INTO execution_attempts "
        "(id, run_id, seq, state, action_identity_hash, scope_json, created_at) "
        "VALUES (?, ?, ?, ?, 'test-action', '[]', "
        "datetime('now', ? || ' hours'))",
        (attempt_id, run_id, seq, state, -age_hours),
    )
    if state == "dispatching":
        project.db.execute(
            "UPDATE runs SET current_attempt_id=? WHERE id=?",
            (attempt_id, run_id),
        )
    project.db.commit()


def test_pre_dispatch_cleanup_preserves_live_dispatching_authority(
    tmp_path: Path,
) -> None:
    from frisket.engine.executor.action_lifecycle import (
        _terminalize_unclaimed_prepared_run,
    )

    project, run_id, _rows, _column_id, claim_token = _seed_claimed_run(
        tmp_path,
        name="pre-dispatch-live-writer",
    )
    with closing(project):
        _insert_attempt(
            project,
            attempt_id="attempt_live",
            run_id=run_id,
            seq=0,
            state="dispatching",
        )

        with pytest.raises(StaleAttemptWriter):
            _terminalize_unclaimed_prepared_run(
                project,
                SimpleNamespace(run_id=run_id),
                receipt_id="receipt_pre_dispatch_live_writer",
                error=ActionError(code="setup_failed", message="setup failed"),
            )

        run = project.db.execute(
            "SELECT status, current_attempt_id FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert run is not None
        assert tuple(run) == ("running", "attempt_live")
        assert (
            project.db.execute(
                "SELECT state FROM execution_attempts WHERE id='attempt_live'"
            ).fetchone()[0]
            == "dispatching"
        )
        assert (
            project.db.execute(
                "SELECT status FROM output_column_claims WHERE claim_token=?",
                (claim_token,),
            ).fetchone()[0]
            == "active"
        )


@pytest.mark.parametrize(
    "state", ["dispatching", "abandoned", "halted", "effected", "two_pending"]
)
def test_unbound_preparation_refusal_preserves_reservation(make_project, state):
    from frisket.engine.executor.action_lifecycle import (
        _terminalize_unclaimed_prepared_run,
    )

    project = make_project("unbound")
    sheet = project.add_sheet("source")
    op = project.append_change("map", {"recipe": "python"})
    run_id = RunResultStore(project).start_run(op, sheet, "map.python", row_ids=[])
    ReceiptStore(project).insert(
        Receipt(
            receipt_id="receipt_unbound",
            project_id="project",
            action_id="action_unbound",
            action_kind="map.python",
            status="running",
        )
    )
    for seq, attempt_state in enumerate(
        ["created", "admitted"] if state == "two_pending" else [state]
    ):
        _insert_attempt(
            project,
            attempt_id=f"attempt_{seq}",
            run_id=run_id,
            seq=seq,
            state=attempt_state,
        )
    before = tuple(project.db.iterdump())
    with pytest.raises(StaleAttemptWriter, match="dispatch history"):
        _terminalize_unclaimed_prepared_run(
            project,
            SimpleNamespace(run_id=run_id),
            receipt_id="receipt_unbound",
            error=ActionError(code="setup_failed", message="setup failed"),
        )
    assert not project.db.in_transaction
    assert tuple(project.db.iterdump()) == before


def _replace_attempt(
    project: Project,
    *,
    run_id: int,
    old_attempt_id: str,
    new_attempt_id: str,
) -> None:
    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute(
        "UPDATE execution_attempts SET state='abandoned' WHERE id=?",
        (old_attempt_id,),
    )
    project.db.execute(
        "INSERT INTO execution_attempts "
        "(id, run_id, seq, state, action_identity_hash, scope_json, created_at) "
        "VALUES (?, ?, 1, 'dispatching', 'test-action', '[]', datetime('now'))",
        (new_attempt_id, run_id),
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id=? WHERE id=?",
        (new_attempt_id, run_id),
    )
    project.db.commit()


def _result_batch(row_id: int, column_id: int, value: str) -> list[dict[str, Any]]:
    return [{"row_id": row_id, "column_id": column_id, "value": value}]


def _fact_batch(row_id: int, column_id: int, call_id: str) -> list[dict[str, Any]]:
    return [
        {
            "row_id": row_id,
            "column_id": column_id,
            "model_calls": [
                {
                    "id": call_id,
                    "fact_version": "frisket.model-call-fact.v1",
                    "capability": "classify",
                    "engine": "fixture",
                    "provider": "fixture",
                    "provider_kind": "test",
                    "model_ids": ["fixture-model"],
                    "credential_source": "platform_key",
                    "provider_cost_usd": None,
                }
            ],
        }
    ]


def _authority_snapshot(project: Project, run_id: int) -> dict[str, Any]:
    return {
        "results": project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=?",
            (run_id,),
        ).fetchone()[0],
        "facts": project.db.execute(
            "SELECT COUNT(*) FROM model_calls WHERE run_id=?",
            (run_id,),
        ).fetchone()[0],
        "checkpoints": [
            tuple(row)
            for row in project.db.execute(
                "SELECT id, state, authorized_attempt_id, payload "
                "FROM effect_checkpoints ORDER BY id"
            ).fetchall()
        ],
        "run": tuple(
            project.db.execute(
                "SELECT status, current_attempt_id, cost_actual FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
        ),
        "claims": [
            tuple(row)
            for row in project.db.execute(
                "SELECT id, run_id, claim_token, status "
                "FROM output_column_claims WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        ],
    }


def test_just_claimed_attempt_survives_an_immediate_sweep(tmp_path: Path) -> None:
    project, run_id, _rows, _column_id, _claim_token = _seed_claimed_run(tmp_path)
    with closing(project):
        _insert_attempt(
            project,
            attempt_id="attempt_just_claimed",
            run_id=run_id,
            seq=0,
            state="dispatching",
            age_hours=7,
        )
        lease = project.db.execute(
            "SELECT renewed_at, lease_expires_at FROM output_column_claims "
            "WHERE run_id=? AND status='active'",
            (run_id,),
        ).fetchone()
        assert lease is not None
        assert lease["lease_expires_at"] is not None

        with write_transaction(project.db):
            assert (
                abandon_stale_dispatching_attempts(
                    project,
                    run_id,
                    max_age=timedelta(hours=6),
                )
                == 0
            )
        assert (
            project.db.execute(
                "SELECT state FROM execution_attempts WHERE id='attempt_just_claimed'"
            ).fetchone()[0]
            == "dispatching"
        )


def test_terminal_queue_job_cannot_release_a_live_claimed_writer(
    tmp_path: Path,
) -> None:
    """Queue status is not execution liveness.

    A queue lease/renewal failure may terminalize the queue row while the
    project-local writer still holds both parts of authority: the current
    dispatching attempt and its unexpired output-claim lease.  An unrelated
    output-busy check must not turn that queue projection into permission to
    release the live writer's claim.
    """

    project, run_id, _rows, column_id, claim_token = _seed_claimed_run(
        tmp_path,
        name="terminal-job-live-writer",
    )
    queue = SqliteJobQueue(tmp_path / "terminal-job-live-writer.queue.db")
    try:
        _insert_attempt(
            project,
            attempt_id="attempt_live_after_queue_terminal",
            run_id=run_id,
            seq=0,
            state="dispatching",
        )
        job_id = queue.enqueue(
            "project.run",
            {"project_id": "project", "run_id": run_id},
        )
        claimed = queue.claim("terminal-job-worker", lease_seconds=60.0)
        assert claimed is not None and claimed.id == job_id
        assert queue.complete(
            job_id,
            "terminal-job-worker",
            result={"status": "queue-terminal"},
        )
        project.db.execute(
            "UPDATE output_column_claims SET job_id=? WHERE run_id=? AND claim_token=?",
            (job_id, run_id, claim_token),
        )
        project.db.commit()

        lease = project.db.execute(
            "SELECT lease_expires_at FROM output_column_claims "
            "WHERE run_id=? AND claim_token=? AND status='active'",
            (run_id, claim_token),
        ).fetchone()
        assert lease is not None and lease["lease_expires_at"] is not None
        blocker = OutputColumnClaimStore(project, queue=queue).active_for_columns(
            [column_id]
        )

        assert blocker is not None
        assert blocker["claim_token"] == claim_token
        assert (
            project.db.execute(
                "SELECT status FROM output_column_claims "
                "WHERE run_id=? AND claim_token=?",
                (run_id, claim_token),
            ).fetchone()[0]
            == "active"
        )
        assert (
            project.db.execute(
                "SELECT current_attempt_id FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()[0]
            == "attempt_live_after_queue_terminal"
        )
    finally:
        queue.close()
        project.close()


def test_map_python_claim_lease_is_liveness_without_model_calls(
    tmp_path: Path,
) -> None:
    project, run_id, row_ids, column_id, claim_token = _seed_claimed_run(tmp_path)
    with closing(project):
        _insert_attempt(
            project,
            attempt_id="attempt_map_python",
            run_id=run_id,
            seq=0,
            state="dispatching",
            age_hours=7,
        )
        project.db.execute(
            "UPDATE output_column_claims "
            "SET lease_expires_at=datetime('now', '-1 second') "
            "WHERE run_id=? AND status='active'",
            (run_id,),
        )
        project.db.commit()

        for index, row_id in enumerate(row_ids[:2]):
            RunResultStore(project).write_results(
                run_id,
                _result_batch(row_id, column_id, f"batch-{index}"),
                fence=WriterFence.claimed(
                    run_id=run_id,
                    writer_attempt_id="attempt_map_python",
                    claim_token=claim_token,
                ),
            )
            with write_transaction(project.db):
                assert (
                    abandon_stale_dispatching_attempts(
                        project,
                        run_id,
                        max_age=timedelta(hours=6),
                    )
                    == 0
                )

        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM model_calls WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            == 0
        )
        project.db.execute(
            "UPDATE output_column_claims "
            "SET lease_expires_at=datetime('now', '-1 second') "
            "WHERE run_id=? AND status='active'",
            (run_id,),
        )
        project.db.commit()
        with write_transaction(project.db):
            assert (
                abandon_stale_dispatching_attempts(
                    project,
                    run_id,
                    max_age=timedelta(hours=6),
                )
                == 1
            )
        _insert_attempt(
            project,
            attempt_id="attempt_map_python_b",
            run_id=run_id,
            seq=1,
            state="dispatching",
        )
        with write_transaction(project.db):
            OutputColumnClaimStore(project).renew(
                claim_token=claim_token,
                run_id=run_id,
                lease_seconds=int(timedelta(hours=6).total_seconds()),
            )
        RunResultStore(project).write_results(
            run_id,
            _result_batch(row_ids[2], column_id, "replacement"),
            fence=WriterFence.claimed(
                run_id=run_id,
                writer_attempt_id="attempt_map_python_b",
                claim_token=claim_token,
            ),
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            == 3
        )


@pytest.mark.parametrize("effect_site", ("result", "fact", "checkpoint"))
def test_paused_writer_cannot_commit_after_replacement(
    tmp_path: Path,
    effect_site: str,
) -> None:
    project, run_id, row_ids, column_id, claim_token = _seed_claimed_run(
        tmp_path,
        name=f"paused-{effect_site}",
    )
    checkpoints = EffectCheckpointStore(project.db)
    with closing(project):
        _insert_attempt(
            project,
            attempt_id="attempt_a",
            run_id=run_id,
            seq=0,
            state="dispatching",
        )
        fence_a = WriterFence.claimed(
            run_id=run_id, writer_attempt_id="attempt_a", claim_token=claim_token
        )
        fence_b = WriterFence.claimed(
            run_id=run_id, writer_attempt_id="attempt_b", claim_token=claim_token
        )
        paused_ref = row_effect_ref(
            "checkpoint-paused",
            run_id=run_id,
            row_id=row_ids[0],
            action_kind="map.python",
            identity="paused",
        )
        if effect_site == "checkpoint":
            assert checkpoints.reserve(
                paused_ref,
                fence=fence_a,
                payload={"request": "paid"},
            )
        _replace_attempt(
            project,
            run_id=run_id,
            old_attempt_id="attempt_a",
            new_attempt_id="attempt_b",
        )
        before = _authority_snapshot(project, run_id)

        if effect_site == "checkpoint":
            # A returned external fact belongs to the dispatched attempt even
            # after replacement. This grants no right to publish cell values.
            with pytest.raises(StaleAttemptWriter):
                checkpoints.complete(
                    paused_ref,
                    payload={"response": "imposter-b"},
                    fence=fence_b,
                )
            checkpoints.complete(
                paused_ref,
                payload={"response": "late-a"},
                fence=fence_a,
            )
            checkpoint = checkpoints.get("checkpoint-paused")
            assert checkpoint["state"] == "returned"
            assert checkpoint["authorized_attempt_id"] == "attempt_a"
            assert _authority_snapshot(project, run_id)["run"] == before["run"]
            return

        with pytest.raises(StaleAttemptWriter) as exc_info:
            if effect_site == "result":
                RunResultStore(project).write_results(
                    run_id,
                    _result_batch(row_ids[0], column_id, "late-a"),
                    fence=fence_a,
                )
            elif effect_site == "fact":
                RunResultStore(project).write_model_calls(
                    run_id,
                    _fact_batch(row_ids[0], column_id, "late-a"),
                    fence=fence_a,
                )
            else:
                checkpoints.complete(
                    paused_ref,
                    payload={"response": "late-a"},
                    fence=fence_a,
                )
        assert exc_info.value.code == "stale_attempt_writer"
        assert _authority_snapshot(project, run_id) == before

        if effect_site == "result":
            RunResultStore(project).write_results(
                run_id,
                _result_batch(row_ids[0], column_id, "winner-b"),
                fence=fence_b,
            )
        elif effect_site == "fact":
            RunResultStore(project).write_model_calls(
                run_id,
                _fact_batch(row_ids[0], column_id, "winner-b"),
                fence=fence_b,
            )
            project.db.commit()
        else:
            checkpoints.complete(
                paused_ref,
                payload={"response": "winner-b"},
                fence=fence_b,
            )
        after = _authority_snapshot(project, run_id)
        assert after["run"][1] == "attempt_b"
        assert all(claim[3] == "active" for claim in after["claims"])
        assert after != before


def test_writer_fence_requires_attempt_to_belong_to_run(tmp_path: Path) -> None:
    project, run_id, rows, column_id, claim_token = _seed_claimed_run(tmp_path)
    with closing(project):
        other_op = project.append_change("map", {"recipe": "python"})
        other_run_id = RunResultStore(project).start_run(
            other_op,
            project.db.execute(
                "SELECT sheet_id FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()[0],
            "map.python",
        )
        _insert_attempt(
            project,
            attempt_id="attempt_other_run",
            run_id=other_run_id,
            seq=0,
            state="dispatching",
        )
        with pytest.raises(StaleAttemptWriter):
            RunResultStore(project).write_results(
                run_id,
                _result_batch(rows[0], column_id, "wrong-run"),
                fence=WriterFence.claimed(
                    run_id=run_id,
                    writer_attempt_id="attempt_other_run",
                    claim_token=claim_token,
                ),
            )
        assert _authority_snapshot(project, run_id)["results"] == 0


def test_writer_fence_requires_dispatching_state(tmp_path: Path) -> None:
    project, run_id, rows, column_id, claim_token = _seed_claimed_run(tmp_path)
    with closing(project):
        _insert_attempt(
            project,
            attempt_id="attempt_not_dispatching",
            run_id=run_id,
            seq=0,
            state="admitted",
        )
        with pytest.raises(StaleAttemptWriter):
            RunResultStore(project).write_results(
                run_id,
                _result_batch(rows[0], column_id, "not-dispatching"),
                fence=WriterFence.claimed(
                    run_id=run_id,
                    writer_attempt_id="attempt_not_dispatching",
                    claim_token=claim_token,
                ),
            )
        assert _authority_snapshot(project, run_id)["results"] == 0


@pytest.mark.parametrize("token", (None, "output-claim:wrong"))
def test_writer_fence_requires_exact_active_claim_token(
    tmp_path: Path,
    token: str | None,
) -> None:
    project, run_id, rows, column_id, _claim_token = _seed_claimed_run(tmp_path)
    with closing(project):
        _insert_attempt(
            project,
            attempt_id="attempt_live",
            run_id=run_id,
            seq=0,
            state="dispatching",
        )
        with pytest.raises(StaleAttemptWriter):
            RunResultStore(project).write_model_calls(
                run_id,
                _fact_batch(rows[0], column_id, "wrong-token"),
                fence=WriterFence(
                    run_id=run_id,
                    writer_attempt_id="attempt_live",
                    claim_token=token,
                    authorized_attempt_id="attempt_live",
                ),
                _renew_claim=False,
            )
        assert _authority_snapshot(project, run_id)["facts"] == 0


@pytest.mark.parametrize("nested", (False, True))
def test_writer_fence_requires_claim_coverage_for_every_fact_column(
    tmp_path: Path,
    nested: bool,
) -> None:
    project, run_id, rows, column_id, claim_token = _seed_claimed_run(tmp_path)
    with closing(project):
        other_column_id = project.add_column(
            project.db.execute(
                "SELECT sheet_id FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()[0],
            "unclaimed",
            ai_generated=True,
        )
        _insert_attempt(
            project,
            attempt_id="attempt_live",
            run_id=run_id,
            seq=0,
            state="dispatching",
        )
        batch = _fact_batch(rows[0], column_id, f"unclaimed-{nested}")
        if nested:
            batch[0]["model_calls"][0]["column_id"] = other_column_id
        else:
            batch[0]["column_id"] = other_column_id
        with pytest.raises(StaleAttemptWriter):
            RunResultStore(project).write_model_calls(
                run_id,
                batch,
                fence=WriterFence.claimed(
                    run_id=run_id,
                    writer_attempt_id="attempt_live",
                    claim_token=claim_token,
                ),
            )
        assert _authority_snapshot(project, run_id)["facts"] == 0


def test_resume_writer_preserves_original_checkpoint_fact_owner(
    tmp_path: Path,
) -> None:
    project, run_id, rows, column_id, claim_token = _seed_claimed_run(tmp_path)
    checkpoints = EffectCheckpointStore(project.db)
    with closing(project):
        _insert_attempt(
            project,
            attempt_id="attempt_a",
            run_id=run_id,
            seq=0,
            state="dispatching",
        )
        fence_a = WriterFence.claimed(
            run_id=run_id, writer_attempt_id="attempt_a", claim_token=claim_token
        )
        ref = row_effect_ref(
            "checkpoint-replay-owner",
            run_id=run_id,
            row_id=rows[0],
            action_kind="map.python",
            identity="replay-owner",
        )
        assert checkpoints.reserve(ref, fence=fence_a, payload={"request": "paid"})
        checkpoints.complete(ref, payload={"response": "returned"}, fence=fence_a)
        _replace_attempt(
            project,
            run_id=run_id,
            old_attempt_id="attempt_a",
            new_attempt_id="attempt_b",
        )

        def accrue(checkpoint: dict[str, Any]) -> float:
            RunResultStore(project).write_returned_call_accounting_in_transaction(
                run_id,
                _fact_batch(rows[0], column_id, "replayed-a"),
                fence=fence_a.for_fact_owner(checkpoint["authorized_attempt_id"]),
            )
            return 0.0

        with write_transaction(checkpoints.db):
            checkpoints.account_returned(
                ref,
                payload={"response": "returned"},
                accrue=accrue,
                fence=fence_a,
            )
        fact = project.db.execute(
            "SELECT attempt_id FROM model_calls WHERE id='replayed-a'"
        ).fetchone()
        assert fact is not None
        assert fact["attempt_id"] == "attempt_a"
        assert (
            project.db.execute(
                "SELECT current_attempt_id FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()[0]
            == "attempt_b"
        )


def test_production_checkpoint_consume_uses_resume_writer_and_original_owner(
    tmp_path: Path,
) -> None:
    project, run_id, rows, column_id, claim_token = _seed_claimed_run(tmp_path)
    store = RunResultStore(project)
    with closing(project):
        _insert_attempt(
            project,
            attempt_id="attempt_a",
            run_id=run_id,
            seq=0,
            state="dispatching",
        )
        batch = _fact_batch(rows[0], column_id, "consume-owner-a")
        batch[0]["value"] = "returned"
        fence_a = WriterFence.claimed(
            run_id=run_id, writer_attempt_id="attempt_a", claim_token=claim_token
        )
        fence_b = WriterFence.claimed(
            run_id=run_id, writer_attempt_id="attempt_b", claim_token=claim_token
        )
        ref = row_effect_ref(
            "checkpoint-production-consume",
            run_id=run_id,
            row_id=rows[0],
            action_kind="map.python",
            identity="production-consume",
        )
        assert store.reserve_row_effect_checkpoint(ref, fence=fence_a)
        store.complete_row_effect_checkpoint(
            ref,
            batch=batch,
            replay_response={"python_result": {"value": "returned"}},
            fence=fence_a,
        )
        _replace_attempt(
            project,
            run_id=run_id,
            old_attempt_id="attempt_a",
            new_attempt_id="attempt_b",
        )

        with write_transaction(store.db):
            store.consume_returned_row_effect_checkpoint(
                ref,
                batch=batch,
                fence=fence_b,
            )

        fact = project.db.execute(
            "SELECT attempt_id FROM model_calls WHERE id='consume-owner-a'"
        ).fetchone()
        assert fact is not None
        assert fact["attempt_id"] == "attempt_a"
        assert (
            project.db.execute(
                "SELECT value FROM results WHERE run_id=? AND row_id=? AND column_id=?",
                (run_id, rows[0], column_id),
            ).fetchone()
            is not None
        )
        assert (
            project.db.execute(
                "SELECT 1 FROM effect_checkpoints "
                "WHERE id='checkpoint-production-consume'"
            ).fetchone()
            is None
        )
        assert (
            project.db.execute(
                "SELECT current_attempt_id FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()[0]
            == "attempt_b"
        )


def test_claimed_run_cannot_select_claimless_writer_mode(tmp_path: Path) -> None:
    project, run_id, rows, column_id, _claim_token = _seed_claimed_run(tmp_path)
    with closing(project):
        _insert_attempt(
            project,
            attempt_id="attempt_claimed",
            run_id=run_id,
            seq=0,
            state="dispatching",
        )
        with pytest.raises(StaleAttemptWriter):
            RunResultStore(project).write_results(
                run_id,
                _result_batch(rows[0], column_id, "forbidden-claimless"),
                fence=WriterFence.claimless(
                    run_id=run_id, attempt_id="attempt_claimed"
                ),
            )
        assert _authority_snapshot(project, run_id)["results"] == 0


def test_claimless_writer_must_remain_the_current_dispatch_holder(
    tmp_path: Path,
) -> None:
    project, run_id, rows, _column_id, claim_token = _seed_claimed_run(tmp_path)
    checkpoints = EffectCheckpointStore(project.db)
    with closing(project):
        with write_transaction(project.db):
            OutputColumnClaimStore(project).release(
                claim_token=claim_token,
                status="released",
            )
        _insert_attempt(
            project,
            attempt_id="attempt_claimless_a",
            run_id=run_id,
            seq=0,
            state="dispatching",
        )
        _replace_attempt(
            project,
            run_id=run_id,
            old_attempt_id="attempt_claimless_a",
            new_attempt_id="attempt_claimless_b",
        )
        with pytest.raises(StaleAttemptWriter):
            checkpoints.reserve(
                EffectCheckpointRef(
                    checkpoint_id="claimless-stale-a",
                    family="claimless",
                    group_key=str(run_id),
                    unit_key=str(rows[0]),
                    action_kind="reduce.group_summary",
                    identity="claimless-stale-a",
                ),
                fence=WriterFence.claimless(
                    run_id=run_id, attempt_id="attempt_claimless_a"
                ),
                payload={"request": "stale"},
            )
        assert checkpoints.reserve(
            EffectCheckpointRef(
                checkpoint_id="claimless-live-b",
                family="claimless",
                group_key=str(run_id),
                unit_key=str(rows[0]),
                action_kind="reduce.group_summary",
                identity="claimless-live-b",
            ),
            fence=WriterFence.claimless(
                run_id=run_id, attempt_id="attempt_claimless_b"
            ),
            payload={"request": "live"},
        )
        assert checkpoints.get("claimless-live-b") is not None


def test_terminal_finalize_refuses_a_writer_replaced_after_its_last_batch(
    tmp_path: Path,
) -> None:
    project, run_id, rows, column_id, claim_token = _seed_claimed_run(tmp_path)
    with closing(project):
        _insert_attempt(
            project,
            attempt_id="attempt_finalize_a",
            run_id=run_id,
            seq=0,
            state="dispatching",
        )
        fence_a = WriterFence.claimed(
            run_id=run_id,
            writer_attempt_id="attempt_finalize_a",
            claim_token=claim_token,
        )
        RunResultStore(project).write_results(
            run_id,
            _result_batch(rows[0], column_id, "last-live-batch"),
            fence=fence_a,
        )
        _replace_attempt(
            project,
            run_id=run_id,
            old_attempt_id="attempt_finalize_a",
            new_attempt_id="attempt_finalize_b",
        )
        before = _authority_snapshot(project, run_id)
        op_id = int(
            project.db.execute(
                "SELECT op_id FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()[0]
        )

        with pytest.raises(StaleAttemptWriter):
            finalize_dispatched_run(
                project,
                RunResultStore(project),
                op_id=op_id,
                status="completed",
                out_cols={"python_result": column_id},
                fence=fence_a,
                terminal_attempt_state="effected",
            )

        assert _authority_snapshot(project, run_id) == before
        assert (
            project.db.execute(
                "SELECT current_run_id FROM columns WHERE id=?",
                (column_id,),
            ).fetchone()[0]
            is None
        )


def test_terminal_claim_release_refuses_a_replaced_writer(
    tmp_path: Path,
) -> None:
    project, run_id, _rows, _column_id, claim_token = _seed_claimed_run(tmp_path)
    claims = OutputColumnClaimStore(project)
    with closing(project):
        _insert_attempt(
            project,
            attempt_id="attempt_release_a",
            run_id=run_id,
            seq=0,
            state="dispatching",
        )
        _replace_attempt(
            project,
            run_id=run_id,
            old_attempt_id="attempt_release_a",
            new_attempt_id="attempt_release_b",
        )
        before = _authority_snapshot(project, run_id)

        with pytest.raises(StaleAttemptWriter):
            claims.finish_current_writer(
                fence=WriterFence.claimed(
                    run_id=run_id,
                    writer_attempt_id="attempt_release_a",
                    claim_token=claim_token,
                ),
                attempt_state="effected",
            )

        assert _authority_snapshot(project, run_id) == before
        assert (
            project.db.execute(
                "SELECT state FROM execution_attempts WHERE id='attempt_release_b'"
            ).fetchone()[0]
            == "dispatching"
        )


@pytest.mark.parametrize("replace_before_terminal", (False, True))
def test_queued_terminal_failure_uses_current_writer_fence(
    tmp_path: Path,
    replace_before_terminal: bool,
) -> None:
    from frisket.engine.executor.queued_actions import (
        queued_v1_terminal_failure_result,
    )

    project, run_id, _rows, _column_id, claim_token = _seed_claimed_run(
        tmp_path,
        name=f"queued-terminal-{replace_before_terminal}",
    )
    receipt_id = claim_token.removeprefix("output-claim:")
    with closing(project):
        _insert_attempt(
            project,
            attempt_id="attempt_queued_a",
            run_id=run_id,
            seq=0,
            state="dispatching",
        )
        if replace_before_terminal:
            _replace_attempt(
                project,
                run_id=run_id,
                old_attempt_id="attempt_queued_a",
                new_attempt_id="attempt_queued_b",
            )
        before = _authority_snapshot(project, run_id)

        def invoke():
            return queued_v1_terminal_failure_result(
                project,
                {},
                project_id="project",
                run_id=run_id,
                error=ActionError(
                    code="provider_rate_limited",
                    message="provider rate limited",
                    action_kind="map.python",
                ),
                fence=WriterFence.claimed(
                    run_id=run_id,
                    writer_attempt_id="attempt_queued_a",
                    claim_token=claim_token,
                ),
            )

        if replace_before_terminal:
            with pytest.raises(StaleAttemptWriter):
                invoke()
            assert _authority_snapshot(project, run_id) == before
            assert (
                project.db.execute(
                    "SELECT status FROM receipts WHERE id=?",
                    (receipt_id,),
                ).fetchone()[0]
                == "running"
            )
            assert (
                project.db.execute(
                    "SELECT state FROM execution_attempts WHERE id='attempt_queued_b'"
                ).fetchone()[0]
                == "dispatching"
            )
        else:
            result = invoke()
            assert result.status == "failed"
            assert result.errors[0].code == "provider_rate_limited"
            assert (
                project.db.execute(
                    "SELECT status FROM receipts WHERE id=?",
                    (receipt_id,),
                ).fetchone()[0]
                == "failed"
            )
            run = project.db.execute(
                "SELECT status,current_attempt_id FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            assert run is not None
            assert tuple(run) == ("failed", None)
            assert (
                project.db.execute(
                    "SELECT state FROM execution_attempts WHERE id='attempt_queued_a'"
                ).fetchone()[0]
                == "effected"
            )
            assert (
                project.db.execute(
                    "SELECT status FROM output_column_claims WHERE claim_token=?",
                    (claim_token,),
                ).fetchone()[0]
                == "failed"
            )


def test_running_cancel_without_writer_tuple_retains_durable_authority(
    tmp_path: Path,
) -> None:
    """Optional caller arguments cannot downgrade a live durable writer."""
    from frisket.engine.executor.queued_actions import (
        queued_v1_terminal_receipt_result,
    )

    project, run_id, _rows, _column_id, claim_token = _seed_claimed_run(
        tmp_path,
        name="running-cancel-no-writer",
    )
    receipt_id = claim_token.removeprefix("output-claim:")
    with closing(project):
        _insert_attempt(
            project,
            attempt_id="attempt_running_cancel",
            run_id=run_id,
            seq=0,
            state="dispatching",
        )
        before = _authority_snapshot(project, run_id)

        with pytest.raises(
            StaleAttemptWriter,
            match="durable dispatching attempt",
        ):
            queued_v1_terminal_receipt_result(
                project,
                {},
                project_id="project",
                run_id=run_id,
                status="cancelled",
                receipt_id=receipt_id,
            )

        assert _authority_snapshot(project, run_id) == before
        assert (
            project.db.execute(
                "SELECT status FROM receipts WHERE id=?",
                (receipt_id,),
            ).fetchone()[0]
            == "running"
        )
        assert (
            project.db.execute(
                "SELECT status FROM output_column_claims WHERE claim_token=?",
                (claim_token,),
            ).fetchone()[0]
            == "active"
        )


def test_renewal_failure_rolls_back_batch_and_leaves_run_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run_id, rows, column_id, claim_token = _seed_claimed_run(tmp_path)
    with closing(project):
        _insert_attempt(
            project,
            attempt_id="attempt_live",
            run_id=run_id,
            seq=0,
            state="dispatching",
        )
        before = _authority_snapshot(project, run_id)

        def fail_renew(*_args: Any, **_kwargs: Any) -> int:
            raise sqlite3.OperationalError("injected renewal failure")

        monkeypatch.setattr(OutputColumnClaimStore, "renew", fail_renew)
        with pytest.raises(sqlite3.OperationalError, match="renewal failure"):
            RunResultStore(project).write_results(
                run_id,
                _result_batch(rows[0], column_id, "must-rollback"),
                fence=WriterFence.claimed(
                    run_id=run_id,
                    writer_attempt_id="attempt_live",
                    claim_token=claim_token,
                ),
            )
        assert _authority_snapshot(project, run_id) == before
        run = project.db.execute(
            "SELECT status, current_attempt_id FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert tuple(run) == ("running", "attempt_live")


def test_returned_checkpoint_does_not_need_publication_lease_renewal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run_id, rows, _column_id, claim_token = _seed_claimed_run(tmp_path)
    checkpoints = EffectCheckpointStore(project.db)
    with closing(project):
        _insert_attempt(
            project,
            attempt_id="attempt_live",
            run_id=run_id,
            seq=0,
            state="dispatching",
        )
        live_fence = WriterFence.claimed(
            run_id=run_id, writer_attempt_id="attempt_live", claim_token=claim_token
        )
        renewal_ref = row_effect_ref(
            "checkpoint-renewal-failure",
            run_id=run_id,
            row_id=rows[0],
            action_kind="map.python",
            identity="checkpoint-renewal-failure",
        )
        assert checkpoints.reserve(
            renewal_ref,
            fence=live_fence,
            payload={"request": "paid"},
        )
        before = _authority_snapshot(project, run_id)

        def fail_renew(*_args: Any, **_kwargs: Any) -> int:
            raise sqlite3.OperationalError("injected checkpoint renewal failure")

        monkeypatch.setattr(
            OutputColumnClaimStore,
            "renew_in_transaction",
            fail_renew,
        )
        checkpoints.complete(
            renewal_ref,
            payload={"response": "retained"},
            fence=live_fence,
        )
        after = _authority_snapshot(project, run_id)
        assert after["run"] == before["run"]
        assert after["claims"] == before["claims"]
        checkpoint = checkpoints.get("checkpoint-renewal-failure")
        assert checkpoint is not None
        assert checkpoint["state"] == "returned"


@pytest.mark.parametrize("winner", ("write", "abandon"))
def test_write_and_abandonment_have_one_serialized_winner(
    tmp_path: Path,
    winner: str,
) -> None:
    project, run_id, rows, column_id, claim_token = _seed_claimed_run(tmp_path)
    peer = Project(project.path)
    try:
        _insert_attempt(
            project,
            attempt_id="attempt_a",
            run_id=run_id,
            seq=0,
            state="dispatching",
            age_hours=7,
        )
        project.db.execute(
            "UPDATE output_column_claims "
            "SET lease_expires_at=datetime('now', '-1 second') "
            "WHERE run_id=? AND status='active'",
            (run_id,),
        )
        project.db.commit()
        started = Event()
        outcome: list[Any] = []

        fence_a = WriterFence.claimed(
            run_id=run_id, writer_attempt_id="attempt_a", claim_token=claim_token
        )
        if winner == "write":
            project.db.execute("BEGIN IMMEDIATE")
            RunResultStore(project).write_results_in_transaction(
                run_id,
                _result_batch(rows[0], column_id, "write-first"),
                fence=fence_a,
            )

            def sweep() -> None:
                started.set()
                with write_transaction(peer.db):
                    outcome.append(
                        abandon_stale_dispatching_attempts(
                            peer,
                            run_id,
                            max_age=timedelta(hours=6),
                        )
                    )

            thread = Thread(target=sweep)
            thread.start()
            assert started.wait(timeout=5)
            project.db.commit()
            thread.join(timeout=10)
            assert not thread.is_alive()
            assert outcome == [0]
            assert _authority_snapshot(project, run_id)["results"] == 1
        else:
            peer.db.execute("BEGIN IMMEDIATE")
            assert (
                abandon_stale_dispatching_attempts(
                    peer,
                    run_id,
                    max_age=timedelta(hours=6),
                )
                == 1
            )

            def write() -> None:
                started.set()
                try:
                    RunResultStore(project).write_results(
                        run_id,
                        _result_batch(rows[0], column_id, "abandon-first"),
                        fence=fence_a,
                    )
                except BaseException as exc:  # capture across the test thread
                    outcome.append(exc)

            thread = Thread(target=write)
            thread.start()
            assert started.wait(timeout=5)
            peer.db.commit()
            thread.join(timeout=10)
            assert not thread.is_alive()
            assert len(outcome) == 1
            assert isinstance(outcome[0], StaleAttemptWriter)
            assert _authority_snapshot(project, run_id)["results"] == 0
    finally:
        peer.close()
        project.close()
