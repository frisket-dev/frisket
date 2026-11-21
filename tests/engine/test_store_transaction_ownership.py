"""Who owns the transaction around run, review, checkpoint, blob and descriptor writes.

Each case states one (function, ``commit`` value, caller transaction open?)
combination and what a separate connection can then see: whether the write
landed, whether the caller's own earlier write was committed with it, and
whether a transaction is left open on the caller's connection.
"""

from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store.blob_metadata import (
    BlobMetadataDocumentStore,
    BlobMetadataNamespaceCASDecision,
)
from frisket.engine.store.column_descriptors import set_descriptor
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.contracts.action import Receipt
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from tests.engine.test_run_review_stats import _managed_run
from frisket.engine.store.effect_checkpoints import EffectCheckpointRef
from frisket.engine.store.writer_fence import WriterFence

CALLER_SHEET_NAME = "Caller edit"


@pytest.fixture
def project(make_project):
    project = make_project("ownership")
    sheet_id = project.add_sheet("Source")
    column_id = project.add_column(sheet_id, "value")
    project.add_rows(sheet_id, [{"value": "original"}], {"value": column_id})
    try:
        yield project
    finally:
        if project.db.in_transaction:
            project.db.rollback()


def _sheet_id(project) -> int:
    return int(project.db.execute("SELECT id FROM sheets").fetchone()[0])


def _running_run(project) -> int:
    op_id = project.append_change("map", {"recipe": "template"})
    return RunResultStore(project).start_run(
        op_id, _sheet_id(project), "map.template", total_rows=0
    )


def _committed(project, sql, params=()):
    """Read what another connection sees: committed state only."""
    peer = sqlite3.connect(project.db_path)
    try:
        return peer.execute(sql, params).fetchall()
    finally:
        peer.close()


def _committed_sheet_name(project) -> str:
    return _committed(project, "SELECT name FROM sheets")[0][0]


def _committed_run_status(project, run_id) -> str | None:
    rows = _committed(project, "SELECT status FROM runs WHERE id=?", (run_id,))
    return rows[0][0] if rows else None


def _open_caller_transaction(project) -> None:
    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute("UPDATE sheets SET name=?", (CALLER_SHEET_NAME,))


# -- finish_run --------------------------------------------------------------


def test_finish_run_owns_its_transaction_when_none_is_open(project):
    run_id = _running_run(project)

    RunResultStore(project).finish_run(run_id, "completed")

    assert not project.db.in_transaction
    assert _committed_run_status(project, run_id) == "completed"


def test_finish_run_commit_true_inside_caller_transaction_refuses_and_keeps_it(
    project,
):
    run_id = _running_run(project)
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        RunResultStore(project).finish_run(run_id, "completed")

    assert project.db.in_transaction
    assert _committed_sheet_name(project) == "Source"
    assert _committed_run_status(project, run_id) == "running"


def test_finish_run_owned_failure_rolls_back(project):
    with pytest.raises(ValueError, match="unknown run"):
        RunResultStore(project).finish_run(999_999, "completed")

    assert not project.db.in_transaction


def test_finish_run_in_transaction_joins_caller_transaction(project):
    run_id = _running_run(project)
    _open_caller_transaction(project)

    RunResultStore(project).finish_run_in_transaction(
        run_id, "completed", rebuild_review_stats=False
    )

    assert project.db.in_transaction
    assert _committed_run_status(project, run_id) == "running"
    project.db.commit()
    assert _committed_run_status(project, run_id) == "completed"
    assert _committed_sheet_name(project) == CALLER_SHEET_NAME


def test_finish_run_in_transaction_requires_caller_transaction(project):
    run_id = _running_run(project)

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        RunResultStore(project).finish_run_in_transaction(
            run_id, "completed", rebuild_review_stats=False
        )

    assert not project.db.in_transaction
    assert _committed_run_status(project, run_id) == "running"


# -- result review state and metadata ----------------------------------------


@pytest.fixture
def review_target(project):
    sheet_id = _sheet_id(project)
    output = project.add_column(sheet_id, "answer", generated=True)
    row_id = int(project.db.execute("SELECT id FROM rows").fetchone()[0])
    run_id = _managed_run(
        project, sheet_id=sheet_id, columns=[output], row_ids=[row_id]
    )
    return run_id, row_id, output


def _committed_review(project, run_id, row_id, column_id):
    return tuple(
        _committed(
            project,
            "SELECT review_state, review_decision FROM results "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            (run_id, row_id, column_id),
        )[0]
    )


