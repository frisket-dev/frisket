"""Annotated-text coordinate-surface store schema.

`text_surfaces` is the coordinate surface a text span's char offsets index,
distinct from `source_artifacts` (provenance). This covers the fresh-create DDL,
the additive bundle migration onto a legacy bundle (mirrors
tests/engine/test_column_semantic_type.py), and the table's invariants
(cell/composite locator rule, content-hash shape, offset-unit enum, append-only
trigger, and the fresh-schema text-span CHECK).
"""

from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store import Project

_HASH = "sha256:" + "a" * 64


def _tables(db) -> set[str]:
    return {
        r["name"]
        for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _cols(db, table: str) -> set[str]:
    return {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}


def _indexes(db) -> set[str]:
    return {
        r["name"]
        for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }


def _triggers(db) -> set[str]:
    return {
        r["name"]
        for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }


class TestFreshBundle:
    def test_fresh_bundle_carries_surface_table_columns_and_indexes(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        try:
            assert "text_surfaces" in _tables(p.db)
            assert "text_surface_id" in _cols(p.db, "source_spans")
            assert "layer_family" in _cols(p.db, "evidence_links")
            idx = _indexes(p.db)
            assert "idx_text_surfaces_cell" in idx
            assert "idx_source_spans_text_surface" in idx
            assert "idx_evidence_link_spans_annotation" in idx
            triggers = _triggers(p.db)
            assert "trg_source_spans_char_integrity_insert" in triggers
            assert "trg_source_spans_char_integrity_update" in triggers
            assert "trg_text_surfaces_no_delete" in triggers
        finally:
            p.close()


def _rebuild_without(db, table: str, drop_cols: set[str]) -> None:
    """Rebuild an EMPTY table without ``drop_cols`` and without any table-level
    constraints, reproducing a pre-feature bundle. DROP COLUMN cannot remove a
    column named by a CHECK, so a real old bundle (which never had the column or
    CHECK) is simulated by a rename/create/drop. Tables are empty in this test so
    no row copy is needed."""
    info = list(db.execute(f"PRAGMA table_info({table})"))
    defs = []
    for r in info:
        if r["name"] in drop_cols:
            continue
        defs.append(
            f"{r['name']} INTEGER PRIMARY KEY"
            if r["pk"]
            else f"{r['name']} {r['type']}"
        )
    db.executescript(
        "PRAGMA foreign_keys=OFF;\n"
        f"ALTER TABLE {table} RENAME TO {table}__old;\n"
        f"CREATE TABLE {table} ({', '.join(defs)});\n"
        f"DROP TABLE {table}__old;\n"
        "PRAGMA foreign_keys=ON;\n"
    )
    db.commit()


class TestSurfaceInvariants:
    def _p(self, tmp_path):
        return Project.create(tmp_path / "inv.frisket", name="inv")

    def _insert(self, db, **over):
        cols = dict(
            stable_id="s1",
            surface_kind="cell",
            text_sheet_id=1,
            text_row_id=2,
            text_column_id=3,
            value_ref_json=None,
            content_hash=_HASH,
            offset_unit="unicode_codepoint",
        )
        cols.update(over)
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        db.execute(
            f"INSERT INTO text_surfaces ({keys}) VALUES ({marks})", tuple(cols.values())
        )
        db.commit()

    def test_cell_surface_requires_full_locator(self, tmp_path):
        p = self._p(tmp_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                self._insert(p.db, text_column_id=None)
        finally:
            p.close()

    def test_composite_surface_forbids_locator(self, tmp_path):
        p = self._p(tmp_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                self._insert(p.db, surface_kind="composite")  # locator still set
        finally:
            p.close()

    def test_composite_surface_with_null_locator_ok(self, tmp_path):
        p = self._p(tmp_path)
        try:
            self._insert(
                p.db,
                stable_id="c1",
                surface_kind="composite",
                text_sheet_id=None,
                text_row_id=None,
                text_column_id=None,
            )
        finally:
            p.close()

    def test_content_hash_shape_enforced(self, tmp_path):
        p = self._p(tmp_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                self._insert(p.db, content_hash="sha256:NOTHEX")
            with pytest.raises(sqlite3.IntegrityError):
                self._insert(p.db, content_hash="deadbeef")  # no prefix / wrong length
        finally:
            p.close()

    def test_offset_unit_enum_enforced(self, tmp_path):
        p = self._p(tmp_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                self._insert(p.db, offset_unit="bytes")
        finally:
            p.close()

    def test_surface_is_append_only(self, tmp_path):
        p = self._p(tmp_path)
        try:
            self._insert(p.db)
            with pytest.raises(sqlite3.IntegrityError):
                p.db.execute(
                    "UPDATE text_surfaces SET content_hash=? WHERE stable_id='s1'",
                    (_HASH,),
                )
            p.db.rollback()
            with pytest.raises(sqlite3.IntegrityError):
                p.db.execute("DELETE FROM text_surfaces WHERE stable_id='s1'")
        finally:
            p.close()


class TestSpanSurfaceFK:
    """Every character range names one matching text coordinate surface."""

    def _artifact(self, p) -> int:
        p.db.execute(
            "INSERT INTO source_artifacts (stable_id, artifact_kind, media_type) "
            "VALUES ('a1', 'text', 'text/plain')"
        )
        return p.db.execute("SELECT id FROM source_artifacts").fetchone()["id"]

    def test_surfaceless_unpaged_text_span_rejected(self, tmp_path):
        p = Project.create(tmp_path / "opt.frisket", name="opt")
        try:
            art = self._artifact(p)
            with pytest.raises(sqlite3.IntegrityError):
                p.db.execute(
                    "INSERT INTO source_spans "
                    "(stable_id, artifact_id, span_kind, char_start, char_end, quote) "
                    "VALUES ('sp1', ?, 'text', 0, 3, 'foo')",
                    (art,),
                )
            p.db.rollback()
        finally:
            p.close()

    def test_page_anchored_char_span_is_not_exempt(self, tmp_path):
        """A page locates a document region, not the string indexed by offsets."""
        p = Project.create(tmp_path / "pg.frisket", name="pg")
        try:
            art = self._artifact(p)
            with pytest.raises(sqlite3.IntegrityError):
                p.db.execute(
                    "INSERT INTO source_spans (stable_id, artifact_id, span_kind, "
                    "page_start, char_start, char_end, quote) "
                    "VALUES ('sp1', ?, 'text', 2, 0, 3, 'foo')",
                    (art,),
                )
        finally:
            p.close()

    def test_offsetless_text_span_allowed_without_surface(self, tmp_path):
        p = Project.create(tmp_path / "q.frisket", name="q")
        try:
            art = self._artifact(p)
            p.db.execute(
                "INSERT INTO source_spans "
                "(stable_id, artifact_id, span_kind, quote) VALUES ('sp1', ?, 'text', 'foo')",
                (art,),
            )
            p.db.commit()
            row = p.db.execute(
                "SELECT text_surface_id, char_start FROM source_spans WHERE stable_id='sp1'"
            ).fetchone()
            assert row["text_surface_id"] is None and row["char_start"] is None
        finally:
            p.close()

    def test_span_referencing_surface_round_trips(self, tmp_path):
        p = Project.create(tmp_path / "ref.frisket", name="ref")
        try:
            art = self._artifact(p)
            p.db.execute(
                "INSERT INTO text_surfaces "
                "(stable_id, surface_kind, text_sheet_id, text_row_id, text_column_id, "
                " content_hash, offset_unit) "
                "VALUES ('surf1', 'cell', 1, 2, 3, ?, 'unicode_codepoint')",
                (_HASH,),
            )
            sid = p.db.execute("SELECT id FROM text_surfaces").fetchone()["id"]
            p.db.execute(
                "INSERT INTO source_spans "
                "(stable_id, artifact_id, span_kind, char_start, char_end, quote, "
                " text_layer_hash, text_surface_id) "
                "VALUES ('sp1', ?, 'text', 0, 3, 'foo', ?, ?)",
                (art, _HASH, sid),
            )
            p.db.commit()
            row = p.db.execute(
                "SELECT text_surface_id FROM source_spans WHERE stable_id='sp1'"
            ).fetchone()
            assert row["text_surface_id"] == sid
        finally:
            p.close()

    def test_direct_sql_surface_hash_must_match(self, tmp_path):
        p = Project.create(tmp_path / "hash-match.frisket", name="hash-match")
        try:
            art = self._artifact(p)
            p.db.execute(
                "INSERT INTO text_surfaces "
                "(stable_id, surface_kind, text_sheet_id, text_row_id, "
                " text_column_id, content_hash, offset_unit) "
                "VALUES ('surf1', 'cell', 1, 2, 3, ?, 'unicode_codepoint')",
                (_HASH,),
            )
            sid = p.db.execute("SELECT id FROM text_surfaces").fetchone()["id"]
            with pytest.raises(sqlite3.IntegrityError):
                p.db.execute(
                    "INSERT INTO source_spans "
                    "(stable_id, artifact_id, span_kind, char_start, char_end, "
                    " text_layer_hash, text_surface_id) "
                    "VALUES ('bad-hash', ?, 'text', 0, 3, ?, ?)",
                    (art, "sha256:" + "b" * 64, sid),
                )
            p.db.rollback()
        finally:
            p.close()

    def test_coordinate_update_is_revalidated(self, tmp_path):
        p = Project.create(tmp_path / "update-guard.frisket", name="update-guard")
        try:
            art = self._artifact(p)
            p.db.execute(
                "INSERT INTO text_surfaces "
                "(stable_id, surface_kind, text_sheet_id, text_row_id, "
                " text_column_id, content_hash, offset_unit) "
                "VALUES ('surf1', 'cell', 1, 2, 3, ?, 'unicode_codepoint')",
                (_HASH,),
            )
            sid = p.db.execute("SELECT id FROM text_surfaces").fetchone()["id"]
            p.db.execute(
                "INSERT INTO source_spans "
                "(stable_id, artifact_id, span_kind, char_start, char_end, "
                " text_layer_hash, text_surface_id) "
                "VALUES ('span1', ?, 'text', 0, 3, ?, ?)",
                (art, _HASH, sid),
            )
            p.db.commit()
            with pytest.raises(sqlite3.IntegrityError):
                p.db.execute(
                    "UPDATE source_spans SET text_layer_hash=? WHERE stable_id='span1'",
                    ("sha256:" + "b" * 64,),
                )
            p.db.rollback()
            with pytest.raises(sqlite3.IntegrityError):
                p.db.execute(
                    "UPDATE source_spans SET char_end=0 WHERE stable_id='span1'"
                )
        finally:
            p.close()

    def test_dangling_surface_id_refused_by_fk(self, tmp_path):
        p = Project.create(tmp_path / "fk.frisket", name="fk")
        try:
            art = self._artifact(p)
            with pytest.raises(sqlite3.IntegrityError):
                p.db.execute(
                    "INSERT INTO source_spans "
                    "(stable_id, artifact_id, span_kind, char_start, char_end, quote, "
                    " text_layer_hash, text_surface_id) "
                    "VALUES ('sp1', ?, 'text', 0, 3, 'foo', ?, 999999)",
                    (art, _HASH),
                )
            p.db.rollback()
        finally:
            p.close()
