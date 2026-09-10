"""Route A: which documents ONE normalized mention appears in.

The load-bearing assertion is cross-surface parity — this endpoint's `totals` must equal
the Mentions panel's `row_count`/`mention_count` for the same group. If they can
drift, the two surfaces disagree for exactly the mentions the feature exists
for, and the panel's headline ("N mentions across M documents") becomes a second
opinion rather than a drill-down.
"""

from __future__ import annotations

import pytest

from frisket.engine.store import Project
from frisket.preview.entity_mention_detail import (
    EntityMentionDetailError,
    resolve_entity_mention_documents,
)
from frisket.preview.entity_mentions import resolve_entity_mentions_preview


def _seed(tmp_path):
    project = Project.create(tmp_path / "m.frisket", name="m")
    sheet = project.add_sheet("Docs")
    title = project.add_column(sheet, "title", type="text")
    ents = project.add_column(sheet, "entities", type="json")
    project.set_column_semantic_type(ents, "entity_mentions")

    def person(text, fingerprint):
        return {"text": text, "type": "person", "fingerprint": fingerprint}

    rows = [
        # Two SPELLINGS of one fingerprint group in one row, plus a repeat —
        # so row_count and mention_count genuinely differ.
        {
            "title": "Hearing",
            "entities": [
                person("Ada Lovelace", "ada lovelace"),
                person("A. Lovelace", "ada lovelace"),
                {"text": "$4.2m", "type": "money"},
            ],
        },
        {
            "title": "Deposition",
            "entities": [person("Ada Lovelace", "ada lovelace")],
        },
        {
            "title": "Unrelated",
            "entities": [person("Charles Babbage", "babbage charles")],
        },
    ]
    project.add_rows(sheet, rows, {"title": title, "entities": ents})
    project.db.commit()
    return project, sheet, ents, title


def test_totals_match_the_mentions_panel_for_the_same_group(tmp_path):
    project, sheet, ents, _title = _seed(tmp_path)
    try:
        preview = resolve_entity_mentions_preview(
            project, sheet_id=sheet, column_id=ents, type="person"
        )
        group = next(g for g in preview["items"] if g["label"] == "Ada Lovelace")
        assert group["selector"] == {
            "kind": "fingerprint",
            "fingerprint": "ada lovelace",
        }

        detail = resolve_entity_mention_documents(
            project,
            sheet_id=sheet,
            column_id=ents,
            type="person",
            fingerprint="ada lovelace",
        )
        # cross-surface parity: the same two numbers, from the same stream, over the same rows.
        assert detail["totals"]["documents"] == group["row_count"] == 2
        assert detail["totals"]["mentions"] == group["mention_count"] == 3
        # And they genuinely differ, so a test that conflated them would fail.
        assert group["row_count"] != group["mention_count"]
    finally:
        project.close()


def test_documents_are_ordered_by_occurrence_count_and_carry_a_title(tmp_path):
    project, sheet, ents, _title = _seed(tmp_path)
    try:
        detail = resolve_entity_mention_documents(
            project,
            sheet_id=sheet,
            column_id=ents,
            type="person",
            fingerprint="ada lovelace",
        )
        assert [(d["title"], d["occurrence_count"]) for d in detail["documents"]] == [
            ("Hearing", 2),
            ("Deposition", 1),
        ]
        # Echoed so the reader and the grid cross-link to the SAME filter (D8).
        assert detail["selector"] == {
            "kind": "fingerprint",
            "fingerprint": "ada lovelace",
        }
    finally:
        project.close()


def test_paging_walks_the_documents_without_repeating_or_dropping(tmp_path):
    project, sheet, ents, _title = _seed(tmp_path)
    try:
        first = resolve_entity_mention_documents(
            project,
            sheet_id=sheet,
            column_id=ents,
            type="person",
            fingerprint="ada lovelace",
            limit=1,
        )
        assert first["next_offset"] == 1
        second = resolve_entity_mention_documents(
            project,
            sheet_id=sheet,
            column_id=ents,
            type="person",
            fingerprint="ada lovelace",
            limit=1,
            offset=first["next_offset"],
        )
        assert second["next_offset"] is None
        assert [d["row_id"] for d in first["documents"]] != [
            d["row_id"] for d in second["documents"]
        ]
        # Totals are full-group facts, unaffected by the page requested.
        assert second["totals"] == first["totals"]
    finally:
        project.close()


def test_an_unfingerprinted_type_is_addressed_by_its_exact_spelling(tmp_path):
    project, sheet, ents, _title = _seed(tmp_path)
    try:
        detail = resolve_entity_mention_documents(
            project, sheet_id=sheet, column_id=ents, type="money", text="$4.2m"
        )
        assert detail["totals"] == {"documents": 1, "mentions": 1}

        # A fingerprint selector for one of those types names a group the
        # writer never creates; returning an empty page would look like "this
        # mention is nowhere", which is a different and false answer.
        with pytest.raises(EntityMentionDetailError) as caught:
            resolve_entity_mention_documents(
                project, sheet_id=sheet, column_id=ents, type="money", fingerprint="x"
            )
        assert caught.value.code == "invalid_params"
    finally:
        project.close()


@pytest.mark.parametrize(
    "identity",
    [{}, {"fingerprint": "a", "text": "b"}],
    ids=["neither", "both"],
)
def test_the_identity_must_be_exactly_one_of_the_two_modes(tmp_path, identity):
    project, sheet, ents, _title = _seed(tmp_path)
    try:
        with pytest.raises(EntityMentionDetailError) as caught:
            resolve_entity_mention_documents(
                project, sheet_id=sheet, column_id=ents, type="person", **identity
            )
        assert caught.value.code == "invalid_params"
    finally:
        project.close()