REVIEW_WRITES = {
    "state": lambda store, target: store.set_result_review_state(*target, "verified"),
    "metadata": lambda store, target: store.set_result_review_metadata(
        *target, "accept", None
    ),
}
REVIEW_AFTER = {"state": ("verified", None), "metadata": ("unreviewed", "accept")}


@pytest.mark.parametrize("field", sorted(REVIEW_WRITES))
def test_review_write_joins_caller_transaction(project, review_target, field):
    _open_caller_transaction(project)

    assert REVIEW_WRITES[field](RunResultStore(project), review_target) == 1

    assert project.db.in_transaction
    assert _committed_review(project, *review_target) == ("unreviewed", None)
    project.db.commit()
    assert _committed_review(project, *review_target) == REVIEW_AFTER[field]


@pytest.mark.parametrize("field", sorted(REVIEW_WRITES))
def test_review_write_requires_caller_transaction(project, review_target, field):
    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        REVIEW_WRITES[field](RunResultStore(project), review_target)

    assert not project.db.in_transaction
    assert _committed_review(project, *review_target) == ("unreviewed", None)


# -- start_run, set_run_halt, begin_run_resume -------------------------------


def test_start_run_in_transaction_requires_caller_transaction(project):
    op_id = project.append_change("map", {"recipe": "template"})

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        RunResultStore(project).start_run_in_transaction(
            op_id, _sheet_id(project), "map.template"
        )

    assert not project.db.in_transaction
    assert _committed(project, "SELECT COUNT(*) FROM runs")[0][0] == 0


def test_start_run_in_transaction_joins_caller_transaction(project):
    op_id = project.append_change("map", {"recipe": "template"})
    _open_caller_transaction(project)

    run_id = RunResultStore(project).start_run_in_transaction(
        op_id, _sheet_id(project), "map.template"
    )

    assert project.db.in_transaction
    project.db.commit()
    assert _committed_run_status(project, run_id) == "running"


def test_start_run_owns_its_transaction_when_none_is_open(project):
    op_id = project.append_change("map", {"recipe": "template"})

    run_id = RunResultStore(project).start_run(
        op_id, _sheet_id(project), "map.template"
    )

    assert not project.db.in_transaction
    assert _committed_run_status(project, run_id) == "running"


def _committed_halt(project, run_id):
    return tuple(
        _committed(
            project, "SELECT halted_code, halted_reason FROM runs WHERE id=?", (run_id,)
        )[0]
    )


def test_set_run_halt_requires_caller_transaction(project):
    run_id = _running_run(project)

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        RunResultStore(project).set_run_halt(run_id, "source_changed", "x")

    assert not project.db.in_transaction
    assert _committed_halt(project, run_id) == (None, None)


def test_set_run_halt_joins_caller_transaction(project):
    run_id = _running_run(project)
    _open_caller_transaction(project)

    RunResultStore(project).set_run_halt(run_id, "source_changed", "x")

    assert project.db.in_transaction
    project.db.commit()
    assert _committed_halt(project, run_id) == ("source_changed", "x")


def _cancelled_run(project) -> int:
    run_id = _running_run(project)
    project.db.execute(
        "UPDATE runs SET status='cancelled', finished_at=datetime('now') WHERE id=?",
        (run_id,),
    )
    project.db.commit()
    return run_id


def test_begin_run_resume_owns_its_transaction_when_none_is_open(project):
    run_id = _cancelled_run(project)

    admission = RunResultStore(project).begin_run_resume(run_id)

    assert admission.admitted
    assert not project.db.in_transaction
    assert _committed_run_status(project, run_id) == "running"


def test_begin_run_resume_inside_caller_transaction_refuses_and_keeps_it(project):
    run_id = _cancelled_run(project)
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        RunResultStore(project).begin_run_resume(run_id)

    assert project.db.in_transaction
    assert _committed_run_status(project, run_id) == "cancelled"


# -- consume_group_retained ---------------------------------------------------

UNIT = {
    "family": "test_family",
    "group_key": "group-1",
    "unit_key": "unit-1",
    "action_kind": "classify",
    "identity": "identity-digest-1",
}


@pytest.fixture
def returned_checkpoint(project):
    store = EffectCheckpointStore(project.db)
    ref = EffectCheckpointRef(checkpoint_id="cp-1", **UNIT)
    assert store.reserve(ref) is True
    store.complete(ref, payload={"envelope": "raw"})
    return store


def _consume_group(store):
    return store.consume_group_retained(
        family=UNIT["family"],
        group_key=UNIT["group_key"],
        action_kind=UNIT["action_kind"],
        audit=lambda raw: {"sha_of": raw},
    )


