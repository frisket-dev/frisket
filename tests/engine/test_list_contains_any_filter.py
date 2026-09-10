"""Element containment filtering for JSON list-valued columns.

The public filter contract is one OR-combined ``list_contains_any`` operator.
Generic JSON columns accept exact typed scalar selectors; marked entity mention
columns accept exact ``type`` + ``text`` selectors. Cells that are not valid
arrays of the supported item shape contribute no matches.
"""

from __future__ import annotations

import json

import pytest

from frisket.engine.store import Project
from frisket.querysets import SheetRowSetError, resolve_sheet_filter_rows


@pytest.fixture
def project(tmp_path):
    value = Project.create(tmp_path / "list-filter.frisket", name="lists")
    yield value
    value.close()


def _filter(column: str, *selectors: dict) -> str:
    return json.dumps({column: {"list_contains_any": list(selectors)}})


def _rows(project: Project, sheet: int, column: str, *selectors: dict) -> set[int]:
    rowset = resolve_sheet_filter_rows(
        project,
        sheet,
        filter_=_filter(column, *selectors),
    )
    assert rowset.total == len(rowset.row_ids)
    return set(rowset.row_ids)


def test_scalar_list_contains_any_is_typed_exact_and_or_combined(project):
    sheet = project.add_sheet("calls")
    tags = project.add_column(sheet, "tags", type="json")
    nypd, number, boolean, zero, false, numeric_text, other = project.add_rows(
        sheet,
        [
            {"tags": ["NYPD", "NYPD"]},
            {"tags": [1]},
            {"tags": [True]},
            {"tags": [0]},
            {"tags": [False]},
            {"tags": ["1"]},
            {"tags": ["FDNY"]},
        ],
        {"tags": tags},
    )

    def scalar(value):
        return {"kind": "scalar", "value": value}

    assert _rows(project, sheet, "tags", scalar("NYPD")) == {nypd}
    assert _rows(project, sheet, "tags", scalar(1)) == {number}
    assert _rows(project, sheet, "tags", scalar(True)) == {boolean}
    assert _rows(project, sheet, "tags", scalar(0)) == {zero}
    assert _rows(project, sheet, "tags", scalar(False)) == {false}
    assert _rows(project, sheet, "tags", scalar("1")) == {numeric_text}
    assert _rows(
        project,
        sheet,
        "tags",
        scalar("NYPD"),
        scalar(True),
    ) == {nypd, boolean}
    assert _rows(project, sheet, "tags", scalar(True), scalar(1)) == {
        boolean,
        number,
    }
    assert _rows(project, sheet, "tags", scalar(False), scalar(0)) == {
        false,
        zero,
    }
    assert other not in _rows(project, sheet, "tags", scalar("NYPD"))


def test_entity_list_contains_any_matches_exact_type_and_text(project):
    sheet = project.add_sheet("documents")
    entities = project.add_column(
        sheet,
        "entities",
        type="json",
        semantic_type="entity_mentions",
    )
    org, agency, person, differently_cased = project.add_rows(
        sheet,
        [
            {"entities": [{"type": "organization", "text": "NYPD"}]},
            {"entities": [{"type": "agency", "text": "NYPD"}]},
            {"entities": [{"type": "person", "text": "Jane Doe"}]},
            {"entities": [{"type": "organization", "text": "nypd"}]},
        ],
        {"entities": entities},
    )

    def entity(type_, text):
        return {"kind": "entity", "type": type_, "text": text}

    assert _rows(project, sheet, "entities", entity("organization", "NYPD")) == {org}
    assert _rows(project, sheet, "entities", entity("agency", "NYPD")) == {agency}
    assert _rows(
        project,
        sheet,
        "entities",
        entity("organization", "NYPD"),
        entity("person", "Jane Doe"),
    ) == {org, person}
    assert differently_cased not in _rows(
        project, sheet, "entities", entity("organization", "NYPD")
    )


