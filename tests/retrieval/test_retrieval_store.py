from __future__ import annotations

import sqlite3
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from frisket.retrieval.contracts import SourceLocator, SourceRevision
from frisket.retrieval.store import (
    BindingDependency,
    CachedRepresentation,
    LexicalInput,
    NamedDatasetMember,
    PartRepresentation,
    RetrievalStore,
    SourcePublication,
    StoredBinding,
    StoredPart,
)


def _publication(revision: SourceRevision, *, token: str) -> SourcePublication:
    part = StoredPart(
        part_id=f"part:{token}",
        kind="text_passage",
        locator=SourceLocator(kind="text"),
        group_key=revision.source_id,
    )
    representation = CachedRepresentation(
        representation_id=f"representation:{token}",
        space_id="space:demo",
        input_hash=f"input:{token}",
        vector=struct.pack("<2f", 1.0, 0.0),
        dimension=2,
    )
    return SourcePublication(
        source_revision=revision,
        recipe_id="passages:v1",
        channel="text",
        parts=(part,),
        bindings=(
            StoredBinding(
                "binding:1",
                sheet_id=1,
                row_id=2,
                column_id=3,
                dependencies=(BindingDependency(2, 3, 11),),
            ),
        ),
        representations=(representation,),
        attachments=(
            PartRepresentation(part.part_id, representation.representation_id, "text"),
        ),
        lexical=LexicalInput(token, SourceLocator(kind="whole_source")),
        named_memberships=(
            NamedDatasetMember(
                "named:documents", part.part_id, representation.representation_id
            ),
        ),
    )


def test_store_uses_one_16k_contentless_sidecar_without_text_copy(tmp_path):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    with RetrievalStore(tmp_path).open() as store:
        store.begin_preparation(revision, recipe_id="passages:v1", channel="text")
        assert store.publish_source(_publication(revision, token="rare needle"))

        assert store.db.execute("PRAGMA page_size").fetchone()[0] == 16 * 1024
        schema = store.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='lexical_fts'"
        ).fetchone()[0]
        assert "contentless_delete=1" in schema.replace(" ", "")
        assert store.db.execute("SELECT content FROM lexical_fts").fetchone()[0] is None
        attention_index = store.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='preparation_attention'"
        ).fetchone()[0]
        assert "WHERE status!='ready'" in attention_index
        channel_plan = store.db.execute(
            "EXPLAIN QUERY PLAN SELECT source_id FROM preparation "
            "WHERE status IN ('pending','preparing') AND channel=? "
            "AND space_id IS ? "
            "ORDER BY CASE status WHEN 'preparing' THEN 0 ELSE 1 END,"
            "source_id,recipe_id,channel LIMIT 1",
            ("text", "space:demo"),
        ).fetchall()
        marker_plan = store.db.execute(
            "EXPLAIN QUERY PLAN SELECT source_id FROM preparation "
            "WHERE status IN ('pending','preparing') "
            "ORDER BY CASE status WHEN 'preparing' THEN 0 ELSE 1 END,"
            "source_id,recipe_id,channel LIMIT 1"
        ).fetchall()
        assert any(
            "preparation_channel_work_order" in str(row[3]) for row in channel_plan
        )
        assert any("preparation_work_order" in str(row[3]) for row in marker_plan)
        assert all(
            "TEMP B-TREE" not in str(row[3]) for row in (*channel_plan, *marker_plan)
        )
        assert (
            store.db.execute(
                "SELECT count(*) FROM lexical_fts WHERE lexical_fts MATCH 'needle'"
            ).fetchone()[0]
            == 1
        )
        assert (
            store.db.execute(
                "SELECT count(*) FROM named_dataset_membership "
                "WHERE dataset_id='named:documents'"
            ).fetchone()[0]
            == 1
        )

    assert (tmp_path / "project.retrieval.db").is_file()


