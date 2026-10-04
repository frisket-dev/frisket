import json
from types import SimpleNamespace

import pytest

from frisket.engine.store import Project
from frisket.querysets import resolve_sheet_filter_rows
import frisket.querysets as querysets
import frisket.server.services.document_browse as browse
from frisket.server.services.document_browse import document_browse
from frisket.server.services.document_browse import _descriptor_sql


@pytest.fixture
def docs(tmp_path):
    project = Project.create(tmp_path / "docs.frisket", name="docs")
    sheet = project.add_sheet("Documents")
    source = project.add_column(sheet, "file", type="file")
    title = project.add_column(sheet, "title")
    rows = project.add_rows(
        sheet,
        [
            {
                "file": {
                    "blob": "abc",
                    "filename": f"file{i}.pdf",
                    "mime": "application/pdf",
                },
                "title": value,
            }
            for i, value in enumerate([None, "b", "A", "a", "z", None, "B"])
        ],
        {"file": source, "title": title},
    )
    yield project, sheet, source, title, rows
    project.close()


def page(docs, **kwargs):
    project, sheet, source, title, _ = docs
    return document_browse(
        project, sheet, source_column_id=source, title_column_id=title, **kwargs
    )


@pytest.mark.parametrize(
    "sort",
    [
        None,
        [{"column": "title", "dir": "asc"}],
        [{"column": "title", "dir": "desc"}],
        [{"column": "title", "dir": "asc"}, {"column": "file", "dir": "desc"}],
    ],
)
def test_keysets_match_grid_both_directions(docs, sort):
    project, sheet, *_ = docs
    expected = resolve_sheet_filter_rows(
        project, sheet, sort=json.dumps(sort) if sort else None
    ).row_ids
    first = page(docs, sort=sort, limit=2)
    current = first
    gathered = []
    while True:
        gathered.extend(item["row_id"] for item in current["items"])
        if not current["next_cursor"]:
            break
        following = page(docs, sort=sort, limit=2, cursor=current["next_cursor"])
        prior = page(docs, sort=sort, limit=2, cursor=following["previous_cursor"])
        assert prior["items"] == current["items"]
        current = following
    assert gathered == expected
    assert first["previous_cursor"] is None


def test_anchor_scope_query_and_capped_title(docs):
    project, _, _, title, rows = docs
    project.apply_edits(
        [{"row_id": rows[4], "column_id": title, "value": "x" * 100000 + "needle"}]
    )
    found = page(docs, q="needle")
    assert len(found["items"]) == 1
    assert found["items"][0]["title"] == "x" * 256
    assert found["items"][0]["title_truncated"]
    assert len(json.dumps(found)) < 2000
    anchored = page(docs, anchor_row_id=rows[3], limit=2)
    assert anchored["items"][0]["ordinal"] == 4
    assert anchored["items"][0]["row_id"] == rows[3]
    assert anchored["previous_cursor"]
    with pytest.raises(ValueError, match="scope"):
        page(docs, cursor=anchored["next_cursor"], q="other")
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (rows[4],))
    project.db.commit()
    with pytest.raises(ValueError, match="anchor"):
        page(docs, cursor=anchored["next_cursor"])


def test_blank_title_uses_media_filename_and_search(docs):
    found = page(docs, q="file0.pdf")
    assert found["items"][0]["title"] == "file0.pdf"
    assert found["items"][0]["source_kind"] == "pdf"


def test_url_label_and_text_media(docs):
    project, _, source, title, rows = docs
    project.apply_edits(
        [
            {
                "row_id": rows[0],
                "column_id": source,
                "value": "https://example.org/reports/note.txt?download=yes",
            },
            {"row_id": rows[0], "column_id": title, "value": " "},
        ]
    )
    item = page(docs, q="note.txt")["items"][0]
    assert item["title"] == "note.txt"
    assert item["source_label"] == "note.txt"
    assert item["source_kind"] == "text"


def test_title_search_matches_unicode_case_like_browser(docs):
    project, _, _, title, rows = docs
    project.apply_edits([{"row_id": rows[1], "column_id": title, "value": "École"}])
    assert [item["row_id"] for item in page(docs, q="école")["items"]] == [rows[1]]


def test_typed_invalid_title_remains_visible(docs):
    project, sheet, source, _, rows = docs
    title = project.add_column(sheet, "numeric title", type="integer")
    project.apply_edits(
        [{"row_id": rows[1], "column_id": title, "value": "not numeric"}]
    )
    result = document_browse(
        project, sheet, source_column_id=source, title_column_id=title, q="not numeric"
    )
    assert result["items"][0]["title"] == "not numeric"


def test_row_fallback_search_uses_pre_search_rank(docs):
    project, _, source, title, rows = docs
    project.apply_edits(
        [
            {"row_id": row, "column_id": col, "value": None}
            for row in rows
            for col in (source, title)
        ]
    )
    result = page(docs, q="Row", limit=2)
    assert [item["title"] for item in result["items"]] == ["Row 1", "Row 2"]
    following = page(docs, q="Row", limit=2, cursor=result["next_cursor"])
    assert [item["title"] for item in following["items"]] == ["Row 3", "Row 4"]
    assert not following["items"][0]["source_present"]
    found = page(docs, q="Row 5")
    assert found["items"][0]["title"] == "Row 5"
    assert found["items"][0]["ordinal"] == 5
    assert found["previous_cursor"] is None
    assert found["next_cursor"] is None


