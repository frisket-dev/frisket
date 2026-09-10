"""HTTP contract for the read-only Mentions preview route.

POST /api/projects/{pid}/entity-mentions/v1/preview is the receipt-free
browsing surface for what `map.ner` already wrote into a column explicitly
marked ``semantic_type='entity_mentions'``. It writes nothing and returns
fingerprint-grouped surface groups with exact full-sheet distinct-row counts,
every raw spelling behind each group, and the ``entity_eq`` selector that
reproduces that same row count as a grid filter.

The load-bearing test in this file is
``test_every_returned_group_row_count_equals_its_own_selector`` — the parity
invariant. It is driven from the endpoint's real
output and measures the expected number by APPLYING the selector, never by
re-deriving it with parallel counting logic.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from helpers import make_client as _client, write_claimed_test_results
from frisket.ops.entities import canonicalize_entities

PREVIEW = "/api/projects/{pid}/entity-mentions/v1/preview"


def _entities(*surfaces: tuple[str, str]) -> list[dict]:
    """Entity items built through the REAL canonicalization path, so stored
    fingerprints are the production contract rather than test fixtures."""
    return canonicalize_entities(
        [
            {"text": text, "type": entity_type, "start": index, "end": index + 1}
            for index, (entity_type, text) in enumerate(surfaces)
        ]
    )


def _seed(client: TestClient) -> tuple[str, int, int, dict[str, int]]:
    pid = client.post("/api/projects", json={"name": "Mentions"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("docs")
    columns = {
        "title": project.add_column(sheet_id, "title"),
        "entities": project.add_column(
            sheet_id, "entities", type="json", semantic_type="entity_mentions"
        ),
        # A json column WITHOUT the marker: OCR/evidence arrays must never be
        # discovered, however entity-shaped their contents look.
        "evidence": project.add_column(sheet_id, "evidence", type="json"),
    }
    rows = project.add_rows(
        sheet_id,
        [
            {
                "title": "one",
                "entities": _entities(
                    ("organization", "ACME Corp."),
                    ("person", "Jon Smith"),
                    ("date", "March 2022"),
                ),
                "evidence": _entities(("organization", "Ghost Industries")),
            },
            {
                "title": "two",
                "entities": _entities(
                    ("organization", "Acme Corporation"),
                    ("person", "Smith, Jon"),
                ),
            },
            {
                "title": "three",
                # the same surface twice in ONE row
                "entities": _entities(
                    ("organization", "ACME Corp."),
                    ("organization", "ACME Corp."),
                    ("date", "March 2022"),
                ),
            },
            {"title": "four", "entities": _entities(("organization", "Acme Inc"))},
            {"title": "five", "entities": _entities(("organization", "Banana Farms"))},
            {"title": "six", "entities": []},
        ],
        columns,
    )
    return pid, sheet_id, columns["entities"], dict(zip("abcdef", rows))


def _preview(client: TestClient, pid: str, **body) -> dict:
    response = client.post(PREVIEW.format(pid=pid), json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _rows_for(client: TestClient, pid: str, sheet_id: int, payload: dict) -> set[int]:
    """The row ids the given ``entity_eq`` payload selects, measured by
    applying it as a real grid filter."""
    response = client.get(
        f"/api/projects/{pid}/sheets/{sheet_id}/data",
        params={
            "filter": json.dumps({"entities": {"entity_eq": payload}}),
            "limit": 1000,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    ids = {row["id"] for row in body["rows"]}
    assert body["total"] == len(ids)
    return ids


def _payload(item: dict) -> dict:
    selector = item["selector"]
    return {"type": item["type"], selector["kind"]: selector[selector["kind"]]}


def _group(body: dict, label: str) -> dict:
    return next(item for item in body["items"] if item["label"] == label)


# ---------------------------------------------------------------------------
# shape, grouping, counts
# ---------------------------------------------------------------------------


def test_preview_shape_grouping_and_no_side_effects(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id, column_id, _rows = _seed(client)
    project = client.app.state.workspace.get(pid)
    ops_before = len(project.history())

    body = _preview(client, pid, sheet_id=sheet_id, column_id=column_id)

    assert body["schema_version"] == "entity-mentions-preview.v2"
    assert body["sheet_id"] == sheet_id
    assert body["column"] == {
        "id": column_id,
        "name": "entities",
        "semantic_type": "entity_mentions",
    }
    # no run: the three run counters are null (zeroes would read as "0 of 0
    # rows extracted"), while sheet_rows is a fact about the SHEET and stays
    # an integer.
    assert body["coverage"] == {
        "target_rows": None,
        "completed_rows": None,
        "failed_rows": None,
        "sheet_rows": project.row_count(sheet_id),
        "scope_kind": None,
    }
    assert body["search"] is None
    assert body["type"] is None
    assert body["limit"] == 100
    assert body["offset"] == 0
    # one entry per type with >= 1 group, in section order
    assert body["type_totals"] == [
        {"type": "organization", "total_groups": 3},
        {"type": "date", "total_groups": 1},
        {"type": "person", "total_groups": 1},
    ]

    acme = _group(body, "ACME Corp.")
    assert acme["type"] == "organization"
    assert acme["selector"] == {"kind": "fingerprint", "fingerprint": "acme corp"}
    assert acme["row_count"] == 3
    assert acme["mention_count"] == 4
    assert acme["surface_count"] == 2
    assert acme["surfaces"] == [
        {"text": "ACME Corp.", "row_count": 2, "mention_count": 3},
        {"text": "Acme Corporation", "row_count": 1, "mention_count": 1},
    ]

    # Smith, Jon / Jon Smith combine; Acme Inc stays apart from Acme Corp.
    smith = _group(body, "Jon Smith")
    assert smith["selector"] == {"kind": "fingerprint", "fingerprint": "jon smith"}
    assert smith["row_count"] == 2
    assert sorted(s["text"] for s in smith["surfaces"]) == ["Jon Smith", "Smith, Jon"]
    inc = _group(body, "Acme Inc")
    assert inc["selector"] == {"kind": "fingerprint", "fingerprint": "acme inc"}
    assert inc["row_count"] == 1

    # numeric-temporal types carry no fingerprint and group by exact text
    march = _group(body, "March 2022")
    assert march["type"] == "date"
    assert march["selector"] == {"kind": "text", "text": "March 2022"}
    assert march["row_count"] == 2

    # the unmarked evidence column's entity-shaped array is never discovered
    assert not any(item["label"] == "Ghost Industries" for item in body["items"])

    # read-only: no receipt, no op, no new column
    assert len(project.history()) == ops_before
    assert [c["name"] for c in project.columns(sheet_id)] == [
        "title",
        "entities",
        "evidence",
    ]


def test_repeated_surface_in_one_row_raises_mentions_not_rows(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id, column_id, rows = _seed(client)
    body = _preview(client, pid, sheet_id=sheet_id, column_id=column_id)
    acme = _group(body, "ACME Corp.")
    surface = next(s for s in acme["surfaces"] if s["text"] == "ACME Corp.")
    # row "three" holds it twice: 2 distinct rows, 3 occurrences.
    assert (surface["row_count"], surface["mention_count"]) == (2, 3)
    assert _rows_for(
        client, pid, sheet_id, {"type": "organization", "text": "ACME Corp."}
    ) == {
        rows["a"],
        rows["c"],
    }


def test_type_sections_and_groups_sort_deterministically(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id, column_id, _rows = _seed(client)
    body = _preview(client, pid, sheet_id=sheet_id, column_id=column_id)
    # organization coverage 3+1+1=5, person 2, date 2 -> person before date on
    # the canonical-name tie-break; groups inside a section by row_count desc
    # then label asc.
    assert [(item["type"], item["label"]) for item in body["items"]] == [
        ("organization", "ACME Corp."),
        ("organization", "Acme Inc"),
        ("organization", "Banana Farms"),
        ("date", "March 2022"),
        ("person", "Jon Smith"),
    ]


def test_unfingerprintable_surfaces_group_by_exact_text_and_stay_selectable(tmp_path):
    """Punctuation-only surfaces receive no fingerprint, so they
    must group by exact text rather than land in one empty-key bucket — and
    the text selector they emit must still filter."""
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Junky"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("docs")
    columns = {
        "entities": project.add_column(
            sheet_id, "entities", type="json", semantic_type="entity_mentions"
        )
    }
    project.add_rows(
        sheet_id,
        [
            {"entities": _entities(("organization", "---"))},
            {"entities": _entities(("organization", "***"))},
        ],
        columns,
    )
    stored = json.loads(
        project.db.execute(
            "SELECT value FROM cells WHERE column_id=?", (columns["entities"],)
        ).fetchone()["value"]
    )
    assert "fingerprint" not in stored[0]

    body = _preview(client, pid, sheet_id=sheet_id, column_id=columns["entities"])
    assert body["total_groups"] == 2
    assert {item["label"] for item in body["items"]} == {"---", "***"}
    for item in body["items"]:
        assert item["selector"]["kind"] == "text"
        assert len(_rows_for(client, pid, sheet_id, _payload(item))) == 1


def test_label_tie_breaks_are_shortest_then_lexical(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Labels"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("docs")
    columns = {
        "entities": project.add_column(
            sheet_id, "entities", type="json", semantic_type="entity_mentions"
        )
    }
    project.add_rows(
        sheet_id,
        [
            {"entities": _entities(("organization", "BETA CORP."))},
            {"entities": _entities(("organization", "beta corp"))},
            {"entities": _entities(("organization", "Beta Corp"))},
        ],
        columns,
    )
    body = _preview(client, pid, sheet_id=sheet_id, column_id=columns["entities"])
    assert len(body["items"]) == 1
    group = body["items"][0]
    assert group["selector"] == {"kind": "fingerprint", "fingerprint": "beta corp"}
    assert group["row_count"] == 3
    assert group["surface_count"] == 3
    # every surface has row_count 1 -> shortest wins ("BETA CORP." is 10
    # chars), then lexical between the two 9-char forms ("B" < "b").
    assert group["label"] == "Beta Corp"
    assert [s["text"] for s in group["surfaces"]] == [
        "BETA CORP.",
        "Beta Corp",
        "beta corp",
    ]


# ---------------------------------------------------------------------------
# THE PARITY INVARIANT
# ---------------------------------------------------------------------------


def test_every_returned_group_row_count_equals_its_own_selector(tmp_path):
    """A group's row_count IS the number of rows its emitted entity_eq
    selector returns. Driven from real endpoint output; the expected
    number comes from applying the filter, not from counting again in Python."""
    client = _client(tmp_path)
    pid, sheet_id, column_id, _rows = _seed(client)
    body = _preview(client, pid, sheet_id=sheet_id, column_id=column_id)
    assert body["total_groups"] == len(body["items"]) == 5

    kinds = set()
    for item in body["items"]:
        kinds.add(item["selector"]["kind"])
        group_rows = _rows_for(client, pid, sheet_id, _payload(item))
        assert len(group_rows) == item["row_count"], item

        # exact-surface selectors have the same parity ...
        union: set[int] = set()
        for surface in item["surfaces"]:
            surface_rows = _rows_for(
                client,
                pid,
                sheet_id,
                {"type": item["type"], "text": surface["text"]},
            )
            assert len(surface_rows) == surface["row_count"], (item, surface)
            union |= surface_rows
        # ... and the group is exactly the union of its spellings
        assert union == group_rows, item
    assert kinds == {"fingerprint", "text"}


def test_type_only_selector_has_the_same_parity(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id, column_id, _rows = _seed(client)
    body = _preview(client, pid, sheet_id=sheet_id, column_id=column_id)

    for entity_type in {item["type"] for item in body["items"]}:
        section = _preview(
            client,
            pid,
            sheet_id=sheet_id,
            column_id=column_id,
            type=entity_type,
            limit=500,
        )
        assert section["type"] == entity_type
        assert section["total_groups"] == len(section["items"])
        type_rows = _rows_for(client, pid, sheet_id, {"type": entity_type})
        union: set[int] = set()
        for item in section["items"]:
            assert item["type"] == entity_type
            group_rows = _rows_for(client, pid, sheet_id, _payload(item))
            assert len(group_rows) == item["row_count"]
            union |= group_rows
        assert union == type_rows, entity_type


def test_every_selector_round_trips_as_an_entity_eq_filter(tmp_path):
    """No selector the endpoint emits may be rejected by the filter parser."""
    client = _client(tmp_path)
    pid, sheet_id, column_id, _rows = _seed(client)
    body = _preview(client, pid, sheet_id=sheet_id, column_id=column_id)
    for item in body["items"]:
        for payload in (
            _payload(item),
            {"type": item["type"]},
            *[
                {"type": item["type"], "text": surface["text"]}
                for surface in item["surfaces"]
            ],
        ):
            response = client.get(
                f"/api/projects/{pid}/sheets/{sheet_id}/data",
                params={"filter": json.dumps({"entities": {"entity_eq": payload}})},
            )
            assert response.status_code == 200, (payload, response.text)


def test_requested_type_is_compared_exactly_like_the_entity_eq_filter(tmp_path):
    """ONE type contract. The preview compares `type` exactly against the
    stored canonical type, which is precisely what `entity_eq` does — the
    endpoint's own output is therefore always a valid request, and the two
    surfaces can never disagree about which rows a type covers.

    `map.ner` canonicalizes before it writes, so every stored type is already
    canonical and a request-side alias pass would buy nothing while making
    these two different contracts.
    """
    client = _client(tmp_path)
    pid, sheet_id, column_id, _rows = _seed(client)
    body = _preview(client, pid, sheet_id=sheet_id, column_id=column_id)

    for entity_type in sorted({item["type"] for item in body["items"]}):
        section = _preview(
            client,
            pid,
            sheet_id=sheet_id,
            column_id=column_id,
            type=entity_type,
            limit=500,
        )
        # Echoed back verbatim, and the groups are exactly this type's groups.
        assert section["type"] == entity_type
        assert section["total_groups"] == len(
            [item for item in body["items"] if item["type"] == entity_type]
        )
        assert section["total_groups"] > 0
        # ... and the filter agrees, on the same value, with no translation.
        assert _rows_for(client, pid, sheet_id, {"type": entity_type}) == {
            row
            for item in section["items"]
            for row in _rows_for(client, pid, sheet_id, _payload(item))
        }

    # A non-canonical alias is NOT translated: "ORG" is not a stored type, so
    # it selects nothing — exactly what entity_eq {"type": "ORG"} returns.
    aliased = _preview(client, pid, sheet_id=sheet_id, column_id=column_id, type="ORG")
    assert aliased["type"] == "ORG"
    assert aliased["total_groups"] == 0
    assert aliased["type_totals"] == []
    assert aliased["items"] == []
    assert _rows_for(client, pid, sheet_id, {"type": "ORG"}) == set()


# ---------------------------------------------------------------------------
# search + paging
# ---------------------------------------------------------------------------


def test_search_matches_a_non_label_surface_and_keeps_whole_group_counts(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id, column_id, _rows = _seed(client)
    body = _preview(
        client, pid, sheet_id=sheet_id, column_id=column_id, search="corporation"
    )
    assert body["search"] == "corporation"
    assert body["total_groups"] == 1
    # types whose every group the search eliminated get no type_totals entry
    assert body["type_totals"] == [{"type": "organization", "total_groups": 1}]
    group = body["items"][0]
    # matched through "Acme Corporation", which is NOT the label
    assert group["label"] == "ACME Corp."
    # full-sheet counts and ALL surfaces survive the search
    assert group["row_count"] == 3
    assert group["mention_count"] == 4
    assert [s["text"] for s in group["surfaces"]] == [
        "ACME Corp.",
        "Acme Corporation",
    ]
    assert len(_rows_for(client, pid, sheet_id, _payload(group))) == 3


def test_search_is_case_insensitive_and_trimmed(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id, column_id, _rows = _seed(client)
    body = _preview(
        client, pid, sheet_id=sheet_id, column_id=column_id, search="  SMITH, jon  "
    )
    assert body["search"] == "SMITH, jon"
    assert [item["label"] for item in body["items"]] == ["Jon Smith"]
    assert (
        _preview(client, pid, sheet_id=sheet_id, column_id=column_id, search="   ")[
            "search"
        ]
        is None
    )


def _seed_sections(client: TestClient) -> tuple[str, int, int]:
    """A type-heavy sheet: 5 organization groups, 3 person, 2 date, one row
    each, so section order is organization -> person -> date and groups inside
    a section order by label."""
    pid = client.post("/api/projects", json={"name": "Sections"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("docs")
    columns = {
        "entities": project.add_column(
            sheet_id, "entities", type="json", semantic_type="entity_mentions"
        )
    }
    project.add_rows(
        sheet_id,
        [
            *[
                {"entities": _entities(("organization", f"Org {index}"))}
                for index in range(5)
            ],
            *[
                {"entities": _entities(("person", f"Person {index}"))}
                for index in range(3)
            ],
            *[
                {"entities": _entities(("date", f"March 202{index}"))}
                for index in range(2)
            ],
        ],
        columns,
    )
    return pid, sheet_id, columns["entities"]


def test_untyped_limit_pages_within_each_type_section(tmp_path):
    """limit=N returns up to N groups of EACH type, never N total: the heavy
    organization section cannot push person and date off the first page."""
    client = _client(tmp_path)
    pid, sheet_id, column_id = _seed_sections(client)

    body = _preview(client, pid, sheet_id=sheet_id, column_id=column_id, limit=2)
    assert body["total_groups"] == 10
    assert body["type_totals"] == [
        {"type": "organization", "total_groups": 5},
        {"type": "person", "total_groups": 3},
        {"type": "date", "total_groups": 2},
    ]
    assert [(item["type"], item["label"]) for item in body["items"]] == [
        ("organization", "Org 0"),
        ("organization", "Org 1"),
        ("person", "Person 0"),
        ("person", "Person 1"),
        ("date", "March 2020"),
        ("date", "March 2021"),
    ]


def test_untyped_offset_advances_within_each_type_section(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id, column_id = _seed_sections(client)

    body = _preview(
        client, pid, sheet_id=sheet_id, column_id=column_id, limit=2, offset=2
    )
    # a section the offset exhausts (date has 2 groups) contributes nothing;
    # the totals are page-independent facts and do not move.
    assert [(item["type"], item["label"]) for item in body["items"]] == [
        ("organization", "Org 2"),
        ("organization", "Org 3"),
        ("person", "Person 2"),
    ]
    assert body["total_groups"] == 10
    assert body["type_totals"] == [
        {"type": "organization", "total_groups": 5},
        {"type": "person", "total_groups": 3},
        {"type": "date", "total_groups": 2},
    ]


def test_typed_request_pages_one_section_and_reports_its_total(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id, column_id = _seed_sections(client)

    first = _preview(
        client, pid, sheet_id=sheet_id, column_id=column_id, type="person", limit=2
    )
    assert first["total_groups"] == 3
    assert first["type_totals"] == [{"type": "person", "total_groups": 3}]
    assert [item["label"] for item in first["items"]] == ["Person 0", "Person 1"]

    rest = _preview(
        client,
        pid,
        sheet_id=sheet_id,
        column_id=column_id,
        type="person",
        limit=2,
        offset=2,
    )
    assert rest["total_groups"] == 3
    assert rest["type_totals"] == [{"type": "person", "total_groups": 3}]
    assert [item["label"] for item in rest["items"]] == ["Person 2"]


def test_paging_and_total_groups_stay_honest_beyond_500_groups(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Many"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("docs")
    columns = {
        "entities": project.add_column(
            sheet_id, "entities", type="json", semantic_type="entity_mentions"
        )
    }
    project.add_rows(
        sheet_id,
        [
            {"entities": _entities(("person", f"Casey Number{index:04d}"))}
            for index in range(600)
        ],
        columns,
    )
    column_id = columns["entities"]

    first = _preview(
        client, pid, sheet_id=sheet_id, column_id=column_id, limit=500, offset=0
    )
    assert first["total_groups"] == 600
    assert first["type_totals"] == [{"type": "person", "total_groups": 600}]
    assert first["limit"] == 500
    assert len(first["items"]) == 500

    second = _preview(
        client, pid, sheet_id=sheet_id, column_id=column_id, limit=500, offset=500
    )
    assert second["total_groups"] == 600
    assert len(second["items"]) == 100
    labels = [item["label"] for item in first["items"] + second["items"]]
    assert len(set(labels)) == 600

    # a spelling on the LAST page is still findable by server-side search
    late = _preview(
        client, pid, sheet_id=sheet_id, column_id=column_id, search="number0599"
    )
    assert late["total_groups"] == 1
    assert late["items"][0]["label"] == "Casey Number0599"
    assert len(_rows_for(client, pid, sheet_id, _payload(late["items"][0]))) == 1

    # over-asks clamp to the ceiling rather than erroring
    assert (
        _preview(client, pid, sheet_id=sheet_id, column_id=column_id, limit=9999)[
            "limit"
        ]
        == 500
    )


# ---------------------------------------------------------------------------
# malformed / non-entity data
# ---------------------------------------------------------------------------


def test_malformed_and_non_entity_cells_contribute_nothing_and_never_500(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Junk"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("docs")
    columns = {
        "entities": project.add_column(
            sheet_id, "entities", type="json", semantic_type="entity_mentions"
        )
    }
    rows = project.add_rows(
        sheet_id,
        [
            {"entities": [1, 2.5, "three", True, None]},  # scalar array
            {"entities": "a bare json string"},
            {"entities": None},  # no cell at all
            {"entities": {"not": "an array"}},
            {"entities": []},
            {"entities": [{"foo": "bar"}, {"type": "person"}, {"text": "no type"}]},
            {"entities": [{"type": "", "text": "empty type"}]},
            {"entities": [{"type": "person", "text": ""}]},
            {"entities": [{"type": 5, "text": 7}]},
            {"entities": _entities(("person", "Real Person"))},
        ],
        columns,
    )
    # invalid JSON text can only be written past json.dumps
    project.db.execute(
        "UPDATE cells SET value=? WHERE row_id=? AND column_id=?",
        ("{not json at all", rows[4], columns["entities"]),
    )
    project.db.commit()

    body = _preview(client, pid, sheet_id=sheet_id, column_id=columns["entities"])
    assert body["total_groups"] == 1
    assert body["items"][0]["label"] == "Real Person"
    assert body["items"][0]["row_count"] == 1
    assert _rows_for(client, pid, sheet_id, {"type": "person"}) == {rows[9]}
    assert (
        _rows_for(client, pid, sheet_id, {"type": "person", "text": "no type"}) == set()
    )


# ---------------------------------------------------------------------------
# eligibility + coverage
# ---------------------------------------------------------------------------


def test_empty_marked_column_is_eligible_and_returns_the_zero_group_state(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Empty"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("docs")
    column_id = project.add_column(
        sheet_id, "entities", type="json", semantic_type="entity_mentions"
    )
    project.add_rows(sheet_id, [{}, {}], {"entities": column_id})

    body = _preview(client, pid, sheet_id=sheet_id, column_id=column_id)
    assert body["total_groups"] == 0
    assert body["type_totals"] == []
    assert body["items"] == []
    assert body["column"]["semantic_type"] == "entity_mentions"


@pytest.mark.parametrize("column_name", ["title", "evidence"])
def test_unmarked_columns_are_rejected_with_a_typed_error(tmp_path, column_name):
    client = _client(tmp_path)
    pid, sheet_id, _entity_column, _rows = _seed(client)
    project = client.app.state.workspace.get(pid)
    column_id = next(
        c["id"] for c in project.columns(sheet_id) if c["name"] == column_name
    )
    response = client.post(
        PREVIEW.format(pid=pid), json={"sheet_id": sheet_id, "column_id": column_id}
    )
    assert response.status_code == 400
    body = response.json()
    assert "detail" not in body
    assert body["code"] == "column_not_entity_mentions"
    assert body["field"] == "column_id"
    assert body["details"]["semantic_type"] is None


def test_column_from_another_sheet_is_rejected(tmp_path):
    client = _client(tmp_path)
    pid, _sheet_id, column_id, _rows = _seed(client)
    project = client.app.state.workspace.get(pid)
    other_sheet = project.add_sheet("other")
    response = client.post(
        PREVIEW.format(pid=pid),
        json={"sheet_id": other_sheet, "column_id": column_id},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_input_ref"
    assert response.json()["field"] == "column_id"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"sheet_id": 0, "column_id": 1}, "sheet_id"),
        ({"sheet_id": 1, "column_id": True}, "column_id"),
        ({"sheet_id": 1, "column_id": 1, "limit": "lots"}, "limit"),
        ({"sheet_id": 1, "column_id": 1, "limit": 0}, "limit"),
        ({"sheet_id": 1, "column_id": 1, "offset": -1}, "offset"),
        ({"sheet_id": 1, "column_id": 1, "search": 5}, "search"),
        ({"sheet_id": 1, "column_id": 1, "type": []}, "type"),
    ],
)
def test_invalid_request_params_are_bare_v1_errors(tmp_path, body, field):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Bad"}).json()["id"]
    response = client.post(PREVIEW.format(pid=pid), json=body)
    assert response.status_code == 400, response.text
    payload = response.json()
    assert "detail" not in payload
    assert payload["field"] == field
    assert payload["code"] in {"invalid_params", "invalid_input_ref"}


def test_coverage_reports_the_columns_current_run_honestly(tmp_path):
    from frisket.engine.store.runs import RunResultStore

    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Coverage"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("docs")
    source = project.add_column(sheet_id, "title")
    column_id = project.add_column(
        sheet_id,
        "entities",
        type="json",
        ai_generated=True,
        semantic_type="entity_mentions",
    )
    rows = project.add_rows(
        sheet_id, [{"title": "a"}, {"title": "b"}, {"title": "c"}], {"title": source}
    )
    store = RunResultStore(project)
    op = project.append_op("map", {"action_kind": "map.ner"})
    run = store.start_run(
        op,
        sheet_id,
        "map.ner",
        total_rows=len(rows),
        params={"row_ids": rows},
    )
    write_claimed_test_results(
        project,
        run,
        [
            {
                "row_id": rows[0],
                "column_id": column_id,
                "value": _entities(("person", "Jon Smith")),
            },
            {
                "row_id": rows[1],
                "column_id": column_id,
                "value": _entities(("person", "Smith, Jon")),
            },
            {
                "row_id": rows[2],
                "column_id": column_id,
                "error": "boom",
                "outcome": "model_error",
            },
        ],
    )
    store.finish_run(run)
    store.point_column_at_run(op, column_id, run)

    body = _preview(client, pid, sheet_id=sheet_id, column_id=column_id)
    # verbatim run counters, exactly as run/provenance payloads report them:
    # completed_rows counts every row the run PROCESSED, failed_rows the
    # subset of those that failed.
    assert body["coverage"] == {
        "target_rows": 3,
        "completed_rows": 3,
        "failed_rows": 1,
        "sheet_rows": 3,
        "scope_kind": "exact_membership",
    }
    run_row = project.db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
    assert body["coverage"] == {
        "target_rows": run_row["total_rows"],
        "completed_rows": run_row["completed_rows"],
        "failed_rows": run_row["failed_rows"],
        "sheet_rows": project.row_count(sheet_id),
        "scope_kind": "exact_membership",
    }
    # run-backed live values are walked by the same guarded json_each, and the
    # failed row simply contributes nothing.
    assert len(body["items"]) == 1
    group = body["items"][0]
    assert group["row_count"] == 2
    assert _rows_for(client, pid, sheet_id, _payload(group)) == {rows[0], rows[1]}

    successor_op = project.append_op("map", {"action_kind": "map.ner"})
    successor = store.start_run(
        successor_op,
        sheet_id,
        "map.ner",
        total_rows=len(rows),
        params={"row_ids": rows},
    )
    write_claimed_test_results(
        project,
        successor,
        [
            {
                "row_id": row_id,
                "column_id": column_id,
                "value": _entities(("person", f"Person {index}")),
            }
            for index, row_id in enumerate(rows)
        ],
    )
    store.finish_run(successor)

    assert project.get_column(column_id)["current_run_id"] == run
    successor_body = _preview(client, pid, sheet_id=sheet_id, column_id=column_id)
    assert successor_body["coverage"] == {
        "target_rows": 3,
        "completed_rows": 3,
        "failed_rows": 0,
        "sheet_rows": 3,
        "scope_kind": "exact_membership",
    }


def test_coverage_discloses_rows_added_after_the_extraction(tmp_path):
    """Rows added after the run leave the run counters untouched, so the three
    of them together still say "2 of 2 rows extracted" over a sheet that is now
    4 rows. `map.ner` refuses `run.backfill`, so that gap is PERMANENT until a
    whole-sheet re-run: the payload has to disclose it, and `sheet_rows` is
    what makes it visible without changing what the run counters mean."""
    from frisket.engine.store.runs import RunResultStore

    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Stale"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("docs")
    source = project.add_column(sheet_id, "title")
    column_id = project.add_column(
        sheet_id,
        "entities",
        type="json",
        ai_generated=True,
        semantic_type="entity_mentions",
    )
    rows = project.add_rows(
        sheet_id, [{"title": "a"}, {"title": "b"}], {"title": source}
    )
    store = RunResultStore(project)
    op = project.append_op("map", {"action_kind": "map.ner"})
    run = store.start_run(op, sheet_id, "map.ner", total_rows=len(rows))
    write_claimed_test_results(
        project,
        run,
        [
            {
                "row_id": row_id,
                "column_id": column_id,
                "value": _entities(("person", "Jon Smith")),
            }
            for row_id in rows
        ],
    )
    store.finish_run(run)
    store.point_column_at_run(op, column_id, run)

    covered = _preview(client, pid, sheet_id=sheet_id, column_id=column_id)
    assert covered["coverage"] == {
        "target_rows": 2,
        "completed_rows": 2,
        "failed_rows": 0,
        "sheet_rows": 2,
        "scope_kind": "all_rows",
    }

    project.add_rows(sheet_id, [{"title": "c"}, {"title": "d"}], {"title": source})

    stale = _preview(client, pid, sheet_id=sheet_id, column_id=column_id)
    # the run counters are historical facts and do NOT move ...
    assert stale["coverage"]["target_rows"] == 2
    assert stale["coverage"]["completed_rows"] == 2
    assert stale["coverage"]["failed_rows"] == 0
    # ... so the disclosure is the live sheet count sitting beside them.
    assert stale["coverage"]["sheet_rows"] == 4
    assert stale["coverage"]["target_rows"] < stale["coverage"]["sheet_rows"]
    # the new rows really do have no extraction: the groups are unchanged.
    assert [item["row_count"] for item in stale["items"]] == [
        item["row_count"] for item in covered["items"]
    ]