def test_malformed_non_array_and_unknown_object_cells_fail_closed(project):
    sheet = project.add_sheet("mixed")
    tags = project.add_column(sheet, "tags", type="json")
    good, non_array, unknown_object, nested, invalid = project.add_rows(
        sheet,
        [
            {"tags": ["NYPD"]},
            {"tags": "NYPD"},
            {"tags": [{"text": "NYPD", "type": "organization"}]},
            {"tags": [["NYPD"]]},
            {"tags": []},
        ],
        {"tags": tags},
    )
    project.db.execute(
        "UPDATE cells SET value=? WHERE row_id=? AND column_id=?",
        ("{not json", invalid, tags),
    )
    project.db.commit()

    selected = _rows(
        project,
        sheet,
        "tags",
        {"kind": "scalar", "value": "NYPD"},
    )
    assert selected == {good}
    assert {non_array, unknown_object, nested, invalid}.isdisjoint(selected)


def test_list_contains_any_requires_eligible_column_and_closed_selectors(project):
    sheet = project.add_sheet("eligibility")
    text = project.add_column(sheet, "text", type="text")
    plain = project.add_column(sheet, "plain", type="json")
    entities = project.add_column(
        sheet,
        "entities",
        type="json",
        semantic_type="entity_mentions",
    )
    project.add_rows(
        sheet,
        [{"text": "NYPD", "plain": ["NYPD"], "entities": []}],
        {"text": text, "plain": plain, "entities": entities},
    )
    scalar = {"kind": "scalar", "value": "NYPD"}
    entity = {"kind": "entity", "type": "organization", "text": "NYPD"}

    invalid = [
        ("text", [scalar]),
        ("plain", [entity]),
        ("entities", [scalar]),
        ("plain", []),
        ("plain", "NYPD"),
        ("plain", [{"kind": "scalar", "value": None}]),
        ("plain", [{"kind": "scalar", "value": ["NYPD"]}]),
        ("plain", [{"kind": "scalar", "value": 9007199254740992}]),
        ("plain", [{"kind": "scalar", "value": "NYPD", "extra": True}]),
        ("plain", [{"kind": "object", "value": "NYPD"}]),
        ("entities", [{"kind": "entity", "type": "", "text": "NYPD"}]),
        ("entities", [{"kind": "entity", "type": "organization", "text": ""}]),
        (
            "entities",
            [
                {
                    "kind": "entity",
                    "type": "organization",
                    "text": "NYPD",
                    "fingerprint": "nypd",
                }
            ],
        ),
    ]
    for column, payload in invalid:
        with pytest.raises(SheetRowSetError, match="list_contains_any"):
            resolve_sheet_filter_rows(
                project,
                sheet,
                filter_=json.dumps({column: {"list_contains_any": payload}}),
            )


def test_list_contains_any_accepts_at_most_one_hundred_selectors(project):
    sheet = project.add_sheet("limits")
    tags = project.add_column(sheet, "tags", type="json")
    project.add_rows(sheet, [{"tags": ["v0"]}], {"tags": tags})
    selectors = [{"kind": "scalar", "value": f"v{index}"} for index in range(101)]

    with pytest.raises(SheetRowSetError, match="at most 100"):
        resolve_sheet_filter_rows(
            project,
            sheet,
            filter_=_filter("tags", *selectors),
        )


def test_list_contains_any_rejects_arbitrarily_large_integer_without_overflow(project):
    sheet = project.add_sheet("huge integers")
    tags = project.add_column(sheet, "tags", type="json")
    project.add_rows(sheet, [{"tags": ["NYPD"]}], {"tags": tags})

    with pytest.raises(SheetRowSetError, match="JavaScript-safe"):
        resolve_sheet_filter_rows(
            project,
            sheet,
            filter_=_filter("tags", {"kind": "scalar", "value": 10**400}),
        )


def test_list_contains_any_does_not_round_safe_integer_into_adjacent_value(project):
    sheet = project.add_sheet("integer boundary")
    tags = project.add_column(sheet, "tags", type="json")
    safe, adjacent = project.add_rows(
        sheet,
        [{"tags": [2**53 - 1]}, {"tags": [2**53]}],
        {"tags": tags},
    )

    selected = _rows(
        project,
        sheet,
        "tags",
        {"kind": "scalar", "value": 2**53 - 1},
    )
    assert selected == {safe}
    assert adjacent not in selected
