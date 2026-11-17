"""Who owns the transaction around run, review, checkpoint, blob and descriptor writes.

Each case states one (function, ``commit`` value, caller transaction open?)
combination and what a separate connection can then see: whether the write
landed, whether the caller's own earlier write was committed with it, and
whether a transaction is left open on the caller's connection.
"""

from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store import Project
from frisket.engine.store.blob_metadata import BlobMetadataDocumentStore
from frisket.engine.store.column_descriptors import set_descriptor
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.engine.store.runs import RunResultStore
from tests.engine.test_run_review_stats import _managed_run

CALLER_SHEET_NAME = "Caller edit"


@pytest.fixture
def project(tmp_path):
    project = Project.create(tmp_path / "ownership.frisket")
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
    return int(project.db.execute("SELECT id FROM sheets").fetchone()[0])


def _running_run(project) -> int:
    op_id = project.append_op("map", {"recipe": "template"})
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


def test_finish_run_commit_false_joins_caller_transaction(project):
    run_id = _running_run(project)
    _open_caller_transaction(project)

    RunResultStore(project).finish_run(run_id, "completed", commit=False)

    assert project.db.in_transaction
    assert _committed_run_status(project, run_id) == "running"
    project.db.commit()
    assert _committed_run_status(project, run_id) == "completed"
    assert _committed_sheet_name(project) == CALLER_SHEET_NAME


def test_finish_run_commit_false_requires_caller_transaction(project):
    run_id = _running_run(project)

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        RunResultStore(project).finish_run(run_id, "completed", commit=False)

    assert not project.db.in_transaction
    assert _committed_run_status(project, run_id) == "running"


# -- result review state and metadata ----------------------------------------


@pytest.fixture
def review_target(project):
    sheet_id = _sheet_id(project)
    output = project.add_column(sheet_id, "answer", ai_generated=True)
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
    "state": lambda store, target, **kw: store.set_result_review_state(
        *target, "verified", **kw
    ),
    "metadata": lambda store, target, **kw: store.set_result_review_metadata(
        *target, "accept", None, **kw
    ),
}
REVIEW_AFTER = {"state": ("verified", None), "metadata": ("unreviewed", "accept")}


@pytest.mark.parametrize("field", sorted(REVIEW_WRITES))
def test_review_write_owns_its_transaction_when_none_is_open(
    project, review_target, field
):
    assert REVIEW_WRITES[field](RunResultStore(project), review_target) == 1

    assert not project.db.in_transaction
    assert _committed_review(project, *review_target) == REVIEW_AFTER[field]


@pytest.mark.parametrize("field", sorted(REVIEW_WRITES))
def test_review_write_commit_false_joins_caller_transaction(
    project, review_target, field
):
    _open_caller_transaction(project)

    REVIEW_WRITES[field](RunResultStore(project), review_target, commit=False)

    assert project.db.in_transaction
    assert _committed_review(project, *review_target) == ("unreviewed", None)
    project.db.commit()
    assert _committed_review(project, *review_target) == REVIEW_AFTER[field]


@pytest.mark.parametrize("field", sorted(REVIEW_WRITES))
def test_review_write_commit_true_inside_caller_transaction_refuses_and_keeps_it(
    project, review_target, field
):
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        REVIEW_WRITES[field](RunResultStore(project), review_target)

    assert project.db.in_transaction
    assert _committed_sheet_name(project) == "Source"
    assert _committed_review(project, *review_target) == ("unreviewed", None)


@pytest.mark.parametrize("field", sorted(REVIEW_WRITES))
def test_review_write_commit_false_requires_caller_transaction(
    project, review_target, field
):
    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        REVIEW_WRITES[field](RunResultStore(project), review_target, commit=False)

    assert not project.db.in_transaction


# -- start_run, set_run_halt, begin_run_resume -------------------------------


def test_start_run_commit_false_requires_caller_transaction(project):
    op_id = project.append_op("map", {"recipe": "template"})

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        RunResultStore(project).start_run(
            op_id, _sheet_id(project), "map.template", commit=False
        )

    assert not project.db.in_transaction
    assert _committed(project, "SELECT COUNT(*) FROM runs")[0][0] == 0


def test_start_run_commit_false_joins_caller_transaction(project):
    op_id = project.append_op("map", {"recipe": "template"})
    _open_caller_transaction(project)

    run_id = RunResultStore(project).start_run(
        op_id, _sheet_id(project), "map.template", commit=False
    )

    assert project.db.in_transaction
    project.db.commit()
    assert _committed_run_status(project, run_id) == "running"


def test_start_run_owns_its_transaction_when_none_is_open(project):
    op_id = project.append_op("map", {"recipe": "template"})

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


