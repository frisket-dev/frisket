"""record_text_surface and the opt-in text_surface_id / layer_family threading
through record_source_span / record_citation.
"""

from __future__ import annotations

import pytest

from frisket.engine.store.citations import (
    _text_hash,
    record_citation,
    record_source_artifact,
    record_source_span,
    record_text_surface,
)
from frisket.engine.store.transactions import write_transaction

_HASH = _text_hash("hello world")
_HASH2 = _text_hash("something else")


def _artifact(p):
    with write_transaction(p.db):
        return record_source_artifact(p, artifact_kind="text", media_type="text/plain")


class TestRecordTextSurface:
    def test_cell_surface_round_trips(self, make_project):
        p = make_project("s")
        with write_transaction(p.db):
            s = record_text_surface(
                p,
                surface_kind="cell",
                content_hash=_HASH,
                offset_unit="unicode_codepoint",
                text_sheet_id=1,
                text_row_id=2,
                text_column_id=3,
                value_ref={"kind": "source_cell", "row_id": 2, "column_id": 3},
            )
        assert s["surface_kind"] == "cell"
        assert (s["text_sheet_id"], s["text_row_id"], s["text_column_id"]) == (
            1,
            2,
            3,
        )
        assert s["content_hash"] == _HASH
        assert s["value_ref"]["kind"] == "source_cell"

    def test_dedup_same_identity_returns_same_row(self, make_project):
        p = make_project("s")
        with write_transaction(p.db):
            a = record_text_surface(
                p,
                surface_kind="cell",
                content_hash=_HASH,
                offset_unit="unicode_codepoint",
                text_sheet_id=1,
                text_row_id=2,
                text_column_id=3,
            )
        with write_transaction(p.db):
            b = record_text_surface(
                p,
                surface_kind="cell",
                content_hash=_HASH,
                offset_unit="unicode_codepoint",
                text_sheet_id=1,
                text_row_id=2,
                text_column_id=3,
            )
        assert a["id"] == b["id"] == a["id"]
        assert p.db.execute("SELECT COUNT(*) c FROM text_surfaces").fetchone()["c"] == 1

    def test_different_content_hash_is_a_new_surface(self, make_project):
        p = make_project("s")
        with write_transaction(p.db):
            a = record_text_surface(
                p,
                surface_kind="cell",
                content_hash=_HASH,
                offset_unit="unicode_codepoint",
                text_sheet_id=1,
                text_row_id=2,
                text_column_id=3,
            )
        with write_transaction(p.db):
            b = record_text_surface(
                p,
                surface_kind="cell",
                content_hash=_HASH2,
                offset_unit="unicode_codepoint",
                text_sheet_id=1,
                text_row_id=2,
                text_column_id=3,
            )
        assert a["id"] != b["id"]

    def test_composite_surface_and_identity(self, make_project):
        p = make_project("s")
        with write_transaction(p.db):
            s = record_text_surface(
                p,
                surface_kind="composite",
                content_hash=_HASH,
                offset_unit="unicode_codepoint",
                surface_ref={"identity": {"renderer": "ner._text", "columns": [3, 4]}},
            )
        assert s["surface_kind"] == "composite"
        assert s["text_column_id"] is None
        assert s["surface_ref"]["identity"]["columns"] == [3, 4]

    def test_composite_diagnostics_do_not_change_identity(self, make_project):
        """Only surface_ref['identity'] enters the stable id — a diagnostics note
        must not fork the surface."""
        p = make_project("s")
        with write_transaction(p.db):
            a = record_text_surface(
                p,
                surface_kind="composite",
                content_hash=_HASH,
                offset_unit="unicode_codepoint",
                surface_ref={
                    "identity": {"renderer": "x"},
                    "diagnostics": {"note": "one"},
                },
            )
        with write_transaction(p.db):
            b = record_text_surface(
                p,
                surface_kind="composite",
                content_hash=_HASH,
                offset_unit="unicode_codepoint",
                surface_ref={
                    "identity": {"renderer": "x"},
                    "diagnostics": {"note": "two"},
                },
            )
        assert a["id"] == b["id"]

    def test_cell_without_locator_raises(self, make_project):
        p = make_project("s")
        with pytest.raises(ValueError):
            with write_transaction(p.db):
                record_text_surface(
                    p,
                    surface_kind="cell",
                    content_hash=_HASH,
                    offset_unit="unicode_codepoint",
                    text_sheet_id=1,
                    text_row_id=2,
                )

    def test_composite_with_locator_raises(self, make_project):
        p = make_project("s")
        with pytest.raises(ValueError):
            with write_transaction(p.db):
                record_text_surface(
                    p,
                    surface_kind="composite",
                    content_hash=_HASH,
                    offset_unit="unicode_codepoint",
                    text_column_id=3,
                )

    def test_composite_with_value_ref_raises(self, make_project):
        p = make_project("s")
        with pytest.raises(ValueError):
            with write_transaction(p.db):
                record_text_surface(
                    p,
                    surface_kind="composite",
                    content_hash=_HASH,
                    offset_unit="unicode_codepoint",
                    value_ref={"kind": "source_cell"},
                    surface_ref={"identity": {"renderer": "x"}},
                )

    def test_invalid_kind_raises(self, make_project):
        p = make_project("s")
        with pytest.raises(ValueError):
            with write_transaction(p.db):
                record_text_surface(
                    p,
                    surface_kind="page",
                    content_hash=_HASH,
                    offset_unit="unicode_codepoint",
                )

    def test_composite_without_identity_raises(self, make_project):
        """A composite must carry its provenance, or distinct compositions with the
        same text would silently collapse to one row."""
        p = make_project("s")
        with pytest.raises(ValueError):
            with write_transaction(p.db):
                record_text_surface(
                    p,
                    surface_kind="composite",
                    content_hash=_HASH,
                    offset_unit="unicode_codepoint",
                )
        with pytest.raises(ValueError):
            with write_transaction(p.db):
                record_text_surface(
                    p,
                    surface_kind="composite",
                    content_hash=_HASH,
                    offset_unit="unicode_codepoint",
                    surface_ref={"identity": {}},
                )

    def test_invalid_offset_unit_raises_valueerror(self, make_project):
        p = make_project("s")
        with pytest.raises(ValueError):
            with write_transaction(p.db):
                record_text_surface(
                    p,
                    surface_kind="cell",
                    content_hash=_HASH,
                    offset_unit="bytes",
                    text_sheet_id=1,
                    text_row_id=2,
                    text_column_id=3,
                )

    def test_invalid_content_hash_raises_valueerror(self, make_project):
        """A malformed hash is a clean ValueError, not a raw IntegrityError leaking
        through the concurrent-insert handler."""
        p = make_project("s")
        for bad in ("deadbeef", "sha256:NOTHEX", "sha256:" + "A" * 64):
            with pytest.raises(ValueError):
                with write_transaction(p.db):
                    record_text_surface(
                        p,
                        surface_kind="cell",
                        content_hash=bad,
                        offset_unit="unicode_codepoint",
                        text_sheet_id=1,
                        text_row_id=2,
                        text_column_id=3,
                    )

    def test_same_cell_differing_value_ref_dedups(self, make_project):
        """content_hash + locator fully define the coordinate space, and no reader
        consumes the surface's value_ref — so the same cell text at different value
        refs is ONE surface. value_ref must not fork the append-only row."""
        p = make_project("s")
        with write_transaction(p.db):
            a = record_text_surface(
                p,
                surface_kind="cell",
                content_hash=_HASH,
                offset_unit="unicode_codepoint",
                text_sheet_id=1,
                text_row_id=2,
                text_column_id=3,
                value_ref=None,
            )
        with write_transaction(p.db):
            b = record_text_surface(
                p,
                surface_kind="cell",
                content_hash=_HASH,
                offset_unit="unicode_codepoint",
                text_sheet_id=1,
                text_row_id=2,
                text_column_id=3,
                value_ref={"kind": "run_result", "run_id": 7},
            )
        assert a["id"] == b["id"]

    def test_different_locator_is_new_surface(self, make_project):
        p = make_project("s")
        with write_transaction(p.db):
            a = record_text_surface(
                p,
                surface_kind="cell",
                content_hash=_HASH,
                offset_unit="unicode_codepoint",
                text_sheet_id=1,
                text_row_id=2,
                text_column_id=3,
            )
        with write_transaction(p.db):
            b = record_text_surface(
                p,
                surface_kind="cell",
                content_hash=_HASH,
                offset_unit="unicode_codepoint",
                text_sheet_id=1,
                text_row_id=2,
                text_column_id=4,
            )
        assert a["id"] != b["id"]

    def test_ensure_inside_caller_transaction_is_usable(self, make_project):
        """A surface recorded inside a caller-owned transaction commits with it."""
        p = make_project("s")
        p.db.execute("BEGIN")
        s = record_text_surface(
            p,
            surface_kind="cell",
            content_hash=_HASH,
            offset_unit="unicode_codepoint",
            text_sheet_id=1,
            text_row_id=2,
            text_column_id=3,
        )
        assert p.db.in_transaction
        p.db.commit()
        assert (
            p.db.execute(
                "SELECT COUNT(*) c FROM text_surfaces WHERE id=?", (s["id"],)
            ).fetchone()["c"]
            == 1
        )


