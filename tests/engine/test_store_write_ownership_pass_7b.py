"""Transaction ownership tests for the final store/executor/server write pass.

Verifies every split and join-only write boundary using a second SQLite
connection to observe committed state only:
- owning writes refuse inside an open caller transaction and keep caller work
  uncommitted;
- joining writes refuse without a caller transaction, leave no transaction
  open, and roll back cleanly when the caller rolls back;
- ``notifications.record_notification_delivery_attempt`` requires a caller
  transaction before reading ``MAX(attempt) + 1`` (preventing concurrent
  attempt-number races);
- ``CallableInvocation.record_in_transaction`` isolates failures with a
  savepoint without rolling back the caller's outer transaction;
- ``ProjectQAStore.events`` and ``detail_with_history`` do not commit an open
  caller transaction; and
- ``embedding_batches.record_embedding_batch[_in_transaction]`` enforce their
  owning vs joining contracts parametrically.
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from frisket.ai.embeddings import (
    EmbeddingStore,
    IndexCountUpdate,
    make_space_descriptor,
)
from frisket.contracts.action import Receipt, ReceiptIO, ReceiptRecord
from frisket.engine.executor import embedding_batches
from frisket.engine.executor.action_families import embeddings as embedding_actions
from frisket.engine.executor.action_inventory import ExecutorDeps, _TypedProjectEnvelope
from frisket.engine.executor.callable_action import (
    CallableInvocation,
    CallablePrimitiveError,
    CallableRecordUpdate,
)
from frisket.engine.executor.project_run_terminalization import (
    AbandonedRunRecoveryAuthority,
    NeverDispatchedRunTerminalAuthority,
    terminalize_project_run,
    terminalize_project_run_in_transaction,
)
from frisket.engine.store import citations, extraction_layouts
from frisket.engine.store.artifact_timeline import (
    MediaTimelineSpec,
    ensure_media_timeline,
    ensure_media_timeline_in_transaction,
    ensure_transcript_timeline,
    write_rate1_timeline_segment,
)
from frisket.engine.store.consumed_sources import (
    read_consumed_source,
    store_capture,
    verify_bundle,
)
from frisket.engine.store.leases import acquire_lease, release_lease
from frisket.engine.store.notifications import record_notification_delivery_attempt
from frisket.engine.store.output_families import OutputFamilyStore
from frisket.engine.store.project_qa import ProjectQACitationInput, ProjectQAStore
from frisket.engine.store.project_qa_research import ProjectQAResearchStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.transactions import write_transaction
from frisket.execution.attempt import (
    ATTEMPT_EXTRA,
    AttemptCommitment,
    UnroutedAdmission,
    set_attempt_state,
    set_attempt_state_in_transaction,
)
from frisket.execution.promise_compiler import OperatorBorneZeroCost
from frisket.notifications.projections import emit_alert_check_notification
from frisket.ops.base import ActionContext
from frisket.server.action_enqueue import (
    attach_run_edition_run_context,
    attach_run_edition_run_context_in_transaction,
)
from frisket.server.services.project_model_routes import ProjectModelRoutesService
from frisket.server.workspace import Workspace
from tests.helpers import run_writer_authority_fixture

CALLER_SHEET_NAME = "Caller edit"


@pytest.fixture
def project(make_project):
    project = make_project("pass-7b-ownership")
    sheet_id = project.add_sheet("Source")
    column_id = project.add_column(sheet_id, "value")
    project.add_rows(sheet_id, [{"value": "original"}], {"value": column_id})
    try:
        yield project
    finally:
        if project.db.in_transaction:
            project.db.rollback()
        project.close()


def _sheet_id(project) -> int:
    return int(
        project.db.execute("SELECT id FROM sheets ORDER BY id LIMIT 1").fetchone()[0]
    )


def _column_id(project) -> int:
    return int(
        project.db.execute("SELECT id FROM columns ORDER BY id LIMIT 1").fetchone()[0]
    )


def _row_id(project) -> int:
    return int(
        project.db.execute("SELECT id FROM rows ORDER BY id LIMIT 1").fetchone()[0]
    )


def _committed(
    project, sql: str, params: tuple[Any, ...] = ()
) -> list[tuple[Any, ...]]:
    peer = sqlite3.connect(project.db_path)
    try:
        return peer.execute(sql, params).fetchall()
    finally:
        peer.close()


def _committed_count(project, table: str) -> int:
    return int(_committed(project, f"SELECT COUNT(*) FROM {table}")[0][0])


def _committed_sheet_name(project) -> str:
    return str(_committed(project, "SELECT name FROM sheets ORDER BY id LIMIT 1")[0][0])


def _open_caller_transaction(project) -> None:
    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute(
        "UPDATE sheets SET name=? WHERE id=?", (CALLER_SHEET_NAME, _sheet_id(project))
    )


def _assert_caller_work_kept_uncommitted(project) -> None:
    assert project.db.in_transaction
    assert _committed_sheet_name(project) == "Source"


def _seed_run(project) -> tuple[int, int]:
    sheet_id = _sheet_id(project)
    row_id = _row_id(project)
    op_id = project.append_change("map", {"action_kind": "map.template"})
    run_id = RunResultStore(project).start_run(
        op_id, sheet_id, "map.template", row_ids=[row_id], total_rows=1
    )
    return op_id, run_id


def _space_descriptor() -> dict[str, Any]:
    return make_space_descriptor(
        provider_id="fastembed",
        provider_kind="local_inproc",
        requested_model="BAAI/bge-small-en-v1.5",
        actual_model_id="BAAI/bge-small-en-v1.5",
        modality="text",
        dimension=384,
    )


def _seed_embedding_index(project) -> tuple[str, str]:
    store = EmbeddingStore(project)
    space_id = store.create_space(_space_descriptor())
    with write_transaction(project.db):
        index_id = store.create_index(
            name="idx",
            space_id=space_id,
            sheet_id=_sheet_id(project),
            source_query={},
            source_columns=["value"],
            source_policy={"kind": "text_cell"},
        )
    return space_id, index_id


def _seed_delivery(project) -> tuple[int, int]:
    channel = project.create_notification_channel(kind="in_app", name="In app")
    req = project.create_notification_delivery_request(
        route_id=None,
        channel_id=channel["id"],
        delivery_kind="immediate",
        dedupe_key="pass7b-delivery",
    )
    return int(channel["id"]), int(req["id"])


def _seed_callable_invocation(
    project, receipt_id: str = "rcpt_callable"
) -> CallableInvocation:
    action = _TypedProjectEnvelope(
        kind="export.csv",
        params={"sheet_id": _sheet_id(project)},
        idempotency_key=f"key-{receipt_id}",
    )
    receipt = Receipt(
        receipt_id=receipt_id,
        project_id="pass-7b-ownership",
        action_id="act_callable",
        action_kind=action.kind,
        idempotency_key=action.idempotency_key,
        params_hash="sha256:callable",
        status="running",
    )
    with write_transaction(project.db):
        ReceiptStore(project).insert_running_in_transaction(receipt)
    return CallableInvocation(project, action, receipt, ExecutorDeps())


def _seed_qa_turn(project) -> tuple[str, str]:
    store = ProjectQAStore(project)
    thread = store.create_thread(title="Ownership thread")
    submitted = store.submit_turn(
        thread["id"], request_id="req-ownership", question="What is here?"
    )
    return str(thread["id"]), str(submitted["id"])


def _seed_consumed_bundle(project) -> int:
    sheet_id = _sheet_id(project)
    col_id = _column_id(project)
    op_id = project.append_change("map", {"action_kind": "map.template"})
    _, _, capture = read_consumed_source(project, sheet_id, [col_id])
    with write_transaction(project.db):
        return store_capture(
            project, capture, owner_op_id=op_id, output_sheet_id=sheet_id
        )


def _seed_layout(project) -> tuple[int, str, int]:
    sheet_id = _sheet_id(project)
    project.add_column(sheet_id, "PDF", type="file")
    layout = extraction_layouts.save_layout(
        project, sheet_id=sheet_id, source="PDF", draft={"fields": []}
    )
    return sheet_id, "PDF", int(layout["id"])


def _seed_alert_run(project) -> tuple[int, int]:
    sheet_id = _sheet_id(project)
    view_id = project.add_view(
        "Watch view",
        {
            "selection": {"scope": {"kind": "sheet", "sheet_id": sheet_id}},
            "presentation": {},
        },
        sheet_id=sheet_id,
    )
    alert_id = project.add_alert("Watch", view_id=view_id)
    with write_transaction(project.db):
        cur = project.db.execute(
            "INSERT INTO watch_runs "
            "(watch_id, status, op_cursor_before, op_cursor_after, "
            "matched_rows, new_rows, notification_pending) "
            "VALUES (?, 'ok', 0, 0, 1, 1, 1)",
            (alert_id,),
        )
        run_id = int(cur.lastrowid)
    return int(alert_id), run_id


def _seed_embedding_batch_context(project) -> tuple[ActionContext, int, str]:
    sheet_id = _sheet_id(project)
    row_id = _row_id(project)
    out_col = project.add_column(sheet_id, "out", generated=True)
    op_id = project.append_change("map", {"action_kind": "map.semantic_join"})
    run_id = RunResultStore(project).start_run(
        op_id, sheet_id, "map.semantic_join", row_ids=[row_id], total_rows=1
    )
    authority = run_writer_authority_fixture(
        project,
        run_id,
        output_column_ids={out_col},
    )
    commitment = AttemptCommitment(
        attempt_id=authority.writer_attempt_id,
        run_id=run_id,
        seq=0,
        identity="sha256:pass7b-emb-batch",
        scope=(row_id,),
        admission=UnroutedAdmission(),
        cost_basis=OperatorBorneZeroCost(),
        price_card_version="test",
    )
    ctx = ActionContext(
        project=project,
        extras={
            "run_id": run_id,
            ATTEMPT_EXTRA: commitment,
            "claim_token": authority.claim_token,
        },
    )
    return ctx, run_id, "openai/text-embedding-3-small"


# -- joining writes refuse without a caller transaction ----------------------


def _joining_write_cases(project) -> dict[str, Any]:
    sheet_id, source_col, layout_id = _seed_layout(project)
    channel_id, delivery_id = _seed_delivery(project)
    _space_id, index_id = _seed_embedding_index(project)
    _op_id, run_id = _seed_run(project)
    invocation = _seed_callable_invocation(project)
    _thread_id, turn_id = _seed_qa_turn(project)
    media_blob = project.add_blob(b"media", filename="clip.mp4", mime="video/mp4")
    with write_transaction(project.db):
        src_art = citations.record_source_artifact(
            project,
            artifact_kind="av",
            media_type="video/mp4",
            blob_hash=media_blob,
            duration_ms=10_000,
        )
        derived_art = citations.record_source_artifact(
            project,
            artifact_kind="av",
            media_type="video/mp4",
            duration_ms=2_000,
        )
        span = citations.record_source_span(
            project, artifact_id=int(src_art["id"]), span_kind="page", page_start=1
        )
    emb_ctx, _emb_run_id, emb_model = _seed_embedding_batch_context(project)

    return {
        "extraction_layouts.select_layout_in_transaction": lambda: (
            extraction_layouts.select_layout_in_transaction(
                project, sheet_id=sheet_id, source=source_col, layout_id=layout_id
            )
        ),
        "notifications.record_notification_delivery_request_error": lambda: (
            project.record_notification_delivery_request_error(
                delivery_id, last_error="retry"
            )
        ),
        "notifications.terminalize_notification_delivery_request": lambda: (
            project.terminalize_notification_delivery_request(
                delivery_id, status="sent"
            )
        ),
        "notifications.record_notification_delivery_attempt": lambda: (
            project.record_notification_delivery_attempt(
                delivery_request_id=delivery_id,
                notification_id=None,
                channel_id=channel_id,
                status="sent",
            )
        ),
        "embeddings.create_space_in_transaction": lambda: EmbeddingStore(
            project
        ).create_space_in_transaction(_space_descriptor()),
        "embeddings.update_index_counts_in_transaction": lambda: EmbeddingStore(
            project
        ).update_index_counts_in_transaction(index_id, IndexCountUpdate(total=1)),
        "embeddings._persist_embedding_provider_fact": lambda: (
            embedding_actions._persist_embedding_provider_fact(
                project,
                run_store=RunResultStore(project),
                run_id=run_id,
                fact={"id": "call-1", "provider": "openai", "provider_cost_usd": 0.01},
                sheet_id=sheet_id,
                row_ids=[_row_id(project)],
            )
        ),
        "attempt.set_attempt_state_in_transaction": lambda: (
            set_attempt_state_in_transaction(project, "att_none", "halted")
        ),
        "terminalize_project_run_in_transaction": lambda: (
            terminalize_project_run_in_transaction(
                project,
                run_id=run_id,
                receipt_id="rcpt_none",
                status="cancelled",
                authority=AbandonedRunRecoveryAuthority(require_cancel_intent=False),
            )
        ),
        "action_enqueue.attach_run_edition_run_context_in_transaction": lambda: (
            attach_run_edition_run_context_in_transaction(
                project, run_id, {"edition": "open"}
            )
        ),
        "callable_invocation.record_in_transaction": lambda: (
            invocation.record_in_transaction(
                CallableRecordUpdate(
                    outputs=(ReceiptIO(name="out", ref={"kind": "file"}),)
                )
            )
        ),
        "artifact_timeline.ensure_media_timeline_in_transaction": lambda: (
            ensure_media_timeline_in_transaction(
                project,
                MediaTimelineSpec(blob_hash=media_blob, duration_ms=10_000),
            )
        ),
        "artifact_timeline.write_rate1_timeline_segment": lambda: (
            write_rate1_timeline_segment(
                project,
                derived_artifact_id=int(derived_art["id"]),
                source_artifact_id=int(src_art["id"]),
                source_start_ms=1_000,
                source_end_ms=3_000,
                precision="exact",
            )
        ),
        "citations.record_citation": lambda: citations.record_citation(
            project,
            subject_kind="cell",
            subject_ref={"kind": "cell", "row_id": _row_id(project)},
            spans=[{"span_id": span["id"]}],
            sheet_id=sheet_id,
            row_id=_row_id(project),
            column_id=_column_id(project),
        ),
        "citations.mark_citations_stale_for_cell_refs": lambda: (
            citations.mark_citations_stale_for_cell_refs(
                project,
                [
                    {
                        "row_id": _row_id(project),
                        "column_id": _column_id(project),
                        "match_all_active": True,
                    }
                ],
            )
        ),
        "output_families.create_or_reuse": lambda: OutputFamilyStore(
            project
        ).create_or_reuse(
            sheet_id=sheet_id,
            fields=[{"name": "gen_out", "column_type": "text"}],
        ),
        "cells.add_row_with_undo": lambda: project.add_row_with_undo(
            sheet_id, {"value": "row2"}, {"value": _column_id(project)}
        ),
        "project_qa.add_citation_in_transaction": lambda: ProjectQAStore(
            project
        ).add_citation_in_transaction(
            turn_id,
            ProjectQACitationInput(
                label="Cell",
                source_kind="cell",
                locator={"sheet_id": sheet_id},
            ),
        ),
        "project_qa.reconcile_abandoned_turns_in_transaction": lambda: ProjectQAStore(
            project
        ).reconcile_abandoned_turns_in_transaction(live_turn_ids=()),
        "embedding_batches.record_embedding_batch_in_transaction": lambda: (
            embedding_batches.record_embedding_batch_in_transaction(
                emb_ctx,
                emb_model,
                {"provider_id": "openai", "actual_model_id": "text-embedding-3-small"},
                ["hello"],
            )
        ),
    }


@pytest.mark.parametrize(
    "case_name",
    [
        "extraction_layouts.select_layout_in_transaction",
        "notifications.record_notification_delivery_request_error",
        "notifications.terminalize_notification_delivery_request",
        "notifications.record_notification_delivery_attempt",
        "embeddings.create_space_in_transaction",
        "embeddings.update_index_counts_in_transaction",
        "embeddings._persist_embedding_provider_fact",
        "attempt.set_attempt_state_in_transaction",
        "terminalize_project_run_in_transaction",
        "action_enqueue.attach_run_edition_run_context_in_transaction",
        "callable_invocation.record_in_transaction",
        "artifact_timeline.ensure_media_timeline_in_transaction",
        "artifact_timeline.write_rate1_timeline_segment",
        "citations.record_citation",
        "citations.mark_citations_stale_for_cell_refs",
        "output_families.create_or_reuse",
        "cells.add_row_with_undo",
        "project_qa.add_citation_in_transaction",
        "project_qa.reconcile_abandoned_turns_in_transaction",
        "embedding_batches.record_embedding_batch_in_transaction",
    ],
)
def test_joining_write_refuses_without_caller_transaction(project, case_name: str):
    cases = _joining_write_cases(project)
    assert not project.db.in_transaction

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        cases[case_name]()

    assert not project.db.in_transaction


def test_pin_ready_routes_in_transaction_refuses_without_caller_transaction(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    project_id = workspace.create("Pin routes")["id"]
    service = ProjectModelRoutesService(workspace)

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        service.pin_ready_routes_in_transaction(project_id)

    assert not workspace.get(project_id).db.in_transaction


# -- owning writes refuse inside a caller transaction ------------------------


def _owning_write_cases(project) -> dict[str, Any]:
    sheet_id, source_col, layout_id = _seed_layout(project)
    _space_id, index_id = _seed_embedding_index(project)
    _op_id, run_id = _seed_run(project)
    _thread_id, turn_id = _seed_qa_turn(project)
    bundle_id = _seed_consumed_bundle(project)
    alert_id, alert_run_id = _seed_alert_run(project)
    media_blob = project.add_blob(b"own-media", filename="own.mp4", mime="video/mp4")
    with write_transaction(project.db):
        text_art = citations.record_source_artifact(
            project, artifact_kind="text", media_type="text/plain"
        )
    lease_token = acquire_lease(
        project.db,
        table="embedding_indexes",
        id_value=index_id,
        token_prefix="embrefresh",
        lease_seconds=60,
    )
    assert lease_token is not None
    emb_ctx, _emb_run_id, emb_model = _seed_embedding_batch_context(project)

    return {
        "extraction_layouts.select_layout": lambda: extraction_layouts.select_layout(
            project, sheet_id=sheet_id, source=source_col, layout_id=layout_id
        ),
        "embeddings.create_space": lambda: EmbeddingStore(project).create_space(
            _space_descriptor()
        ),
        "embeddings.update_index_counts": lambda: EmbeddingStore(
            project
        ).update_index_counts(index_id, total=2),
        "attempt.set_attempt_state": lambda: set_attempt_state(
            project, "att_none", "halted"
        ),
        "terminalize_project_run": lambda: terminalize_project_run(
            project,
            run_id=run_id,
            receipt_id="rcpt_none",
            status="cancelled",
            authority=AbandonedRunRecoveryAuthority(require_cancel_intent=False),
        ),
        "action_enqueue.attach_run_edition_run_context": lambda: (
            attach_run_edition_run_context(project, run_id, {"edition": "open"})
        ),
        "project_meta.set_retention_policy": lambda: project.set_retention_policy(
            no_compact=True
        ),
        "project_meta.set_network_policy": lambda: project.set_network_policy(
            mode="off"
        ),
        "leases.release_lease": lambda: release_lease(
            project.db,
            table="embedding_indexes",
            id_value=index_id,
            token=lease_token,
        ),
        "artifact_timeline.ensure_media_timeline": lambda: ensure_media_timeline(
            project, blob_hash=media_blob, duration_ms=5_000
        ),
        "artifact_timeline.ensure_transcript_timeline": lambda: (
            ensure_transcript_timeline(
                project,
                artifact_id=int(text_art["id"]),
                duration_ms=1_000,
                segments=[{"start_ms": 0, "end_ms": 1_000, "text": "hi"}],
            )
        ),
        "consumed_sources.verify_bundle": lambda: verify_bundle(project, bundle_id),
        "project_qa.add_citation": lambda: ProjectQAStore(project).add_citation(
            turn_id,
            label="Cell",
            source_kind="cell",
            locator={"sheet_id": sheet_id},
        ),
        "project_qa.reconcile_abandoned_turns": lambda: ProjectQAStore(
            project
        ).reconcile_abandoned_turns(live_turn_ids=()),
        "project_qa_research.create": lambda: ProjectQAResearchStore(project).create(
            turn_id=turn_id,
            actor="local",
            budget_micros=1_000_000,
            currency="USD",
            write_mode="ask_each",
            max_turns=3,
            skills=[],
        ),
        "notifications.emit_alert_check_notification": lambda: (
            emit_alert_check_notification(
                project, alert_id=alert_id, run_id=alert_run_id
            )
        ),
        "embedding_batches.record_embedding_batch": lambda: (
            embedding_batches.record_embedding_batch(
                emb_ctx,
                emb_model,
                {"provider_id": "openai", "actual_model_id": "text-embedding-3-small"},
                ["hello"],
            )
        ),
    }


@pytest.mark.parametrize(
    "case_name",
    [
        "extraction_layouts.select_layout",
        "embeddings.create_space",
        "embeddings.update_index_counts",
        "attempt.set_attempt_state",
        "terminalize_project_run",
        "action_enqueue.attach_run_edition_run_context",
        "project_meta.set_retention_policy",
        "project_meta.set_network_policy",
        "leases.release_lease",
        "artifact_timeline.ensure_media_timeline",
        "artifact_timeline.ensure_transcript_timeline",
        "consumed_sources.verify_bundle",
        "project_qa.add_citation",
        "project_qa.reconcile_abandoned_turns",
        "project_qa_research.create",
        "notifications.emit_alert_check_notification",
        "embedding_batches.record_embedding_batch",
    ],
)
def test_owning_write_inside_caller_transaction_refuses_and_keeps_caller_work(
    project, case_name: str
):
    cases = _owning_write_cases(project)
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        cases[case_name]()

    _assert_caller_work_kept_uncommitted(project)


def test_pin_ready_routes_owning_refuses_inside_caller_transaction(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    project_id = workspace.create("Pin routes owning")["id"]
    proj = workspace.get(project_id)
    sheet_id = proj.add_sheet("Source")
    service = ProjectModelRoutesService(workspace)

    proj.db.execute("BEGIN IMMEDIATE")
    proj.db.execute(
        "UPDATE sheets SET name=? WHERE id=?", (CALLER_SHEET_NAME, sheet_id)
    )

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        service.pin_ready_routes(project_id)

    assert proj.db.in_transaction
    assert _committed(proj, "SELECT name FROM sheets WHERE id=?", (sheet_id,)) == [
        ("Source",)
    ]
    proj.db.rollback()


def test_callable_invocation_owning_record_inside_caller_txn_keeps_caller_work(
    project,
):
    invocation = _seed_callable_invocation(project, receipt_id="rcpt_own_inside")
    _open_caller_transaction(project)

    with pytest.raises(CallablePrimitiveError):
        invocation.record(
            outputs=[ReceiptIO(name="out", ref={"kind": "file", "path": "a.csv"})]
        )

    _assert_caller_work_kept_uncommitted(project)


# -- outer rollback isolation & second-connection visibility -----------------


def test_joined_writes_remain_invisible_until_commit_and_roll_back_with_caller(project):
    sheet_id, source_col, layout_id = _seed_layout(project)
    channel_id, delivery_id = _seed_delivery(project)
    _space_id, index_id = _seed_embedding_index(project)
    _op_id, run_id = _seed_run(project)
    _thread_id, turn_id = _seed_qa_turn(project)
    media_blob = project.add_blob(b"joined-media", filename="j.mp4", mime="video/mp4")
    with write_transaction(project.db):
        src_art = citations.record_source_artifact(
            project,
            artifact_kind="av",
            media_type="video/mp4",
            blob_hash=media_blob,
            duration_ms=10_000,
        )
        derived_art = citations.record_source_artifact(
            project, artifact_kind="av", media_type="video/mp4", duration_ms=2_000
        )
        span = citations.record_source_span(
            project, artifact_id=int(src_art["id"]), span_kind="page", page_start=1
        )
    emb_ctx, emb_run_id, emb_model = _seed_embedding_batch_context(project)

    rows_before = _committed_count(project, "rows")
    cols_before = _committed_count(project, "columns")
    links_before = _committed_count(project, "citations")
    calls_before = _committed_count(project, "model_calls")

    _open_caller_transaction(project)
    extraction_layouts.select_layout_in_transaction(
        project, sheet_id=sheet_id, source=source_col, layout_id=layout_id
    )
    project.record_notification_delivery_request_error(delivery_id, last_error="tmp")
    project.record_notification_delivery_attempt(
        delivery_request_id=delivery_id,
        notification_id=None,
        channel_id=channel_id,
        status="failed",
        error="tmp",
    )
    EmbeddingStore(project).update_index_counts_in_transaction(
        index_id, IndexCountUpdate(total=42)
    )
    attach_run_edition_run_context_in_transaction(project, run_id, {"edition": "open"})
    write_rate1_timeline_segment(
        project,
        derived_artifact_id=int(derived_art["id"]),
        source_artifact_id=int(src_art["id"]),
        source_start_ms=0,
        source_end_ms=2_000,
        precision="exact",
    )
    citations.record_citation(
        project,
        subject_kind="cell",
        subject_ref={"kind": "cell", "row_id": _row_id(project)},
        spans=[{"span_id": span["id"]}],
        sheet_id=sheet_id,
        row_id=_row_id(project),
        column_id=_column_id(project),
    )
    OutputFamilyStore(project).create_or_reuse(
        sheet_id=sheet_id, fields=[{"name": "gen_tmp", "column_type": "text"}]
    )
    project.add_row_with_undo(
        sheet_id, {"value": "rolled_back"}, {"value": _column_id(project)}
    )
    ProjectQAStore(project).add_citation_in_transaction(
        turn_id,
        ProjectQACitationInput(
            label="Temp", source_kind="cell", locator={"sheet_id": sheet_id}
        ),
    )
    embedding_batches.record_embedding_batch_in_transaction(
        emb_ctx,
        emb_model,
        {"provider_id": "openai", "actual_model_id": "text-embedding-3-small"},
        ["joined-text"],
    )

    _assert_caller_work_kept_uncommitted(project)
    assert _committed_count(project, "notification_delivery_attempts") == 0
    assert _committed_count(project, "artifact_timeline_segments") == 0
    assert _committed_count(project, "citations") == links_before
    assert _committed_count(project, "columns") == cols_before
    assert _committed_count(project, "rows") == rows_before
    assert _committed_count(project, "project_qa_citations") == 0
    assert _committed_count(project, "model_calls") == calls_before
    assert _committed(
        project, "SELECT edition_run_context FROM runs WHERE id=?", (run_id,)
    ) == [(None,)]

    project.db.rollback()

    assert not project.db.in_transaction
    assert _committed_count(project, "notification_delivery_attempts") == 0
    assert _committed_count(project, "artifact_timeline_segments") == 0
    assert _committed_count(project, "citations") == links_before
    assert _committed_count(project, "columns") == cols_before
    assert _committed_count(project, "rows") == rows_before
    assert _committed_count(project, "project_qa_citations") == 0
    assert _committed_count(project, "model_calls") == calls_before
    assert _committed(
        project, "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (emb_run_id,)
    ) == [(0,)]


# -- live bug #1: notifications MAX(attempt)+1 race fix ----------------------


def test_notification_delivery_attempt_requires_txn_before_reading_max_attempt(project):
    channel_id, delivery_id = _seed_delivery(project)
    executed: list[str] = []

    class RecordingDB:
        def __getattr__(self, name: str) -> Any:
            return getattr(project.db, name)

        def execute(self, sql: str, *args: Any) -> Any:
            executed.append(sql)
            return project.db.execute(sql, *args)

    proxy = SimpleNamespace(
        db=RecordingDB(),
        notification_delivery_attempt=project.notification_delivery_attempt,
    )

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        record_notification_delivery_attempt(
            proxy,
            delivery_request_id=delivery_id,
            notification_id=None,
            channel_id=channel_id,
            status="failed",
        )

    assert executed == []
    assert not project.db.in_transaction

    with write_transaction(project.db):
        first = project.record_notification_delivery_attempt(
            delivery_request_id=delivery_id,
            notification_id=None,
            channel_id=channel_id,
            status="failed",
            error="transient",
        )
        second = project.record_notification_delivery_attempt(
            delivery_request_id=delivery_id,
            notification_id=None,
            channel_id=channel_id,
            status="sent",
        )
        assert (first["attempt"], second["attempt"]) == (1, 2)
        assert _committed_count(project, "notification_delivery_attempts") == 0

    assert _committed(
        project,
        "SELECT attempt, status FROM notification_delivery_attempts ORDER BY attempt",
    ) == [(1, "failed"), (2, "sent")]


# -- live bug #2: CallableInvocation.record_in_transaction savepoint ---------


@pytest.mark.parametrize(
    "failure_exc",
    [RuntimeError("before_commit boom"), asyncio.CancelledError("cancelled")],
)
def test_callable_record_in_transaction_savepoint_preserves_outer_caller_txn(
    project, failure_exc: BaseException
):
    invocation = _seed_callable_invocation(project, receipt_id="rcpt_savepoint")
    _open_caller_transaction(project)

    def fail_after_side_write() -> None:
        project.db.execute(
            "INSERT INTO meta (key, value) VALUES ('savepoint_leak', 'yes')"
        )
        raise failure_exc

    with pytest.raises(type(failure_exc)):
        invocation.record_in_transaction(
            CallableRecordUpdate(
                outputs=(ReceiptIO(name="out", ref={"kind": "file", "path": "x.csv"}),),
                records=(ReceiptRecord(ref={"kind": "probe"}, retention="pinned"),),
            ),
            before_commit=fail_after_side_write,
        )

    # Outer caller transaction stayed open with earlier caller edits intact,
    # while both the receipt update and before_commit write rolled back.
    _assert_caller_work_kept_uncommitted(project)
    assert (
        project.db.execute(
            "SELECT name FROM sheets WHERE id=?", (_sheet_id(project),)
        ).fetchone()[0]
        == CALLER_SHEET_NAME
    )
    assert (
        project.db.execute(
            "SELECT value FROM meta WHERE key='savepoint_leak'"
        ).fetchone()
        is None
    )
    stored = ReceiptStore(project).require_parsed("rcpt_savepoint")
    assert stored.outputs == []

    # Caller can still commit its own work cleanly.
    project.db.commit()
    assert _committed_sheet_name(project) == CALLER_SHEET_NAME
    assert (
        _committed(project, "SELECT value FROM meta WHERE key='savepoint_leak'") == []
    )


# -- terminalize_project_run_in_transaction refuses never-dispatched ---------


def test_terminalize_in_transaction_refuses_never_dispatched_authority(project):
    _op_id, run_id = _seed_run(project)
    _open_caller_transaction(project)
    with pytest.raises(ValueError, match="owned BEGIN IMMEDIATE"):
        terminalize_project_run_in_transaction(
            project,
            run_id=run_id,
            receipt_id="rcpt_nd",
            status="failed",
            authority=NeverDispatchedRunTerminalAuthority(),
        )
    _assert_caller_work_kept_uncommitted(project)


# -- ProjectQAStore read snapshots never commit a caller transaction ---------


def test_project_qa_read_snapshots_never_commit_caller_transaction(project):
    thread_id, _turn_id = _seed_qa_turn(project)
    store = ProjectQAStore(project)
    _open_caller_transaction(project)

    events = store.events(thread_id)
    detail = store.detail_with_history(thread_id)

    assert "events" in events
    assert detail["thread"]["id"] == thread_id
    _assert_caller_work_kept_uncommitted(project)
    project.db.rollback()
    assert _committed_sheet_name(project) == "Source"