@pytest.mark.parametrize("existing_without_index", [False, True])
def test_binding_page_index_preserves_existing_retrieval_data(
    tmp_path, existing_without_index
):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    tables = (
        "retrieval_meta",
        "sources",
        "parts",
        "bindings",
        "binding_dependencies",
        "representations",
        "part_representations",
        "lexical_sources",
        "preparation",
        "named_dataset_membership",
    )

    def stored_rows(store):
        return {
            table: [
                tuple(row)
                for row in store.db.execute(f"SELECT * FROM {table} ORDER BY 1")
            ]
            for table in tables
        }

    with RetrievalStore(tmp_path).open() as store:
        store.begin_preparation(revision, recipe_id="passages:v1", channel="text")
        assert store.publish_source(_publication(revision, token="rare needle"))
        before = stored_rows(store)
        if existing_without_index:
            # A current sidecar created before this additive index migration.
            store.db.execute("DROP INDEX bindings_column_row")

    with RetrievalStore(tmp_path).open() as store:
        assert stored_rows(store) == before
        assert (
            store.db.execute(
                "SELECT count(*) FROM lexical_fts WHERE lexical_fts MATCH 'needle'"
            ).fetchone()[0]
            == 1
        )
        page_sql = (
            "SELECT row_id,min(sheet_id) AS sheet_id FROM bindings "
            "WHERE column_id=? AND row_id>? AND row_id<=? "
            "GROUP BY row_id ORDER BY row_id LIMIT ?"
        )
        params = (3, 1, 100, 500)
        assert [tuple(row) for row in store.db.execute(page_sql, params)] == [(2, 1)]
        page_plan = store.db.execute("EXPLAIN QUERY PLAN " + page_sql, params)
        column_plan = store.db.execute(
            "EXPLAIN QUERY PLAN SELECT column_id FROM bindings "
            "WHERE column_id>? ORDER BY column_id LIMIT 1",
            (0,),
        )
        for plan in (page_plan.fetchall(), column_plan.fetchall()):
            details = [str(row[3]) for row in plan]
            assert any(
                "SEARCH bindings USING COVERING INDEX bindings_column_row" in detail
                for detail in details
            )
            assert all("TEMP B-TREE" not in detail for detail in details)


def test_publication_batch_commits_once_and_is_visible_only_after_commit(tmp_path):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    with (
        RetrievalStore(tmp_path).open() as store,
        RetrievalStore(tmp_path).open() as reader,
    ):
        statements = []
        store.db.set_trace_callback(statements.append)
        with store.publication_batch():
            store.begin_preparation(revision, recipe_id="passages:v1", channel="text")
            assert store.publish_source(_publication(revision, token="needle"))
            store.remove_cell_bindings_except(
                sheet_id=1, row_id=2, column_id=3, keep_binding_ids=("binding:1",)
            )
            assert reader.db.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
        assert reader.db.execute("SELECT count(*) FROM sources").fetchone()[0] == 1
        assert statements.count("BEGIN IMMEDIATE") == 1
        assert statements.count("COMMIT") == 1


def test_failed_publication_aborts_batch_even_when_caught(tmp_path, monkeypatch):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    with RetrievalStore(tmp_path).open() as store:
        replace_lexical = store._replace_lexical

        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("publication failed")

        monkeypatch.setattr(store, "_replace_lexical", fail)
        with pytest.raises(RuntimeError, match="publication batch failed"):
            with store.publication_batch():
                store.begin_preparation(
                    revision, recipe_id="passages:v1", channel="text"
                )
                with pytest.raises(
                    sqlite3.OperationalError, match="publication failed"
                ):
                    store.publish_source(_publication(revision, token="needle"))
                with pytest.raises(RuntimeError, match="publication batch failed"):
                    store.queue_preparation(
                        revision, recipe_id="retry:v1", channel="text"
                    )
        assert not store.db.in_transaction
        for table in ("sources", "parts", "bindings", "preparation", "lexical_fts"):
            assert store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0

        monkeypatch.setattr(store, "_replace_lexical", replace_lexical)
        with store.publication_batch():
            store.begin_preparation(revision, recipe_id="passages:v1", channel="text")
            assert store.publish_source(_publication(revision, token="needle"))
        assert store.db.execute("SELECT count(*) FROM sources").fetchone()[0] == 1