class TestSpanAndLinkThreading:
    def test_span_carries_surface_id(self, make_project):
        p = make_project("s")
        art = _artifact(p)
        with write_transaction(p.db):
            surf = record_text_surface(
                p,
                surface_kind="cell",
                content_hash=_HASH,
                offset_unit="unicode_codepoint",
                text_sheet_id=1,
                text_row_id=2,
                text_column_id=3,
            )
        with write_transaction(p.db):
            span = record_source_span(
                p,
                artifact_id=art["id"],
                span_kind="text",
                char_start=0,
                char_end=5,
                quote="hello",
                text_layer_hash=_HASH,
                text_surface_id=surf["id"],
            )
        assert span["text_surface_id"] == surf["id"]

    @pytest.mark.parametrize(
        ("start", "end", "surface", "layer_hash"),
        [
            (0, None, None, None),
            (-1, 2, None, None),
            (True, 2, None, None),
            (2, 2, None, None),
            (3, 2, None, None),
            (0, 2, None, _HASH),
        ],
    )
    def test_invalid_character_range_raises_cleanly(
        self, make_project, start, end, surface, layer_hash
    ):
        p = make_project("s")
        art = _artifact(p)
        with pytest.raises(ValueError):
            with write_transaction(p.db):
                record_source_span(
                    p,
                    artifact_id=art["id"],
                    span_kind="region",
                    page_start=1,
                    char_start=start,
                    char_end=end,
                    text_surface_id=surface,
                    text_layer_hash=layer_hash,
                )

    def test_surface_hash_mismatch_raises_cleanly(self, make_project):
        p = make_project("s")
        art = _artifact(p)
        with write_transaction(p.db):
            surf = record_text_surface(
                p,
                surface_kind="cell",
                content_hash=_HASH,
                offset_unit="unicode_codepoint",
                text_sheet_id=1,
                text_row_id=2,
                text_column_id=3,
            )
        with pytest.raises(ValueError, match="must match"):
            with write_transaction(p.db):
                record_source_span(
                    p,
                    artifact_id=art["id"],
                    span_kind="text",
                    char_start=0,
                    char_end=2,
                    text_surface_id=surf["id"],
                    text_layer_hash=_HASH2,
                )

    def test_link_carries_layer_family(self, make_project):
        p = make_project("s")
        art = _artifact(p)
        with write_transaction(p.db):
            span = record_source_span(
                p,
                artifact_id=art["id"],
                span_kind="text",
                quote="hello",
            )
        link = record_citation(
            p,
            subject_kind="cell",
            subject_ref={"row_id": 2, "column_id": 9},
            spans=[{"span_id": span["id"], "rank": 0, "span_role": "annotation"}],
            layer_family="entities",
        )
        row = p.db.execute(
            "SELECT layer_family FROM citations WHERE id=?", (link["id"],)
        ).fetchone()
        assert row["layer_family"] == "entities"
        js = p.db.execute(
            "SELECT span_role FROM citation_spans WHERE citation_id=?",
            (link["id"],),
        ).fetchone()
        assert js["span_role"] == "annotation"

    @pytest.mark.parametrize(
        "bad", ["Entities", " entities", "x" * 65, "", "foo bar", "a/b", "1abc", "x!"]
    )
    def test_invalid_layer_family_rejected(self, make_project, bad):
        p = make_project("s")
        art = _artifact(p)
        with write_transaction(p.db):
            span = record_source_span(
                p,
                artifact_id=art["id"],
                span_kind="text",
                quote="hello",
            )
        with pytest.raises(ValueError):
            record_citation(
                p,
                subject_kind="cell",
                subject_ref={},
                spans=[{"span_id": span["id"], "rank": 0}],
                layer_family=bad,
            )

    def test_link_without_family_stays_null(self, make_project):
        p = make_project("s")
        art = _artifact(p)
        with write_transaction(p.db):
            span = record_source_span(
                p,
                artifact_id=art["id"],
                span_kind="text",
                quote="hello",
            )
        link = record_citation(
            p,
            subject_kind="cell",
            subject_ref={},
            spans=[{"span_id": span["id"], "rank": 0}],
        )
        row = p.db.execute(
            "SELECT layer_family FROM citations WHERE id=?", (link["id"],)
        ).fetchone()
        assert row["layer_family"] is None