def _committed_checkpoint_state(project):
    return _committed(project, "SELECT state FROM effect_checkpoints")[0][0]


def test_consume_group_retained_owns_its_transaction_when_none_is_open(
    project, returned_checkpoint
):
    assert _consume_group(returned_checkpoint) == 1

    assert not project.db.in_transaction
    assert _committed_checkpoint_state(project) == "consumed"


def test_consume_group_retained_inside_caller_transaction_refuses(
    project, returned_checkpoint
):
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        _consume_group(returned_checkpoint)

    assert project.db.in_transaction
    assert _committed_checkpoint_state(project) == "returned"


# -- blob metadata ----------------------------------------------------------


@pytest.fixture
def blob_digest(project):
    digest = project.add_blob(b"bytes", filename="a.bin", mime="application/x-test")
    return digest


def _committed_blob_metadata(project, digest) -> str:
    return _committed(project, "SELECT metadata FROM blobs WHERE hash=?", (digest,))[0][
        0
    ]


def _mark(document):
    return {**document, "marked": True}


def test_blob_document_update_requires_caller_transaction(project, blob_digest):
    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        BlobMetadataDocumentStore(project).transactionally_update_document(
            blob_digest, _mark
        )

    assert not project.db.in_transaction
    assert "marked" not in _committed_blob_metadata(project, blob_digest)


def test_blob_document_update_joins_caller_transaction(project, blob_digest):
    _open_caller_transaction(project)

    BlobMetadataDocumentStore(project).transactionally_update_document(
        blob_digest, _mark
    )

    assert project.db.in_transaction
    assert "marked" not in _committed_blob_metadata(project, blob_digest)
    project.db.commit()
    assert "marked" in _committed_blob_metadata(project, blob_digest)
    assert _committed_sheet_name(project) == CALLER_SHEET_NAME


def test_blob_document_update_rolls_back_with_caller_transaction(project, blob_digest):
    _open_caller_transaction(project)

    BlobMetadataDocumentStore(project).transactionally_update_document(
        blob_digest, _mark
    )
    project.db.rollback()

    assert not project.db.in_transaction
    assert "marked" not in _committed_blob_metadata(project, blob_digest)


def test_media_probe_replace_owns_its_transaction(project, blob_digest):
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        MediaBlobStore(project).replace_probe_metadata(blob_digest, {"kind": "audio"})

    assert project.db.in_transaction
    project.db.rollback()

    MediaBlobStore(project).replace_probe_metadata(blob_digest, {"kind": "audio"})
    assert not project.db.in_transaction
    assert "_media_probe_v1" in _committed_blob_metadata(project, blob_digest)


def _write_namespace(_current, proposal):
    return BlobMetadataNamespaceCASDecision(
        written=True, value=proposal, reason="written"
    )


def test_blob_namespace_swap_inside_caller_transaction_refuses(project, blob_digest):
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        BlobMetadataDocumentStore(project).compare_and_swap_metadata_namespace(
            blob_digest, "ns", {"v": 1}, _write_namespace
        )

    assert project.db.in_transaction
    assert '"ns"' not in _committed_blob_metadata(project, blob_digest)


def test_media_metadata_update_owns_the_merge_transaction(project, blob_digest):
    MediaBlobStore(project).update_metadata(blob_digest, {"marked": True})

    assert not project.db.in_transaction
    assert "marked" in _committed_blob_metadata(project, blob_digest)


# -- column descriptors -------------------------------------------------------


def _committed_column_type(project, column_id):
    return _committed(project, "SELECT type FROM columns WHERE id=?", (column_id,))[0][
        0
    ]


def _value_column(project) -> int:
    return int(project.db.execute("SELECT id FROM columns").fetchone()[0])


def test_set_descriptor_requires_caller_transaction(project):
    column_id = _value_column(project)

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        set_descriptor(project, column_id, "type", "number")

    assert not project.db.in_transaction
    assert _committed_column_type(project, column_id) != "number"


def test_set_descriptor_joins_caller_transaction(project):
    column_id = _value_column(project)
    _open_caller_transaction(project)

    set_descriptor(project, column_id, "type", "number")

    assert project.db.in_transaction
    assert _committed_column_type(project, column_id) != "number"
    project.db.commit()
    assert _committed_column_type(project, column_id) == "number"