@pytest.mark.parametrize("first_mutation", ["begin", "queue", "register", "publish"])
def test_sqlite_rollback_refuses_every_subsequent_batch_mutation(
    tmp_path, first_mutation
):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    publication = _publication(revision, token="needle")
    with RetrievalStore(tmp_path).open() as store:
        # Trigger the same transaction loss possible after SQLITE_FULL/interrupt.
        store.db.execute(
            "CREATE TEMP TRIGGER rollback_preparation BEFORE INSERT ON preparation "
            "BEGIN SELECT RAISE(ROLLBACK, 'forced rollback'); END"
        )
        mutations = {
            "begin": lambda: store.begin_preparation(
                revision, recipe_id="passages:v1", channel="text"
            ),
            "queue": lambda: store.queue_preparation(
                revision, recipe_id="passages:v1", channel="text"
            ),
            "register": lambda: store.register_preparation(
                revision, publication.bindings, recipe_id="passages:v1", channel="text"
            ),
            "publish": lambda: store.publish_source(publication),
        }
        # Public direct-SQL preparation methods must detect transaction loss too.
        with pytest.raises(RuntimeError, match="publication batch failed"):
            with store.publication_batch():
                with pytest.raises(sqlite3.IntegrityError, match="forced rollback"):
                    store.begin_preparation(
                        revision, recipe_id="passages:v1", channel="text"
                    )
                with pytest.raises(RuntimeError, match="publication batch failed"):
                    mutations[first_mutation]()
                for mutation in mutations.values():
                    with pytest.raises(RuntimeError, match="publication batch failed"):
                        mutation()
        assert not store.db.in_transaction
        for table in ("sources", "parts", "bindings", "preparation", "lexical_fts"):
            assert store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        store.db.execute("DROP TRIGGER rollback_preparation")
        with store.publication_batch():
            mutations["begin"]()
            assert mutations["publish"]()
        assert store.db.execute("SELECT count(*) FROM sources").fetchone()[0] == 1


def test_publication_batch_rejects_external_and_nested_transactions(tmp_path):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    with RetrievalStore(tmp_path).open() as store:
        store.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="owns its write transaction"):
            with store.publication_batch():
                pytest.fail("entered a batch inside an external transaction")
        with pytest.raises(RuntimeError, match="owns its write transaction"):
            store.publish_source(_publication(revision, token="needle"))
        assert store.db.in_transaction
        store.db.rollback()
        with store.publication_batch():
            with pytest.raises(RuntimeError, match="cannot be nested"):
                with store.publication_batch():
                    pytest.fail("entered a nested batch")
        assert not store.db.in_transaction


@pytest.mark.parametrize(
    "error_code",
    [
        "blob_integrity_failed",
        "encoding_failed",
        "media_decode_failed",
        "missing_blob",
        "missing_media_tool",
        "pdf_render_failed",
        "stale_source",
    ],
)
def test_source_event_requeues_only_retryable_failed_target(
    tmp_path, error_code: str
) -> None:
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    with RetrievalStore(tmp_path).open() as store:
        store.begin_preparation(revision, recipe_id="passages:v1", channel="text")
        assert store.fail_preparation(
            revision,
            recipe_id="passages:v1",
            channel="text",
            error_code=error_code,
            error_message="retry after a new source event",
        )

        store.queue_preparation(revision, recipe_id="passages:v1", channel="text")

        state = store.preparation_state(
            revision.source_id, recipe_id="passages:v1", channel="text"
        )
        assert state is not None
        assert (state.status, state.error_code) == ("pending", None)


