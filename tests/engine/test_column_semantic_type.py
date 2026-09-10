"""columns.semantic_type: an explicit, NEVER-inferred marker for columns whose
contents follow a named contract (currently only 'entity_mentions', written
by map.ner) -- distinct from `type`, the physical storage type.

Layer 1 (store): the fresh-create DDL carries the column (defaulting to
NULL), a pre-migration bundle is repaired additively on open (mirrors
tests/engine/test_sheet_title_column.py's TestTitleColumnIdMigration
"hand-build a legacy bundle, open it, assert it migrated" pattern), and
Project.add_column round-trips the value through cells.py.
"""

from __future__ import annotations


import pytest

from frisket.engine.store import Project


class TestFreshBundleSemanticType:
    def test_fresh_bundle_carries_the_column_defaulting_to_null(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        sheet = p.add_sheet("data")
        p.add_column(sheet, "name")

        cols = {r["name"] for r in p.db.execute("PRAGMA table_info(columns)")}
        assert "semantic_type" in cols

        row = p.columns(sheet)[0]
        assert row["semantic_type"] is None
        p.close()


class TestAddColumnSemanticTypeRoundTrip:
    def test_add_column_with_semantic_type_round_trips(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        sheet = p.add_sheet("data")
        col_id = p.add_column(sheet, "mentions", semantic_type="entity_mentions")

        row = p.get_column(col_id)
        assert row["semantic_type"] == "entity_mentions"
        assert p.columns(sheet)[0]["semantic_type"] == "entity_mentions"
        p.close()

    def test_add_column_without_semantic_type_stays_null(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        sheet = p.add_sheet("data")
        col_id = p.add_column(sheet, "plain")

        row = p.get_column(col_id)
        assert row["semantic_type"] is None
        p.close()

    def test_set_column_semantic_type_updates_and_clears(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        sheet = p.add_sheet("data")
        col_id = p.add_column(sheet, "mentions")
        assert p.get_column(col_id)["semantic_type"] is None

        p.set_column_semantic_type(col_id, "entity_mentions")
        assert p.get_column(col_id)["semantic_type"] == "entity_mentions"

        p.set_column_semantic_type(col_id, None)
        assert p.get_column(col_id)["semantic_type"] is None
        p.close()

    def test_set_column_semantic_type_rejects_unknown_column(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        with pytest.raises(KeyError):
            p.set_column_semantic_type(999999, "entity_mentions")
        p.close()


def test_revived_column_converges_its_semantic_marker(tmp_path) -> None:
    """A column hidden by undo and re-created by name must take the NEW
    producer's marker. Keeping a stale one makes the marker a lie; dropping a
    declared one reads as "no entity column", which is indistinguishable from
    clean data."""
    project = Project.create(tmp_path / "revive.frisket")
    sheet_id = project.add_sheet("s")

    column_id = project.add_column(
        sheet_id,
        "entities",
        type="json",
        ai_generated=True,
        semantic_type="entity_mentions",
    )
    project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (column_id,))
    project.db.commit()

    # Re-created by a producer that declares no contract: marker must clear.
    revived = project.add_column(sheet_id, "entities", type="text", ai_generated=True)
    assert revived == column_id
    row = project.db.execute(
        "SELECT semantic_type FROM columns WHERE id=?", (column_id,)
    ).fetchone()
    assert row["semantic_type"] is None

    project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (column_id,))
    project.db.commit()

    # Re-created by map.ner: marker must be set.
    project.add_column(
        sheet_id,
        "entities",
        type="json",
        ai_generated=True,
        semantic_type="entity_mentions",
    )
    row = project.db.execute(
        "SELECT semantic_type FROM columns WHERE id=?", (column_id,)
    ).fetchone()
    assert row["semantic_type"] == "entity_mentions"
