"""Who owns the transaction around view, alert, feed, embedding-index and
evidence writes.

Each case is read from another connection, which sees committed state only:
an owning write refuses inside a caller's transaction and leaves the caller's
work uncommitted; a joining write refuses without one and rolls back with its
caller; and a write of several statements whose later statement fails leaves
nothing behind, even after the next commit on the same connection.
"""

from __future__ import annotations

import sqlite3

import pytest

from frisket.ai.embeddings import EmbeddingStore, make_space_descriptor
from frisket.engine.store import alerts, evidence, extraction_layouts
from frisket.engine.store.feeds import FeedStore
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.transactions import write_transaction

CALLER_SHEET_NAME = "Caller edit"


@pytest.fixture
def project(make_project):
    project = make_project("write-ownership")
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


def _committed(project, sql, params=()):
    peer = sqlite3.connect(project.db_path)
    try:
        return peer.execute(sql, params).fetchall()
    finally:
        peer.close()


def _committed_count(project, table: str) -> int:
    return int(_committed(project, f"SELECT COUNT(*) FROM {table}")[0][0])


def _open_caller_transaction(project) -> None:
    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute(
        "UPDATE sheets SET name=? WHERE id=?", (CALLER_SHEET_NAME, _sheet_id(project))
    )


def _assert_caller_work_kept_uncommitted(project) -> None:
    assert project.db.in_transaction
    assert (
        _committed(project, "SELECT name FROM sheets ORDER BY id LIMIT 1")[0][0]
        == "Source"
    )


def _assert_nothing_left_behind(project) -> None:
    """No transaction is left open, so a later commit publishes nothing."""
    assert not project.db.in_transaction
    project.db.commit()


def _refuse(project, statement: str, table: str) -> None:
    """Make the next ``statement`` on ``table`` fail on this connection only."""
    project.db.execute(
        f"CREATE TEMP TRIGGER refuse_{statement.lower()}_{table} "
        f"BEFORE {statement} ON main.{table} "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )


def _view_spec(project) -> dict:
    return {
        "selection": {"scope": {"kind": "sheet", "sheet_id": _sheet_id(project)}},
        "presentation": {},
    }


def _add_view(project, name: str = "Probe view") -> int:
    return project.add_view(name, _view_spec(project))


# -- views and alerts --------------------------------------------------------


def test_view_whose_row_insert_fails_leaves_no_change(project):
    before = _committed_count(project, "ops")
    _refuse(project, "INSERT", "views")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        _add_view(project)

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "ops") == before


def test_view_whose_row_update_fails_leaves_no_change(project):
    view_id = _add_view(project)
    before = _committed_count(project, "ops")
    _refuse(project, "UPDATE", "views")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        project.update_view(view_id, name="Renamed")

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "ops") == before


def test_view_whose_row_delete_fails_keeps_its_alert(project):
    view_id = _add_view(project)
    project.add_alert("Probe alert", view_id=view_id)
    _refuse(project, "DELETE", "views")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        project.delete_view(view_id)

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "watches") == 1


@pytest.mark.parametrize("write", ["add_alert", "update_alert"])
def test_alert_write_inside_caller_transaction_refuses_and_keeps_it(project, write):
    view_id = _add_view(project)
    alert_id = (
        project.add_alert("Probe alert", view_id=view_id)
        if write == "update_alert"
        else None
    )
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        if alert_id is None:
            project.add_alert("Probe alert", view_id=view_id)
        else:
            project.update_alert(alert_id, name="Renamed")

    _assert_caller_work_kept_uncommitted(project)


def test_alert_row_delete_joins_and_rolls_back_with_its_caller(project):
    alert_id = project.add_alert("Probe alert", view_id=_add_view(project))
    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        alerts.delete_alert_rows(project, alert_id)
    _open_caller_transaction(project)

    assert alerts.delete_alert_rows(project, alert_id) is True
    project.db.rollback()

    assert _committed_count(project, "watches") == 1


# -- feeds -------------------------------------------------------------------


def _add_feed(project) -> int:
    return FeedStore(project).add_feed("Probe feed", kind="rss")


OWNING_FEED_WRITES = {
    "add_feed": lambda project, _feed_id: _add_feed(project),
    "delete_feed": lambda project, feed_id: FeedStore(project).delete_feed(feed_id),
    "record_feed_check": lambda project, feed_id: FeedStore(project).record_feed_check(
        feed_id
    ),
}