def test_source_event_does_not_retry_invalid_input(tmp_path) -> None:
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    with RetrievalStore(tmp_path).open() as store:
        store.begin_preparation(revision, recipe_id="passages:v1", channel="text")
        assert store.fail_preparation(
            revision,
            recipe_id="passages:v1",
            channel="text",
            error_code="invalid_input",
            error_message="the source bytes are not decodable",
        )

        store.queue_preparation(revision, recipe_id="passages:v1", channel="text")

        state = store.preparation_state(
            revision.source_id, recipe_id="passages:v1", channel="text"
        )
        assert state is not None
        assert (state.status, state.error_code) == ("failed", "invalid_input")


def test_concurrent_fresh_store_open_serializes_schema_creation(tmp_path):
    workers = 12
    ready = Barrier(workers)

    def open_store() -> str:
        ready.wait()
        with RetrievalStore(tmp_path).open() as store:
            return store.sidecar_id()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        sidecar_ids = tuple(executor.map(lambda _index: open_store(), range(workers)))

    assert len(set(sidecar_ids)) == 1


def test_current_store_open_does_not_wait_for_existing_writer(tmp_path):
    with RetrievalStore(tmp_path).open() as store:
        sidecar_id = store.sidecar_id()

    writer = sqlite3.connect(tmp_path / "project.retrieval.db", isolation_level=None)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("BEGIN IMMEDIATE")

        started = time.monotonic()  # realtime: bound actual SQLite lock waiting
        with RetrievalStore(tmp_path).open(busy_timeout_ms=0) as reader:
            assert reader.sidecar_id() == sidecar_id
        elapsed = time.monotonic() - started  # realtime: assert nonblocking open

        assert elapsed < 1
    finally:
        writer.rollback()
        writer.close()


def test_relations_use_numeric_keys_while_public_ids_remain_unique(tmp_path):
    with RetrievalStore(tmp_path).open() as store:
        columns = {
            table: {
                row["name"] for row in store.db.execute(f"PRAGMA table_info({table})")
            }
            for table in (
                "sources",
                "parts",
                "bindings",
                "binding_dependencies",
                "representations",
                "part_representations",
                "named_dataset_membership",
            )
        }

        assert {"source_key", "source_id"} <= columns["sources"]
        assert {"part_key", "part_id", "source_key"} <= columns["parts"]
        assert {"binding_key", "binding_id", "source_key"} <= columns["bindings"]
        assert columns["binding_dependencies"] >= {"binding_key"}
        assert {"representation_key", "representation_id"} <= columns["representations"]
        assert columns["part_representations"] >= {
            "part_key",
            "representation_key",
        }
        assert columns["named_dataset_membership"] >= {
            "part_key",
            "representation_key",
        }
        assert "source_id" not in columns["parts"] | columns["bindings"]
        assert "part_id" not in columns["part_representations"]


def test_stale_worker_cannot_publish_over_new_target(tmp_path):
    old = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    new = SourceRevision("cell:2:3", "text", "cell_revision", 12)
    with RetrievalStore(tmp_path).open() as store:
        store.begin_preparation(old, recipe_id="passages:v1", channel="text")
        store.begin_preparation(new, recipe_id="passages:v1", channel="text")

        assert not store.publish_source(_publication(old, token="stale needle"))
        assert store.publish_source(_publication(new, token="fresh needle"))
        assert store.db.execute("SELECT count(*) FROM parts").fetchone()[0] == 1
        assert (
            store.db.execute(
                "SELECT count(*) FROM lexical_fts WHERE lexical_fts MATCH 'stale'"
            ).fetchone()[0]
            == 0
        )
        state = store.preparation_state(
            new.source_id, recipe_id="passages:v1", channel="text"
        )
        assert state is not None
        assert (state.target_version_value, state.status, state.prepared_parts) == (
            12,
            "ready",
            1,
        )


