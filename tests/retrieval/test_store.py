from __future__ import annotations

import sqlite3
import struct
from dataclasses import replace

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
            "failed": 1,
        }


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
        struct.pack("<2f", 0.6, 0.8),
        2,
    )
    with RetrievalStore(tmp_path).open() as store:
        store.put_representations((cached,))

        assert store.get_representations("space:demo", ("missing",)) == {}
        assert store.get_representations(
            "space:demo", ("input:cache-only", "input:cache-only")
        ) == {"input:cache-only": cached}
        assert store.db.execute("SELECT count(*) FROM parts").fetchone()[0] == 0


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