def test_set_descriptor_rolls_back_with_caller_transaction(project):
    column_id = _value_column(project)
    _open_caller_transaction(project)

    set_descriptor(project, column_id, "type", "number")
    project.db.rollback()

    assert not project.db.in_transaction
    assert _committed_column_type(project, column_id) != "number"


# -- review stats are chosen apart from transaction ownership ----------------


def _committed_review_stats_ready(project, run_id) -> int:
    return _committed(
        project, "SELECT review_stats_ready FROM runs WHERE id=?", (run_id,)
    )[0][0]


def test_finish_run_joined_rebuild_lands_with_the_caller_commit(project):
    run_id = _running_run(project)
    _open_caller_transaction(project)

    RunResultStore(project).finish_run_in_transaction(
        run_id, "completed", rebuild_review_stats=True
    )

    assert _committed_run_status(project, run_id) == "running"
    project.db.commit()
    assert _committed_run_status(project, run_id) == "completed"
    assert _committed_review_stats_ready(project, run_id) == 1


def test_finish_run_owned_without_rebuild_leaves_review_stats_stale(project):
    run_id = _running_run(project)

    RunResultStore(project).finish_run(run_id, "completed", rebuild_review_stats=False)

    assert not project.db.in_transaction
    assert _committed_run_status(project, run_id) == "completed"
    assert _committed_review_stats_ready(project, run_id) == 0


# -- receipts ----------------------------------------------------------------


def _running_receipt(receipt_id: str = "receipt_ownership") -> Receipt:
    return Receipt(
        receipt_id=receipt_id,
        project_id="ownership",
        action_id="action_ownership",
        action_kind="map.template",
        status="running",
    )


def _committed_receipt_status(project, receipt_id="receipt_ownership"):
    rows = _committed(project, "SELECT status FROM receipts WHERE id=?", (receipt_id,))
    return rows[0][0] if rows else None


def test_receipt_insert_owned_is_durable_on_return(project):
    ReceiptStore(project).insert_running(_running_receipt())

    assert not project.db.in_transaction
    assert _committed_receipt_status(project) == "running"


def test_receipt_insert_owned_inside_caller_transaction_refuses(project):
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        ReceiptStore(project).insert_running(_running_receipt())

    assert project.db.in_transaction
    assert _committed_sheet_name(project) == "Source"
    assert _committed_receipt_status(project) is None


def test_receipt_insert_joined_rolls_back_with_the_caller(project):
    _open_caller_transaction(project)

    ReceiptStore(project).insert_running_in_transaction(_running_receipt())

    assert _committed_receipt_status(project) is None
    project.db.rollback()
    assert _committed_receipt_status(project) is None
    assert _committed_sheet_name(project) == "Source"


def test_receipt_update_joined_commits_only_with_the_caller(project):
    store = ReceiptStore(project)
    store.insert_running(_running_receipt())
    _open_caller_transaction(project)

    assert store.update_body_status_in_transaction(
        _running_receipt().model_copy(update={"status": "completed"}),
        require_status="running",
    )

    assert _committed_receipt_status(project) == "running"
    project.db.commit()
    assert _committed_receipt_status(project) == "completed"
    assert _committed_sheet_name(project) == CALLER_SHEET_NAME


def test_receipt_update_owned_inside_caller_transaction_refuses(project):
    store = ReceiptStore(project)
    store.insert_running(_running_receipt())
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        store.update_body_status(
            _running_receipt().model_copy(update={"status": "completed"}),
            require_status="running",
        )

    assert _committed_sheet_name(project) == "Source"
    assert _committed_receipt_status(project) == "running"


RECEIPT_CALLER_WRITES = {
    "insert_queued": lambda store: store.insert_queued(
        _running_receipt().model_copy(update={"status": "queued"})
    ),
    "insert_completed": lambda store: store.insert_completed(
        _running_receipt().model_copy(update={"status": "completed"})
    ),
    "insert_partial": lambda store: store.insert_partial(
        _running_receipt().model_copy(update={"status": "partial"})
    ),
    "insert_failed": lambda store: store.insert_failed(
        _running_receipt().model_copy(update={"status": "failed"})
    ),
    "update_if_status_in": lambda store: store.update_if_status_in(
        _running_receipt(), {"queued"}
    ),
    "delete_if_status": lambda store: store.delete_if_status(
        "receipt_ownership", "running"
    ),
    "delete_queued": lambda store: store.delete_queued("receipt_ownership"),
}


@pytest.mark.parametrize("write", sorted(RECEIPT_CALLER_WRITES))
def test_receipt_caller_writes_require_caller_transaction(project, write):
    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        RECEIPT_CALLER_WRITES[write](ReceiptStore(project))

    assert not project.db.in_transaction