def test_republishing_revision_replaces_old_postings_atomically(tmp_path):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    with RetrievalStore(tmp_path).open() as store:
        store.begin_preparation(revision, recipe_id="passages:v1", channel="text")
        assert store.publish_source(_publication(revision, token="firsttoken"))
        store.begin_preparation(revision, recipe_id="passages:v1", channel="text")
        assert store.publish_source(_publication(revision, token="secondtoken"))

        assert (
            store.db.execute(
                "SELECT count(*) FROM lexical_fts WHERE lexical_fts MATCH 'firsttoken'"
            ).fetchone()[0]
            == 0
        )
        assert (
            store.db.execute(
                "SELECT count(*) FROM lexical_fts WHERE lexical_fts MATCH 'secondtoken'"
            ).fetchone()[0]
            == 1
        )
        assert store.db.execute("SELECT count(*) FROM parts").fetchone()[0] == 2


def test_failed_preparation_is_conditional_and_counts_are_maintained(tmp_path):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    newer = SourceRevision("cell:2:3", "text", "cell_revision", 12)
    with RetrievalStore(tmp_path).open() as store:
        store.begin_preparation(revision, recipe_id="passages:v1", channel="text")
        store.begin_preparation(newer, recipe_id="passages:v1", channel="text")

        assert not store.fail_preparation(
            revision,
            recipe_id="passages:v1",
            channel="text",
            error_code="cancelled",
            error_message="old work cancelled",
        )
        assert store.fail_preparation(
            newer,
            recipe_id="passages:v1",
            channel="text",
            error_code="encoder_unavailable",
            error_message="install the selected encoder",
        )
        assert store.readiness_counts(recipe_id="passages:v1", channel="text") == {
            "pending": 0,
            "preparing": 0,
            "ready": 0,
            "unavailable": 0,
            "failed": 1,
        }


def test_preparation_cursor_resumes_and_retargets_conditionally(tmp_path):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    newer = SourceRevision("cell:2:3", "text", "cell_revision", 12)
    with RetrievalStore(tmp_path).open() as store:
        store.begin_preparation(revision, recipe_id="passages:v1", channel="text")
        assert store.publish_source(_publication(revision, token="seed"))
        store.queue_preparation(revision, recipe_id="passages:v2", channel="text")
        state = store.next_preparation(channel="text")
        assert state is not None and state.status == "pending"
        assert state.source_revision == revision

        store.begin_preparation(revision, recipe_id="passages:v2", channel="text")
        assert store.advance_preparation(
            revision,
            recipe_id="passages:v2",
            channel="text",
            cursor_json='{"next_offset":128}',
        )
        store.queue_preparation(revision, recipe_id="passages:v2", channel="text")
        resumed = store.preparation_state(
            revision.source_id, recipe_id="passages:v2", channel="text"
        )
        assert resumed is not None
        assert (resumed.status, resumed.cursor_json) == (
            "pending",
            '{"next_offset":128}',
        )

        store.queue_preparation(newer, recipe_id="passages:v2", channel="text")
        retargeted = store.preparation_state(
            revision.source_id, recipe_id="passages:v2", channel="text"
        )
        assert retargeted is not None
        assert (retargeted.target_version_value, retargeted.cursor_json) == (12, None)
        assert not store.advance_preparation(
            revision,
            recipe_id="passages:v2",
            channel="text",
            cursor_json='{"next_offset":256}',
        )