@pytest.mark.parametrize("write", sorted(OWNING_FEED_WRITES))
def test_owning_feed_write_inside_caller_transaction_refuses_and_keeps_it(
    project, write
):
    feed_id = _add_feed(project)
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        OWNING_FEED_WRITES[write](project, feed_id)

    _assert_caller_work_kept_uncommitted(project)


JOINING_FEED_WRITES = {
    "update_feed": lambda project, feed_id: FeedStore(project).update_feed(
        feed_id, name="Renamed"
    ),
    "start_feed_check": lambda project, feed_id: FeedStore(project).start_feed_check(
        feed_id
    ),
    "finish_feed_check": lambda project, _feed_id: FeedStore(project).finish_feed_check(
        1, status="ok"
    ),
    "add_feed": lambda project, _feed_id: FeedStore(project).add_feed(
        "Joined", commit=False
    ),
    "record_feed_check": lambda project, feed_id: FeedStore(project).record_feed_check(
        feed_id, commit=False
    ),
}


@pytest.mark.parametrize("write", sorted(JOINING_FEED_WRITES))
def test_joining_feed_write_requires_caller_transaction(project, write):
    feed_id = _add_feed(project)

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        JOINING_FEED_WRITES[write](project, feed_id)

    assert not project.db.in_transaction


def test_joined_feed_check_rolls_back_with_its_caller(project):
    feed_id = _add_feed(project)
    _open_caller_transaction(project)

    store = FeedStore(project)
    store.finish_feed_check(store.start_feed_check(feed_id), status="ok")
    project.db.rollback()

    assert _committed_count(project, "source_runs") == 0
    assert _committed(project, "SELECT last_status FROM sources")[0][0] == "never"


def test_feed_check_whose_feed_update_fails_leaves_no_check(project):
    feed_id = _add_feed(project)
    _refuse(project, "UPDATE", "sources")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        FeedStore(project).record_feed_check(feed_id)

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "source_runs") == 0


def test_feed_delete_whose_feed_row_delete_fails_leaves_nothing_pending(project):
    feed_id = _add_feed(project)
    _refuse(project, "DELETE", "sources")

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        FeedStore(project).delete_feed(feed_id)

    _assert_nothing_left_behind(project)
    assert _committed_count(project, "sources") == 1


# -- embedding spaces and indexes --------------------------------------------


def _space_descriptor() -> dict:
    return make_space_descriptor(
        provider_id="frisket-models",
        provider_kind="local_http",
        requested_model="default-text",
        actual_model_id="probe/text",
        modality="text",
        dimension=8,
        dtype="float32",
        distance_metric="cosine",
        normalization="l2",
        vector_options={},
    )


def _add_index(project) -> tuple[str, str]:
    store = EmbeddingStore(project)
    space_id = store.create_space(_space_descriptor())
    with write_transaction(project.db):
        index_id = store.create_index(
            name="probe",
            space_id=space_id,
            sheet_id=_sheet_id(project),
            source_query={},
            source_columns=["value"],
            source_policy={"kind": "text_cell"},
        )
    return space_id, index_id


OWNING_EMBEDDING_WRITES = {
    "create_space": lambda store, _space_id, _index_id: store.create_space(
        _space_descriptor()
    ),
    "update_index_counts": lambda store, _space_id, index_id: store.update_index_counts(
        index_id, total=1
    ),
}


@pytest.mark.parametrize("write", sorted(OWNING_EMBEDDING_WRITES))
def test_owning_embedding_write_inside_caller_transaction_refuses_and_keeps_it(
    project, write
):
    space_id, index_id = _add_index(project)
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        OWNING_EMBEDDING_WRITES[write](EmbeddingStore(project), space_id, index_id)

    _assert_caller_work_kept_uncommitted(project)


JOINING_EMBEDDING_WRITES = {
    "create_space": lambda store, _space_id, _index_id: store.create_space(
        _space_descriptor(), commit=False
    ),
    "create_index": lambda store, space_id, _index_id: store.create_index(
        name="joined",
        space_id=space_id,
        sheet_id=None,
        source_query={},
        source_columns=["value"],
        source_policy={"kind": "text_cell"},
    ),
    "delete_index": lambda store, _space_id, index_id: store.delete_index(index_id),
    "update_index_policy": lambda store, _space_id, index_id: store.update_index_policy(
        index_id, provider_policy={"allow_remote": False}
    ),
    "delete_space_if_orphan": lambda store, space_id, _index_id: (
        store.delete_space_if_orphan(space_id)
    ),
    "update_index_counts": lambda store, _space_id, index_id: store.update_index_counts(
        index_id, total=1, commit=False
    ),
}


