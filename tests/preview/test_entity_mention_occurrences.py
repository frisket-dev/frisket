"""Route B: WHERE inside one document a mention occurs.

Two things are load-bearing here and neither is the happy path.

1. The snippet is cut from the CURRENT cell text at coordinates the substrate
   still vouches for. A cell edited after extraction must yield the mismatch
   reason and no snippets — a window cut at stale offsets is the confident-wrong
   highlight the whole coordinate surface exists to prevent (D9).
2. Offsets cross a unit boundary. Spans arrive UTF-16 (what a JS string
   indexes), the window is cut on code points (so it cannot split a surrogate
   pair), and the returned `mark_start`/`mark_end` are UTF-16 offsets INTO THE
   SNIPPET. An astral character anywhere before the mark moves those numbers,
   so the astral case is pinned rather than assumed.
"""

from __future__ import annotations

import pytest

from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    _text_hash,
    record_evidence_link,
    record_source_artifact,
    record_source_span,
    record_text_surface,
)
from frisket.preview.entity_mention_detail import (
    EntityMentionDetailError,
    resolve_entity_mention_documents,
    resolve_entity_mention_occurrences,
)

BODY = (
    "The committee convened by Ada Lovelace reviewed the motion. "
    "A. Lovelace abstained on item 4, and Ada Lovelace closed the session."
)


def _utf16_slice(snippet: dict) -> str:
    """The marked run of a snippet, indexed the way the client indexes it."""
    units = snippet["text"].encode("utf-16-le")
    return units[snippet["mark_start"] * 2 : snippet["mark_end"] * 2].decode(
        "utf-16-le"
    )


def _write_layer(
    project, *, sheet, body_col, ents_col, row_id, text, spans_in, content_hash=None
):
    """One NER-shaped annotation layer over a cell, written through the store
    writers directly — the same construction tests/engine/test_text_annotations_
    query.py uses, so this exercises the real join rather than a fixture."""
    surface_hash = content_hash or _text_hash(text)
    surface = record_text_surface(
        project,
        surface_kind="cell",
        content_hash=surface_hash,
        offset_unit="unicode_codepoint",
        text_sheet_id=sheet,
        text_row_id=row_id,
        text_column_id=body_col,
    )
    artifact = record_source_artifact(
        project, artifact_kind="text", media_type="text/plain"
    )
    spans = [
        record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="text",
            char_start=start,
            char_end=end,
            quote=text[start:end],
            text_layer_hash=surface_hash,
            text_surface_id=surface["id"],
            metadata={"entity_type": entity_type},
        )
        for start, end, entity_type in spans_in
    ]
    _values, refs = project.get_values_with_refs(sheet, ents_col, row_ids=[row_id])
    return record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref=refs[row_id],
        spans=[
            {"span_id": span["id"], "rank": i, "span_role": "annotation"}
            for i, span in enumerate(spans)
        ],
        sheet_id=sheet,
        row_id=row_id,
        column_id=ents_col,
        layer_family="entities",
        producer={"kind": "map.ner", "engine": "gliner"},
    )


def _seed(tmp_path, *, body_text: str = BODY, content_hash=None):
    project = Project.create(tmp_path / "occ.frisket", name="occ")
    sheet = project.add_sheet("Docs")
    body_col = project.add_column(sheet, "body", type="text")
    ents_col = project.add_column(sheet, "entities", type="json")
    project.set_column_semantic_type(ents_col, "entity_mentions")

    def person(text, fingerprint):
        return {"text": text, "type": "person", "fingerprint": fingerprint}

    project.add_rows(
        sheet,
        [
            {
                "body": body_text,
                "entities": [
                    person("Ada Lovelace", "ada lovelace"),
                    person("A. Lovelace", "ada lovelace"),
                    person("Ada Lovelace", "ada lovelace"),
                    {"text": "item 4", "type": "ordinal"},
                ],
            }
        ],
        {"body": body_col, "entities": ents_col},
    )
    row_id = project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? ORDER BY id LIMIT 1", (sheet,)
    ).fetchone()["id"]
    # Spans are located in whatever body_text a case supplies; a shorter body
    # simply carries fewer of them rather than forcing a second fixture.
    wanted = [
        ("Ada Lovelace", "person", False),
        ("A. Lovelace", "person", False),
        ("Ada Lovelace", "person", True),
        ("item 4", "ordinal", False),
    ]
    spans_in = []
    for needle, entity_type, from_end in wanted:
        find = body_text.rfind if from_end else body_text.find
        start = find(needle)
        if start < 0 or any(start == s for s, _e, _t in spans_in):
            continue
        spans_in.append((start, start + len(needle), entity_type))
    _write_layer(
        project,
        sheet=sheet,
        body_col=body_col,
        ents_col=ents_col,
        row_id=row_id,
        text=body_text,
        spans_in=spans_in,
        content_hash=content_hash,
    )
    project.db.commit()
    return project, sheet, body_col, ents_col, row_id