def test_preparation_space_is_an_exact_target_and_retarget_clears_cursor(tmp_path):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    with RetrievalStore(tmp_path).open() as store:
        store.register_preparation(
            revision,
            (
                StoredBinding(
                    "binding:1",
                    sheet_id=1,
                    row_id=2,
                    column_id=3,
                    dependencies=(BindingDependency(2, 3, 11),),
                ),
            ),
            recipe_id="passages:v1",
            channel="text",
            space_id="space:first",
        )
        assert store.advance_preparation(
            revision,
            recipe_id="passages:v1",
            channel="text",
            space_id="space:first",
            cursor_json='{"next_offset":128}',
        )

        store.queue_preparation(
            revision,
            recipe_id="passages:v1",
            channel="text",
            space_id="space:second",
        )

        state = store.preparation_state(
            revision.source_id, recipe_id="passages:v1", channel="text"
        )
        assert state is not None
        assert (state.space_id, state.status, state.cursor_json) == (
            "space:second",
            "pending",
            None,
        )
        assert store.next_preparation(channel="text", space_id="space:first") is None
        selected = store.next_preparation(channel="text", space_id="space:second")
        assert selected is not None and selected.space_id == "space:second"


def test_unavailable_preparation_requires_explicit_requeue(tmp_path):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    with RetrievalStore(tmp_path).open() as store:
        store.begin_preparation(revision, recipe_id="passages:v1", channel="text")
        assert store.publish_source(_publication(revision, token="seed"))
        store.queue_preparation(revision, recipe_id="passages:v2", channel="text")
        assert store.mark_preparation_unavailable(
            revision,
            recipe_id="passages:v2",
            channel="text",
            reason="install the local encoder",
        )
        store.queue_preparation(revision, recipe_id="passages:v2", channel="text")

        state = store.preparation_state(
            revision.source_id, recipe_id="passages:v2", channel="text"
        )
        assert state is not None and state.status == "unavailable"
        assert store.next_preparation(channel="text") is None
        assert store.requeue_unavailable(channel="text") == 1
        assert store.next_preparation(channel="text").status == "pending"


def test_registration_atomically_exposes_bound_pending_source(tmp_path):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    binding = StoredBinding(
        "binding:1",
        sheet_id=1,
        row_id=2,
        column_id=3,
        dependencies=(BindingDependency(2, 3, 11),),
    )
    with RetrievalStore(tmp_path).open() as store:
        store.register_preparation(
            revision,
            (binding,),
            recipe_id="passages:v1",
            channel="text",
        )

        work = store.next_preparation(channel="text")
        assert work is not None and work.source_revision == revision
        assert store.db.execute("SELECT count(*) FROM sources").fetchone()[0] == 1
        assert store.db.execute("SELECT count(*) FROM bindings").fetchone()[0] == 1
        assert (
            store.db.execute("SELECT count(*) FROM binding_dependencies").fetchone()[0]
            == 1
        )


def test_failed_registration_rolls_back_source_binding_and_work(tmp_path):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    binding = StoredBinding(
        "binding:1",
        sheet_id=1,
        row_id=2,
        column_id=3,
        dependencies=(BindingDependency(2, 3, 11),),
    )
    with RetrievalStore(tmp_path).open() as store:
        store.db.executescript(
            "CREATE TRIGGER reject_preparation BEFORE INSERT ON preparation "
            "BEGIN SELECT RAISE(ABORT, 'injected preparation failure'); END;"
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            store.register_preparation(
                revision,
                (binding,),
                recipe_id="passages:v1",
                channel="text",
            )

        assert store.db.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM bindings").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM preparation").fetchone()[0] == 0


def test_opened_sidecar_has_no_authoritative_text_table(tmp_path):
    with RetrievalStore(tmp_path).open() as store:
        tables = {
            row[0]
            for row in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }

    assert "search_content" not in tables
    assert "source_text" not in tables
    read = sqlite3.connect(tmp_path / "project.retrieval.db")
    try:
        assert read.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        read.close()


