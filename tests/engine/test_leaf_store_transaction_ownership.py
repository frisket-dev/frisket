"""Who owns the transaction around sheet, column, change, blob, secret,
artifact, output-claim and notification writes.

Each case is read from another connection, which sees committed state only:
an owning write refuses inside a caller's transaction and leaves the caller's
work uncommitted; a joining write refuses without one; and a write of several
statements whose later statement fails leaves nothing behind, even after the
next commit on the same connection.
"""

from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store import cells, evidence, notifications, op_log
from frisket.engine.store.credentials import replace_secret_consumers
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.transactions import write_transaction

CALLER_SHEET_NAME = "Caller edit"


class _Interrupted(BaseException):
    pass


@pytest.fixture
def project(make_project):
    project = make_project("leaf-ownership")
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


def _committed(project, sql, params=()):
    peer = sqlite3.connect(project.db_path)
    try:
        return peer.execute(sql, params).fetchall()
    finally:
        peer.close()


def _committed_count(project, table: str) -> int:
    return int(_committed(project, f"SELECT COUNT(*) FROM {table}")[0][0])


def _committed_sheet_name(project) -> str:
    return _committed(project, "SELECT name FROM sheets ORDER BY id LIMIT 1")[0][0]


def _open_caller_transaction(project) -> None:
    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute(
        "UPDATE sheets SET name=? WHERE id=?", (CALLER_SHEET_NAME, _sheet_id(project))
    )


def _assert_caller_work_kept_uncommitted(project) -> None:
    assert project.db.in_transaction
    assert _committed_sheet_name(project) == "Source"


def _assert_nothing_left_behind(project) -> None:
    """No transaction is left open, so a later commit publishes nothing."""
    assert not project.db.in_transaction
    project.db.commit()


# -- owning writes refuse inside a caller's transaction ----------------------

OWNING_WRITES = {
    "add_sheet": lambda project: project.add_sheet("Other"),
    "add_column": lambda project: project.add_column(_sheet_id(project), "other"),
    "append_change": lambda project: project.append_change("edit", {}),
    "set_meta": lambda project: project.set_meta("probe", "value"),
    "add_blob": lambda project: project.add_blob(b"owned bytes"),
    "set_secret": lambda project: project.set_secret(
        name="API_TOKEN", encrypted="ciphertext", hint="…oken"
    ),
    "add_secret_consumer": lambda project: project.add_secret_consumer(
        kind="plugin", consumer_id="probe", name="API_TOKEN"
    ),
    "set_sheet_title_column": lambda project: project.set_sheet_title_column(
        _sheet_id(project), None
    ),
    "notification_item": lambda project: _insert_notification(project),
    "notification_actor_state": lambda project: project.ensure_notification_actor_state(
        _insert_notification(project), "a"
    ),
}


def _insert_notification(project, dedupe_key: str = "probe") -> int:
    return notifications.insert_notification_item(
        project,
        source_kind="run",
        source_ref="{}",
        dedupe_key=dedupe_key,
        source_event_ids="[]",
        event_count=1,
        event_kinds="[]",
        title="Probe",
        summary="",
        severity="info",
        deep_link="{}",
        payload="{}",
    )


@pytest.mark.parametrize("write", sorted(OWNING_WRITES))
def test_owning_write_inside_caller_transaction_refuses_and_keeps_it(project, write):
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        OWNING_WRITES[write](project)

    _assert_caller_work_kept_uncommitted(project)


# -- joining writes require a caller transaction -----------------------------