def test_running_receipt_delete_joined_rolls_back_with_the_caller(project):
    store = ReceiptStore(project)
    store.insert_running(_running_receipt())
    _open_caller_transaction(project)

    assert store.delete_running_in_transaction("receipt_ownership")

    project.db.rollback()
    assert _committed_receipt_status(project) == "running"


# -- run counters, cancellation and pointers ---------------------------------


@pytest.mark.parametrize("write", ["set_total_rows", "increment_total_rows"])
def test_run_total_writes_require_caller_transaction(project, write):
    run_id = _running_run(project)

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        getattr(RunResultStore(project), write)(run_id, 3)

    assert not project.db.in_transaction


def test_request_cancel_joined_rolls_back_with_the_caller(project):
    run_id = _running_run(project)
    _open_caller_transaction(project)

    assert RunResultStore(project).request_cancel_in_transaction(run_id)

    project.db.rollback()
    assert _committed(
        project, "SELECT cancel_requested_at FROM runs WHERE id=?", (run_id,)
    ) == [(None,)]


def test_request_cancel_owned_inside_caller_transaction_refuses(project):
    run_id = _running_run(project)
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        RunResultStore(project).request_cancel(run_id)

    assert _committed_sheet_name(project) == "Source"


def test_point_column_at_run_requires_caller_transaction(project):
    run_id = _running_run(project)
    column_id = _value_column(project)
    op_id = int(project.db.execute("SELECT op_id FROM runs").fetchone()[0])

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        RunResultStore(project).point_column_at_run(op_id, column_id, run_id)

    assert not project.db.in_transaction


# -- provider facts and spend ------------------------------------------------


def _unscoped_call(call_id: str) -> dict:
    return {
        "id": call_id,
        "fact_version": "frisket.model-call-fact.v1",
        "capability": "llm.complete",
        "engine": "anthropic/test-model",
        "provider": "anthropic",
        "provider_kind": "chat_api",
        "credential_source": "platform_key",
        "provider_reported_cost_usd": 0.5,
        "provider_cost_usd": 0.5,
        "cost_source": "pricing_data",
        "units": {"tokens_in": 10, "tokens_out": 5},
    }


def _committed_call_ids(project) -> set[str]:
    return {row[0] for row in _committed(project, "SELECT id FROM model_calls")}


def _write_unscoped(project, call_id):
    return RunResultStore(project).write_unscoped_model_calls(
        [_unscoped_call(call_id)], row_id=None, column_id=None
    )


def _write_unscoped_in_txn(project, call_id):
    return RunResultStore(project).write_unscoped_model_calls_in_transaction(
        [_unscoped_call(call_id)], row_id=None, column_id=None
    )


def test_unscoped_facts_owned_are_durable_on_return(project):
    _write_unscoped(project, "call-owned")

    assert not project.db.in_transaction
    assert _committed_call_ids(project) == {"call-owned"}


def test_unscoped_facts_owned_inside_caller_transaction_refuse(project):
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        _write_unscoped(project, "call-refused")

    assert project.db.in_transaction
    assert _committed_sheet_name(project) == "Source"
    assert _committed_call_ids(project) == set()


def test_unscoped_facts_joined_roll_back_with_the_caller(project):
    _open_caller_transaction(project)

    _write_unscoped_in_txn(project, "call-joined")

    assert _committed_call_ids(project) == set()
    project.db.rollback()
    assert _committed_call_ids(project) == set()


def test_unscoped_facts_joined_require_caller_transaction(project):
    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        _write_unscoped_in_txn(project, "call-unowned")

    assert not project.db.in_transaction
    assert _committed_call_ids(project) == set()


def test_returned_accounting_commit_true_inside_caller_transaction_refuses(
    project,
):
    run_id = _running_run(project)
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        RunResultStore(project).write_returned_call_accounting(
            run_id,
            [{"row_id": 1, "column_id": None, "model_calls": [_unscoped_call("c")]}],
            fence=WriterFence(run_id=run_id, claimless_direct_effect=True),
        )

    assert _committed_sheet_name(project) == "Source"
    assert _committed_call_ids(project) == set()


def test_result_batch_commit_true_inside_caller_transaction_refuses(
    project, review_target
):
    run_id, row_id, column_id = review_target
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        RunResultStore(project).write_results(
            run_id,
            [{"row_id": row_id, "column_id": column_id, "value": "x"}],
        )

    assert _committed_sheet_name(project) == "Source"