def test_filtered_sorted_search_preserves_cursor_order(docs):
    filter_ = {"title": {"contains": "b"}}
    sort = [{"column": "title", "dir": "desc"}]
    first = page(docs, filter=filter_, sort=sort, q="b", limit=1)
    following = page(
        docs,
        filter=filter_,
        sort=sort,
        q="b",
        limit=1,
        cursor=first["next_cursor"],
    )
    previous = page(
        docs,
        filter=filter_,
        sort=sort,
        q="b",
        limit=1,
        cursor=following["previous_cursor"],
    )
    assert [item["title"] for item in first["items"] + following["items"]] == [
        "b",
        "B",
    ]
    assert previous["items"] == first["items"]


def test_annotated_title_query_never_reads_source_value(docs):
    project, _, _, title, rows = docs
    # The title-only projection must not reference source validity/value at all.
    # Title presentation intentionally preserves typed-invalid values, so any
    # validity read here would come from the unwanted source lookup.
    sql, params = _descriptor_sql(
        {"id": 999, "type": "text"}, {"id": title}, title_only=True
    )

    assert isinstance(params, dict)
    assert 999 not in params.values()
    assert (
        project.db.execute(
            f"SELECT display_title FROM ({sql})", {**params, "row_id": rows[1]}
        ).fetchone()[0]
        == "b"
    )


def test_deep_default_page_seeks_without_counting_or_body_transfer(docs):
    project, sheet, source, title, _ = docs
    rows = project.add_rows(
        sheet,
        [{"title": f"document {i}", "file": None} for i in range(10000)],
        {"file": source, "title": title},
    )
    near_end = page(docs, anchor_row_id=rows[-30], limit=10)
    statements = []
    instructions = 0

    def progress():
        nonlocal instructions
        instructions += 100
        return instructions > 15000

    project.db.set_progress_handler(progress, 100)
    project.db.set_trace_callback(statements.append)
    try:
        result = page(docs, cursor=near_end["next_cursor"], limit=10)
    finally:
        project.db.set_progress_handler(None, 0)
        project.db.set_trace_callback(None)
    assert [item["row_id"] for item in result["items"]] == rows[-20:-10]
    assert not any(
        "COUNT(" in statement.upper() or "OFFSET" in statement.upper()
        for statement in statements
    )
    assert instructions < 15000


def test_filtered_sorted_anchor_reuses_joined_scope_plan(docs):
    project, sheet, source, title, _ = docs
    group = project.add_column(sheet, "group")
    rows = project.add_rows(
        sheet,
        [
            {
                "file": {
                    "blob": "abc",
                    "filename": f"file-{index}.pdf",
                    "mime": "application/pdf",
                },
                "title": f"Document {index}",
                "group": "keep" if index % 50 == 0 else "drop",
            }
            for index in range(5000)
        ],
        {"file": source, "title": title, "group": group},
    )
    instructions = 0

    def progress():
        nonlocal instructions
        instructions += 100
        return 0

    project.db.set_progress_handler(progress, 100)
    try:
        result = page(
            docs,
            filter={"group": {"eq": "keep"}},
            sort=[{"column": "title", "dir": "desc"}],
            anchor_row_id=rows[2500],
            limit=2,
        )
    finally:
        project.db.set_progress_handler(None, 0)
    assert result["items"][0]["row_id"] == rows[2500]
    assert all(item["title"].startswith("Document ") for item in result["items"])
    # The typed relation resolves three authority branches and materializes
    # only the two referenced columns for each bounded SQL statement.
    assert instructions < 6_000_000


def test_page_snapshot_survives_concurrent_edit(docs, monkeypatch):
    project, _, _, title, rows = docs
    original = browse._descriptor_sql
    writer = Project(project.path)

    def descriptor_after_edit(*args, **kwargs):
        writer.apply_edits(
            [{"row_id": rows[1], "column_id": title, "value": "replacement"}]
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(browse, "_descriptor_sql", descriptor_after_edit)
    try:
        assert page(docs)["items"][1]["title"] == "b"
    finally:
        writer.close()


def test_runtime_filter_retains_full_project_read_api_and_outer_transaction(
    docs, monkeypatch
):
    project, _, _, _, rows = docs
    calls = []

    def handler(payload):
        runtime_project = payload["project"]
        calls.append(runtime_project.get_meta("name"))
        assert runtime_project is project
        return {
            "schemaVersion": "frisket.runtime_operator_plan.v1",
            "rowIds": payload["candidateRowIds"][1:3],
        }

    binding = SimpleNamespace(
        handler=handler, handler_api="trusted", plugin="demo", handler_key="demo:match"
    )
    monkeypatch.setattr(
        querysets, "_runtime_operator_bindings", lambda project: {"demo.match": binding}
    )
    project.db.execute("BEGIN")
    try:
        result = page(docs, filter={"title": {"demo.match": True}})
        assert [item["row_id"] for item in result["items"]] == rows[1:3]
        assert len(calls) == 1
        assert project.db.in_transaction
    finally:
        project.db.rollback()