JOINING_WRITES = {
    "add_rows": lambda project: project.add_rows_in_transaction(
        _sheet_id(project),
        [{"value": "joined"}],
        {"value": int(project.db.execute("SELECT id FROM columns").fetchone()[0])},
    ),
    "append_change": lambda project: project.append_change_in_transaction("edit", {}),
    "add_column": lambda project: project.add_column_in_transaction(
        _sheet_id(project), "joined"
    ),
    "set_meta": lambda project: project.set_meta_in_transaction("probe", "value"),
    "replace_secret_consumers": lambda project: replace_secret_consumers(
        project, kind="mcp_connector", consumer_id="probe", names={"API_TOKEN"}
    ),
    "set_provider_key": lambda project: project.set_provider_key(
        provider="openai",
        encrypted="ciphertext",
        hint="…key",
        spend_cap_micro=None,
    ),
    "bind_to_run": lambda project: OutputColumnClaimStore(project).bind_to_run(
        claim_token="claim:none", run_id=1
    ),
    "output_claim_release": lambda project: OutputColumnClaimStore(project).release(
        claim_token="claim:none"
    ),
}


@pytest.mark.parametrize("write", sorted(JOINING_WRITES))
def test_joining_write_requires_caller_transaction(project, write):
    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        JOINING_WRITES[write](project)

    assert not project.db.in_transaction


def test_joined_change_append_rolls_back_with_its_caller(project):
    before = _committed_count(project, "ops")
    _open_caller_transaction(project)

    project.append_change_in_transaction("edit", {})
    project.db.rollback()

    assert _committed_count(project, "ops") == before
    assert _committed_sheet_name(project) == "Source"


# -- multi-statement writes are atomic ---------------------------------------


def test_change_append_whose_undo_records_fail_leaves_no_operation(
    project, monkeypatch
):
    before = _committed_count(project, "ops")

    def fail(*_args, **_kwargs):
        raise RuntimeError("undo records refused")

    monkeypatch.setattr(
        "frisket.engine.store.structural_changes.record_structural_changes", fail
    )

    with pytest.raises(RuntimeError, match="undo records refused"):
        op_log.append_change(project, "edit", {}, undo_info={"created_rows": [1]})

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "ops") == before


def test_column_revival_whose_cell_refresh_fails_stays_hidden(project, monkeypatch):
    sheet_id = _sheet_id(project)
    hidden_id = project.add_column(sheet_id, "staged", hidden=True)

    def fail(*_args, **_kwargs):
        raise RuntimeError("refresh refused")

    monkeypatch.setattr(cells, "refresh_current_cells", fail)

    with pytest.raises(RuntimeError, match="refresh refused"):
        project.add_column(sheet_id, "staged")

    _assert_nothing_left_behind(project)
    assert (
        _committed(project, "SELECT hidden FROM columns WHERE id=?", (hidden_id,))[0][0]
        == 1
    )


def test_blob_whose_metadata_merge_fails_leaves_no_blob_row(project, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("metadata refused")

    monkeypatch.setattr(MediaBlobStore, "merge_metadata", fail)

    with pytest.raises(RuntimeError, match="metadata refused"):
        project.add_blob(b"atomic bytes", metadata={"kind": "probe"})

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "blobs") == 0


def test_interrupted_source_artifact_leaves_no_captured_text(project, monkeypatch):
    before = _committed(project, "SELECT COUNT(*) FROM citation_texts")[0][0]

    def interrupt(_value):
        raise _Interrupted

    monkeypatch.setattr(evidence, "_json_dumps", interrupt)

    with pytest.raises(_Interrupted), write_transaction(project.db):
        evidence.record_source_artifact(
            project,
            artifact_kind="web_page",
            media_type="text/html",
            metadata={"captured_text": "captured"},
        )

    _assert_nothing_left_behind(project)
    assert _committed(project, "SELECT COUNT(*) FROM citation_texts")[0][0] == before
    assert _committed_count(project, "source_artifacts") == 0


def test_notifications_seen_is_atomic_across_items(project, monkeypatch):
    first = _insert_notification(project, "first")
    second = _insert_notification(project, "second")
    original = notifications._joined_actor_state

    def fail_on_second(project_, notification_id, actor_id):
        if notification_id == second:
            raise RuntimeError("actor state refused")
        return original(project_, notification_id, actor_id)

    monkeypatch.setattr(notifications, "_joined_actor_state", fail_on_second)

    with pytest.raises(RuntimeError, match="actor state refused"):
        project.mark_notifications_seen(actor_id="a", notification_ids=[first, second])

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "notification_actor_state") == 0