def _occurrences(project, sheet, ents_col, row_id, **kwargs):
    return resolve_entity_mention_occurrences(
        project,
        sheet_id=sheet,
        row_id=row_id,
        column_id=ents_col,
        type="person",
        fingerprint="ada lovelace",
        **kwargs,
    )


def test_every_spelling_in_the_group_is_an_occurrence_in_reading_order(tmp_path):
    project, sheet, _body, ents, row_id = _seed(tmp_path)
    try:
        page = _occurrences(project, sheet, ents, row_id)
        assert page["totals"] == {"occurrences": 3}
        # The fingerprint group holds two spellings; both are occurrences, and
        # they come back in the order a reader meets them, not by spelling.
        assert [o["quote"] for o in page["occurrences"]] == [
            "Ada Lovelace",
            "A. Lovelace",
            "Ada Lovelace",
        ]
        assert [o["start"] for o in page["occurrences"]] == sorted(
            o["start"] for o in page["occurrences"]
        )
        # The text column is recovered from the link's OUTPUT cell: the panel
        # only ever knows the entity column.
        assert page["text_column"]["name"] == "body"
    finally:
        project.close()


def test_the_occurrence_count_matches_route_as_number_for_the_same_row(tmp_path):
    """The two levels of one panel must not disagree about one document."""
    project, sheet, _body, ents, row_id = _seed(tmp_path)
    try:
        documents = resolve_entity_mention_documents(
            project,
            sheet_id=sheet,
            column_id=ents,
            type="person",
            fingerprint="ada lovelace",
        )
        listed = next(d for d in documents["documents"] if d["row_id"] == row_id)
        page = _occurrences(project, sheet, ents, row_id)
        assert page["totals"]["occurrences"] == listed["occurrence_count"] == 3
    finally:
        project.close()


def test_a_snippet_is_a_window_of_the_current_text_around_the_mark(tmp_path):
    project, sheet, _body, ents, row_id = _seed(tmp_path)
    try:
        page = _occurrences(project, sheet, ents, row_id, snippet_radius=10)
        first = page["occurrences"][0]
        snippet = first["snippet"]
        # The marked slice of the snippet IS the quote — the offsets are not
        # decoration, the client slices with them.
        assert _utf16_slice(snippet) == "Ada Lovelace"
        assert (
            snippet["text"]
            == BODY[
                BODY.index("Ada Lovelace") - 10 : BODY.index("Ada Lovelace") + 12 + 10
            ]
        )
        assert snippet["truncated_start"] is True
        assert snippet["truncated_end"] is True
        # A radius that swallows the cell says so, rather than claiming an
        # ellipsis it has not earned.
        whole = _occurrences(project, sheet, ents, row_id, snippet_radius=10_000)
        assert whole["occurrences"][0]["snippet"]["text"] == BODY
        assert whole["occurrences"][0]["snippet"]["truncated_start"] is False
        assert whole["occurrences"][0]["snippet"]["truncated_end"] is False
    finally:
        project.close()


def test_snippet_offsets_are_utf16_so_an_astral_char_shifts_the_mark(tmp_path):
    body = "🛰 satellite notes: Ada Lovelace signed off."
    project, sheet, _body, ents, row_id = _seed(tmp_path, body_text=body)
    try:
        page = _occurrences(project, sheet, ents, row_id, snippet_radius=10_000)
        snippet = page["occurrences"][0]["snippet"]
        assert snippet["text"] == body
        # The satellite is ONE code point and TWO UTF-16 code units. A
        # code-point offset here would be off by one and the client would slice
        # "Ada Lovelac".
        assert snippet["mark_start"] == body.index("Ada Lovelace") + 1
        # Sliced the way the CLIENT slices — a JS string indexes UTF-16 code
        # units. A plain Python slice here would index code points and quietly
        # pass a test of the wrong unit.
        assert _utf16_slice(snippet) == "Ada Lovelace"
    finally:
        project.close()