@pytest.mark.parametrize("write", sorted(JOINING_EMBEDDING_WRITES))
def test_joining_embedding_write_requires_caller_transaction(project, write):
    space_id, index_id = _add_index(project)

    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        JOINING_EMBEDDING_WRITES[write](EmbeddingStore(project), space_id, index_id)

    assert not project.db.in_transaction


def test_joined_index_delete_rolls_back_with_its_caller(project):
    space_id, index_id = _add_index(project)
    store = EmbeddingStore(project)
    _open_caller_transaction(project)

    assert store.delete_index(index_id) is True
    assert store.delete_space_if_orphan(space_id) is True
    project.db.rollback()

    assert _committed_count(project, "embedding_indexes") == 1
    assert _committed_count(project, "embedding_spaces") == 1


# -- writes whose every production caller joins ------------------------------

_HASH = "sha256:" + "0" * 64


def _artifact(project) -> dict:
    return evidence.record_source_artifact(
        project, artifact_kind="web_page", media_type="text/html"
    )


JOINING_ONLY_WRITES = {
    "record_source_artifact": _artifact,
    "record_source_span": lambda project: evidence.record_source_span(
        project, artifact_id=1, span_kind="page"
    ),
    "record_text_surface": lambda project: evidence.record_text_surface(
        project,
        surface_kind="composite",
        content_hash=_HASH,
        offset_unit="unicode_codepoint",
        surface_ref={"identity": {"probe": 1}},
    ),
    "merge_artifact_metadata": lambda project: evidence.merge_artifact_metadata(
        project, 1, {"probe": True}
    ),
    "bind_to_run": lambda project: OutputColumnClaimStore(project).bind_to_run(
        claim_token="claim:none", run_id=1
    ),
    "set_provider_key": lambda project: project.set_provider_key(
        provider="openai", encrypted="ciphertext", hint="…key", spend_cap_micro=None
    ),
    "delete_provider_key": lambda project: project.delete_provider_key("openai"),
    "remember_success": lambda project: extraction_layouts.remember_success(
        project, 1, []
    ),
}


@pytest.mark.parametrize("write", sorted(JOINING_ONLY_WRITES))
def test_write_every_caller_joins_refuses_without_a_caller_transaction(project, write):
    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        JOINING_ONLY_WRITES[write](project)

    assert not project.db.in_transaction


def test_joined_evidence_rolls_back_with_its_caller(project):
    with pytest.raises(RuntimeError, match="requires a caller transaction"):
        _artifact(project)
    _open_caller_transaction(project)

    artifact = _artifact(project)
    surface = evidence.record_text_surface(
        project,
        surface_kind="composite",
        content_hash=_HASH,
        offset_unit="unicode_codepoint",
        surface_ref={"identity": {"probe": 1}},
    )
    evidence.record_source_span(
        project,
        artifact_id=int(artifact["id"]),
        span_kind="text",
        char_start=0,
        char_end=1,
        text_surface_id=int(surface["id"]),
        text_layer_hash=_HASH,
    )
    evidence.merge_artifact_metadata(project, int(artifact["id"]), {"probe": True})
    project.db.rollback()

    for table in ("source_artifacts", "text_surfaces", "source_spans"):
        assert _committed_count(project, table) == 0


def test_joined_provider_key_rolls_back_with_its_caller(project):
    _open_caller_transaction(project)

    project.set_provider_key(
        provider="openai", encrypted="ciphertext", hint="…key", spend_cap_micro=None
    )
    project.db.rollback()

    assert _committed_count(project, "project_provider_keys") == 0


def test_layout_selection_inside_caller_transaction_refuses_and_keeps_it(project):
    project.add_column(_sheet_id(project), "PDF", type="file")
    layout = extraction_layouts.save_layout(
        project, sheet_id=_sheet_id(project), source="PDF", draft={"fields": []}
    )
    _open_caller_transaction(project)

    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        extraction_layouts.select_layout(
            project, sheet_id=_sheet_id(project), source="PDF", layout_id=layout["id"]
        )

    _assert_caller_work_kept_uncommitted(project)