def test_bounded_batches_append_then_finalize_without_erasing_prior_parts(tmp_path):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    first = replace(_publication(revision, token="first"), complete=False, lexical=None)
    second_part = StoredPart("part:second", "text_passage", SourceLocator(kind="text"))
    second_representation = CachedRepresentation(
        "representation:second",
        "space:demo",
        "input:second",
        struct.pack("<2f", 0.0, 1.0),
        2,
    )
    second = SourcePublication(
        source_revision=revision,
        recipe_id="passages:v1",
        channel="text",
        parts=(second_part,),
        bindings=(
            StoredBinding(
                "binding:2",
                sheet_id=1,
                row_id=4,
                column_id=3,
                dependencies=(BindingDependency(4, 3, 11),),
            ),
        ),
        representations=(second_representation,),
        attachments=(
            PartRepresentation(
                second_part.part_id, second_representation.representation_id, "text"
            ),
        ),
        complete=True,
    )
    with RetrievalStore(tmp_path).open() as store:
        store.begin_preparation(revision, recipe_id="passages:v1", channel="text")
        assert store.publish_source(first)
        state = store.preparation_state(
            revision.source_id, recipe_id="passages:v1", channel="text"
        )
        assert state is not None and state.status == "preparing"
        assert store.publish_source(second)

        assert store.db.execute("SELECT count(*) FROM parts").fetchone()[0] == 2
        assert store.db.execute("SELECT count(*) FROM bindings").fetchone()[0] == 2
        state = store.preparation_state(
            revision.source_id, recipe_id="passages:v1", channel="text"
        )
        assert state is not None
        assert (state.status, state.prepared_parts) == ("ready", 2)


def test_dataset_membership_and_binding_removal_have_explicit_ownership(tmp_path):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    publication = _publication(revision, token="member")
    part = publication.parts[0]
    representation = publication.representations[0]
    second_dataset = NamedDatasetMember(
        "named:secondary", part.part_id, representation.representation_id
    )
    with RetrievalStore(tmp_path).open() as store:
        store.begin_preparation(revision, recipe_id="passages:v1", channel="text")
        assert store.publish_source(publication)
        store.replace_named_dataset_members("named:secondary", (second_dataset,))
        assert {
            row[0]
            for row in store.db.execute(
                "SELECT dataset_id FROM named_dataset_membership"
            )
        } == {"named:documents", "named:secondary"}

        store.replace_named_dataset_members("named:documents", ())
        assert (
            store.db.execute(
                "SELECT count(*) FROM named_dataset_membership "
                "WHERE dataset_id='named:secondary'"
            ).fetchone()[0]
            == 1
        )
        assert store.remove_bindings(("binding:1",)) == 1
        assert store.db.execute("SELECT count(*) FROM parts").fetchone()[0] == 1


def test_representation_cache_is_independent_of_located_parts(tmp_path):
    cached = CachedRepresentation(
        "representation:cache-only",
        "space:demo",
        "input:cache-only",
        struct.pack("<2f", 2.0, 0.0),
        2,
    )
    with RetrievalStore(tmp_path).open() as store:
        store.put_representations((cached,))

        assert store.get_representations("space:demo", ("missing",)) == {}
        assert store.get_representations(
            "space:demo", ("input:cache-only", "input:cache-only")
        ) == {"input:cache-only": cached}
        assert store.db.execute("SELECT count(*) FROM parts").fetchone()[0] == 0

        invalid = replace(
            cached,
            representation_id="representation:not-finite",
            input_hash="input:not-finite",
            vector=struct.pack("<2f", float("nan"), 0.0),
        )
        with pytest.raises(ValueError, match="finite"):
            store.put_representations((invalid,))


def test_named_dataset_vector_read_is_bounded_and_ordered(tmp_path):
    revision = SourceRevision("cell:2:3", "text", "cell_revision", 11)
    with RetrievalStore(tmp_path).open() as store:
        store.begin_preparation(revision, recipe_id="passages:v1", channel="text")
        assert store.publish_source(_publication(revision, token="member"))

        page = store.read_named_dataset_vectors(
            "named:documents", space_id="space:demo", limit=1
        )

    assert len(page) == 1
    assert page[0].part_id == "part:member"
    assert page[0].channel == "text"
    assert page[0].representation.input_hash == "input:member"
