"""Lane 2.2 boundary fixes on the LIVE row-effect checkpoint path.

Two fixes ship ahead of the 2.4 migration onto the shared store:

- a pre-egress uncaught exception (a recipe bug's TypeError, not just the
  allowlisted halts) on a row that provably cannot egress discards the
  reservation instead of bricking the row behind
  ``external_effect_reconciliation_required`` forever;
- the checkpoint complete/consume/account paths refuse id-less
  ``model_calls`` entries, because replay dedup rests entirely on the id
  minted at call time (an id-less entry double-accrues cap spend on replay).
"""

from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

import asyncio
import json
import wave

import pytest

from frisket.ai.llm import ModelRouter
from frisket.contracts.action import Receipt
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.effect_checkpoints import ModelCallIdMissing
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore


def _wav(path) -> str:
    """PCM16 mono 16 kHz half-second of silence."""

    sr = 16000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * (sr // 2))
    return str(path)


def _age_silent_attempt(project: Project, attempt_id: str) -> None:
    """Model the recovery detector observing an old attempt and expired lease."""

    project.db.execute(
        "UPDATE execution_attempts SET created_at="
        "datetime('now', '-7 hours') WHERE id=?",
        (attempt_id,),
    )
    project.db.execute(
        "UPDATE output_column_claims "
        "SET renewed_at=datetime('now', '-7 hours'), "
        "lease_expires_at=datetime('now', '-1 second') "
        "WHERE run_id=(SELECT run_id FROM execution_attempts WHERE id=?) "
        "AND status='active'",
        (attempt_id,),
    )
    project.db.commit()


def test_pre_egress_typeerror_discards_checkpoint_and_run_stays_resumable(
    tmp_path, monkeypatch
) -> None:
    """Named Lane 2.2(a) acceptance: a recipe bug's TypeError BEFORE any wire
    I/O, on a metered-but-local row (no remote capability resolves — static,
    server-owned classification), must discard the reservation so resume can
    buy the row fresh instead of refusing with reconciliation_required."""

    from tests.execution_composition_helpers import open_attempt_authority
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest, SheetRows
    from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan

    project = Project.create(tmp_path / "pre-egress-bug.frisket")
    try:
        sheet = project.add_sheet("data")
        columns = {"audio": project.add_column(sheet, "audio", type="audio")}
        project.add_rows(
            sheet,
            [{"audio": _wav(tmp_path / f"row-{index}.wav")} for index in range(2)],
            columns,
        )
        request = ActionRequest(
            action_id="media.transcribe",
            scope=SheetRows(sheet_id=sheet),
            params={"source": "audio", "engine": "faster_whisper"},
            output_names={"text": "transcript"},
            idempotency_key="pre-egress-bug",
        )
        plan = build_typed_map_rows_plan(
            project,
            BoundTypedActionRequest.bind(
                ACTION_REGISTRY.get(request.action_id), request
            ),
        )
        spec = plan.spec_dict()
        receipt_id = "receipt_pre_egress_bug"
        claim_token = f"output-claim:{receipt_id}"
        ReceiptStore(project).insert_running(
            Receipt(
                receipt_id=receipt_id,
                project_id="project",
                action_id="action_pre_egress_bug",
                action_kind="media.transcribe",
                status="running",
            )
        )
        output_names = [
            str(field["name"]) for field in plan.program.output_fields(spec)
        ]
        claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet,
            output_names=output_names,
            action_kind="media.transcribe",
            receipt_id=receipt_id,
            claim_token=claim_token,
        )
        assert conflict is None
        assert len(claims) == len(output_names)

        def bind_claim(progress) -> None:  # noqa: ANN001
            OutputColumnClaimStore(project).bind_to_run(
                claim_token=claim_token,
                run_id=progress.run_id,
                expected_output_names=output_names,
            )

        calls = 0
        explode = True

        async def typeerror_then_recover(self, path, spec, *, should_cancel=None):  # noqa: ANN001
            nonlocal calls
            del self, path, spec
            calls += 1
            if explode and calls == 2:
                # The recipe-bug shape: an ordinary uncaught exception, not an
                # allowlisted RecipeInvocationHalt, raised before any egress.
                raise TypeError("'NoneType' object is not subscriptable")
            return {
                "text": f"transcript {calls}",
                "segments": [{"start": 0.0, "end": 0.5, "text": f"transcript {calls}"}],
                "language": "en",
                "duration": 0.5,
            }

        monkeypatch.setattr(
            transcribe_engines.FasterWhisperAdapter,
            "transcribe",
            typeerror_then_recover,
        )
        with pytest.raises(TypeError, match="not subscriptable"):
            asyncio.run(
                MapRunner(
                    project,
                    ModelRouter(cache=None, cache_mode="off"),
                    concurrency=1,
                    authority=open_attempt_authority(project),
                    on_progress=bind_claim,
                    allow_action_lifecycle_only_recipes=True,
                ).run(
                    spec, program=plan.program, confirmed=True, claim_token=claim_token
                )
            )
        run_id = int(project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0])
        assert calls == 2
        # The buggy row provably bought nothing; its reservation must be
        # discarded, never left to brick the row behind
        # external_effect_reconciliation_required.
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM effect_checkpoints "
                "WHERE family='row_effect' AND group_key=CAST(? AS TEXT)",
                (run_id,),
            ).fetchone()[0]
            == 0
        )

        explode = False
        first_attempt_id = str(
            project.db.execute(
                "SELECT id FROM execution_attempts WHERE run_id=? ORDER BY seq",
                (run_id,),
            ).fetchone()[0]
        )
        _age_silent_attempt(project, first_attempt_id)
        resumed = asyncio.run(
            MapRunner(
                project,
                ModelRouter(cache=None, cache_mode="off"),
                concurrency=1,
                authority=open_attempt_authority(project),
                on_progress=bind_claim,
                allow_action_lifecycle_only_recipes=True,
            ).run(
                spec,
                program=plan.program,
                confirmed=True,
                resume_run_id=run_id,
                claim_token=claim_token,
            )
        )

        assert resumed.done is True
        assert resumed.halted_code is None
        assert resumed.completed == 1
        assert calls == 3
        transcript = next(
            c for c in project.columns(sheet) if c["name"] == "transcript"
        )
        assert sorted(project.get_values(sheet, transcript["id"]).values()) == [
            "transcript 1",
            "transcript 3",
        ]
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM effect_checkpoints "
                "WHERE family='row_effect' AND group_key=CAST(? AS TEXT)",
                (run_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


@pytest.fixture()
def checkpointed_run(tmp_path):
    """A minimal run row plus a reserved row-effect checkpoint, store-level."""

    project = Project.create(tmp_path / "idless.frisket", name="idless")
    try:
        sheet_id = project.add_sheet("S")
        source_column_id = project.add_column(sheet_id, "source")
        output_column_id = project.add_column(
            sheet_id,
            "classification",
            ai_generated=True,
        )
        row_id = project.add_rows(
            sheet_id,
            [{"source": "one"}],
            {"source": source_column_id},
        )[0]
        op_id = project.append_op("map", {"action_kind": "map.classify"})
        store = RunResultStore(project)
        run_id = store.start_run(
            op_id,
            sheet_id,
            "map.classify",
            row_ids=[row_id],
        )
        receipt_id = "receipt_idless_checkpoint"
        claim_token = f"output-claim:{receipt_id}"
        ReceiptStore(project).insert_running(
            Receipt(
                receipt_id=receipt_id,
                project_id="project",
                action_id="action_idless_checkpoint",
                action_kind="map.classify",
                status="running",
                run_id=run_id,
            )
        )
        claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["classification"],
            action_kind="map.classify",
            receipt_id=receipt_id,
            run_id=run_id,
            claim_token=claim_token,
        )
        assert conflict is None
        assert len(claims) == 1
        OutputColumnClaimStore(project).bind_to_run(
            claim_token=claim_token,
            run_id=run_id,
            expected_output_names=["classification"],
        )
        attempt_id = "attempt_idless_checkpoint"
        project.db.execute(
            "INSERT INTO execution_attempts "
            "(id, run_id, seq, state, action_identity_hash, scope_json, "
            "created_at) VALUES (?, ?, 0, 'dispatching', 'idless', ?, "
            "datetime('now'))",
            (attempt_id, run_id, json.dumps([row_id])),
        )
        project.db.execute(
            "UPDATE runs SET current_attempt_id=? WHERE id=?",
            (attempt_id, run_id),
        )
        project.db.commit()
        assert store.reserve_row_effect_checkpoint(
            "cp-1",
            run_id=run_id,
            row_id=row_id,
            action_kind="classify",
            identity="identity-digest",
            authorized_attempt_id=attempt_id,
            writer_attempt_id=attempt_id,
            claim_token=claim_token,
        )
        yield (
            project,
            store,
            int(run_id),
            int(row_id),
            int(output_column_id),
            attempt_id,
            claim_token,
        )
    finally:
        project.close()