def test_set_run_halt_commit_false_requires_caller_transaction(project):
    run_id = _running_run(project)

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        RunResultStore(project).set_run_halt(
            run_id, "source_changed", "x", commit=False
        )

    assert not project.db.in_transaction
    assert _committed_halt(project, run_id) == (None, None)


def test_set_run_halt_commit_false_joins_caller_transaction(project):
    run_id = _running_run(project)
    _open_caller_transaction(project)

    RunResultStore(project).set_run_halt(run_id, "source_changed", "x", commit=False)

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


def test_begin_run_resume_commit_false_requires_caller_transaction(project):
    run_id = _cancelled_run(project)

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        RunResultStore(project).begin_run_resume(run_id, commit=False)

    assert not project.db.in_transaction
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
    assert store.reserve("cp-1", **UNIT) is True
    store.complete("cp-1", **UNIT, payload={"envelope": "raw"})
    return store


def _consume_group(store, **kw):
    return store.consume_group_retained(
        family=UNIT["family"],
        group_key=UNIT["group_key"],
        action_kind=UNIT["action_kind"],
        audit=lambda raw: {"sha_of": raw},
        **kw,
    )


def _committed_checkpoint_state(project):
    return _committed(project, "SELECT state FROM effect_checkpoints")[0][0]


def test_consume_group_retained_owns_its_transaction_when_none_is_open(
    project, returned_checkpoint
):
    assert _consume_group(returned_checkpoint) == 1

    assert not project.db.in_transaction
    assert _committed_checkpoint_state(project) == "consumed"


def test_consume_group_retained_commit_false_requires_caller_transaction(
    project, returned_checkpoint
):
    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        _consume_group(returned_checkpoint, commit=False)

    assert not project.db.in_transaction
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


def test_blob_document_update_commit_false_without_transaction_hands_one_back(
    project, blob_digest
):
    BlobMetadataDocumentStore(project).transactionally_update_document(
        blob_digest, _mark, commit=False
    )

    assert project.db.in_transaction
    assert "marked" not in _committed_blob_metadata(project, blob_digest)
    project.db.commit()
    assert "marked" in _committed_blob_metadata(project, blob_digest)


def test_blob_document_update_joins_caller_transaction(project, blob_digest):
    _open_caller_transaction(project)

    BlobMetadataDocumentStore(project).transactionally_update_document(
        blob_digest, _mark
    )

    assert project.db.in_transaction
    assert "marked" not in _committed_blob_metadata(project, blob_digest)
    project.db.commit()


def test_blob_document_update_owns_its_transaction_when_none_is_open(
    project, blob_digest
):
    BlobMetadataDocumentStore(project).transactionally_update_document(
        blob_digest, _mark
    )

    assert not project.db.in_transaction
    assert "marked" in _committed_blob_metadata(project, blob_digest)


def _write_namespace(_current, proposal):
    from frisket.engine.store.blob_metadata import BlobMetadataNamespaceCASDecision

    return BlobMetadataNamespaceCASDecision(
        written=True, value=proposal, reason="written"
    )


def test_blob_namespace_swap_commit_false_without_transaction_hands_one_back(
    project, blob_digest
):
    BlobMetadataDocumentStore(project).compare_and_swap_metadata_namespace(
        blob_digest, "ns", {"v": 1}, _write_namespace, commit=False
    )

    assert project.db.in_transaction
    assert '"ns"' not in _committed_blob_metadata(project, blob_digest)
    project.db.rollback()


# -- column descriptors -------------------------------------------------------


def _committed_column_type(project, column_id):
    return _committed(project, "SELECT type FROM columns WHERE id=?", (column_id,))[0][
        0
    ]


def _value_column(project) -> int:
    return int(project.db.execute("SELECT id FROM columns").fetchone()[0])


def test_set_descriptor_commit_false_without_transaction_hands_one_back(project):
    column_id = _value_column(project)

    set_descriptor(project, column_id, "type", "number", commit=False)

    assert project.db.in_transaction
    assert _committed_column_type(project, column_id) != "number"
    project.db.commit()
    assert _committed_column_type(project, column_id) == "number"


def test_set_descriptor_joins_caller_transaction(project):
    column_id = _value_column(project)
    _open_caller_transaction(project)

    set_descriptor(project, column_id, "type", "number")

    assert project.db.in_transaction
    assert _committed_column_type(project, column_id) != "number"
    project.db.commit()


def test_set_descriptor_owns_its_transaction_when_none_is_open(project):
    column_id = _value_column(project)

    set_descriptor(project, column_id, "type", "number")

    assert not project.db.in_transaction
    assert _committed_column_type(project, column_id) == "number"