def test_stale_text_yields_the_reason_and_no_snippets(tmp_path):
    # The surface was captured against text the cell no longer holds — what an
    # edit after extraction leaves behind.
    project, sheet, _body, ents, row_id = _seed(
        tmp_path, content_hash=_text_hash("the text this layer was measured against")
    )
    try:
        page = _occurrences(project, sheet, ents, row_id)
        assert page["occurrences"] == []
        assert page["totals"] == {"occurrences": 0}
        # Named, not silent: the panel says the text moved, it does not say the
        # mention is absent.
        assert page["unpositioned"] == {"reason": "content_hash_mismatch", "total": 4}
    finally:
        project.close()


def test_paging_walks_the_occurrences_without_repeating_or_dropping(tmp_path):
    project, sheet, _body, ents, row_id = _seed(tmp_path)
    try:
        first = _occurrences(project, sheet, ents, row_id, limit=2)
        assert first["next_offset"] == 2
        second = _occurrences(
            project, sheet, ents, row_id, limit=2, offset=first["next_offset"]
        )
        assert second["next_offset"] is None
        ids = [o["occurrence_id"] for o in first["occurrences"] + second["occurrences"]]
        assert len(ids) == len(set(ids)) == 3
        assert second["totals"] == first["totals"]
    finally:
        project.close()


def test_a_row_with_no_annotation_layer_is_empty_not_an_error(tmp_path):
    """Route A lists rows from the entity arrays; some of them legitimately
    have no coordinate layer (an older run, a superseded link). That is an
    empty Level 2, not a failure of the request."""
    project, sheet, body_col, ents, _row_id = _seed(tmp_path)
    try:
        project.add_rows(
            sheet,
            [
                {
                    "body": "Ada Lovelace again.",
                    "entities": [
                        {
                            "text": "Ada Lovelace",
                            "type": "person",
                            "fingerprint": "ada lovelace",
                        }
                    ],
                }
            ],
            {"body": body_col, "entities": ents},
        )
        project.db.commit()
        bare = project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY id DESC LIMIT 1", (sheet,)
        ).fetchone()["id"]
        page = _occurrences(project, sheet, ents, bare)
        assert page["occurrences"] == []
        assert page["text_column"] is None
        assert page["unpositioned"] is None
    finally:
        project.close()


def test_an_unfingerprinted_type_matches_its_exact_spelling(tmp_path):
    project, sheet, _body, ents, row_id = _seed(tmp_path)
    try:
        page = resolve_entity_mention_occurrences(
            project,
            sheet_id=sheet,
            row_id=row_id,
            column_id=ents,
            type="ordinal",
            text="item 4",
        )
        assert [o["quote"] for o in page["occurrences"]] == ["item 4"]
        assert page["selector"] == {"kind": "text", "text": "item 4"}
    finally:
        project.close()


@pytest.mark.parametrize(
    "identity",
    [{}, {"fingerprint": "a", "text": "b"}],
    ids=["neither", "both"],
)
def test_the_identity_must_be_exactly_one_of_the_two_modes(tmp_path, identity):
    project, sheet, _body, ents, row_id = _seed(tmp_path)
    try:
        with pytest.raises(EntityMentionDetailError) as caught:
            resolve_entity_mention_occurrences(
                project,
                sheet_id=sheet,
                row_id=row_id,
                column_id=ents,
                type="person",
                **identity,
            )
        assert caught.value.code == "invalid_params"
    finally:
        project.close()


def test_a_hidden_sheet_leaks_nothing(tmp_path):
    project, sheet, _body, ents, row_id = _seed(tmp_path)
    try:
        project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (sheet,))
        project.db.commit()
        with pytest.raises(EntityMentionDetailError):
            _occurrences(project, sheet, ents, row_id)
    finally:
        project.close()