def _fact(call_id: str | None) -> dict:
    fact = {
        "fact_version": "frisket.model-call-fact.v1",
        "capability": "llm.complete",
        "engine": "anthropic/claude-haiku-4-5",
        "provider": "anthropic",
        "provider_kind": "chat_api",
        "credential_source": "none",
        "provider_cost_usd": 0.0001,
        "units": {"tokens_in": 10, "tokens_out": 5},
    }
    if call_id is not None:
        fact["id"] = call_id
    return fact


def _batch(
    call_id: str | None,
    *,
    row_id: int,
    column_id: int,
) -> list[dict]:
    return [
        {
            "row_id": row_id,
            "column_id": column_id,
            "value": "x",
            "cost": 0.0001,
            "model_calls": [_fact(call_id)],
        }
    ]


def test_id_less_model_call_refuses_checkpoint_complete_and_writes_nothing(
    checkpointed_run,
) -> None:
    """Named Lane 2.2(b) acceptance: an id-less ``model_calls`` entry in a
    checkpoint complete is refused with the named error and nothing is
    written — no fact, no spend, no state transition."""

    (
        project,
        store,
        run_id,
        row_id,
        column_id,
        attempt_id,
        claim_token,
    ) = checkpointed_run
    with pytest.raises(ModelCallIdMissing) as refusal:
        store.complete_row_effect_checkpoint(
            "cp-1",
            run_id=run_id,
            row_id=row_id,
            action_kind="classify",
            identity="identity-digest",
            batch=_batch(None, row_id=row_id, column_id=column_id),
            replay_response={"field": {"value": "x", "cost": 0.0}},
            writer_attempt_id=attempt_id,
            claim_token=claim_token,
        )
    assert refusal.value.code == "model_call_id_missing"
    assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
    checkpoint = project.db.execute(
        "SELECT state FROM effect_checkpoints WHERE id='cp-1'"
    ).fetchone()
    assert checkpoint is not None and checkpoint["state"] == "reserved"
    assert (
        project.db.execute(
            "SELECT cost_actual FROM runs WHERE id=?", (run_id,)
        ).fetchone()[0]
        == 0
    )


def test_id_less_model_call_refuses_consume_and_account_of_returned_row(
    checkpointed_run,
) -> None:
    """The consume and account paths carry the same replay-dedup obligation:
    their idempotence rests on stable call ids, so an id-less entry refuses
    before anything is written."""

    (
        project,
        store,
        run_id,
        row_id,
        column_id,
        attempt_id,
        claim_token,
    ) = checkpointed_run
    cost = store.complete_row_effect_checkpoint(
        "cp-1",
        run_id=run_id,
        row_id=row_id,
        action_kind="classify",
        identity="identity-digest",
        batch=_batch("call-1", row_id=row_id, column_id=column_id),
        replay_response={"field": {"value": "x", "cost": 0.0}},
        writer_attempt_id=attempt_id,
        claim_token=claim_token,
    )
    assert cost == pytest.approx(0.0001)
    assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1

    for refuse in (
        lambda: store.consume_returned_row_effect_checkpoint(
            "cp-1",
            run_id=run_id,
            row_id=row_id,
            action_kind="classify",
            identity="identity-digest",
            batch=_batch(None, row_id=row_id, column_id=column_id),
            writer_attempt_id=attempt_id,
            claim_token=claim_token,
        ),
        lambda: store.account_returned_row_effect_checkpoint(
            "cp-1",
            run_id=run_id,
            row_id=row_id,
            action_kind="classify",
            identity="identity-digest",
            batch=_batch(None, row_id=row_id, column_id=column_id),
            replay_response={"field": {"value": "x", "cost": 0.0}},
            writer_attempt_id=attempt_id,
            claim_token=claim_token,
        ),
    ):
        with pytest.raises(ModelCallIdMissing):
            refuse()

    # Nothing moved: no result row, no second fact, checkpoint still returned.
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
    checkpoint = project.db.execute(
        "SELECT state, payload AS response FROM effect_checkpoints WHERE id='cp-1'"
    ).fetchone()
    assert checkpoint is not None and checkpoint["state"] == "returned"
    assert json.loads(checkpoint["response"]) == {"field": {"value": "x", "cost": 0.0}}
