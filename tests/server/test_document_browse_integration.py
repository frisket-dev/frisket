from __future__ import annotations

import json

from fastapi.testclient import TestClient

from frisket.engine.store.evidence import (
    _text_hash,
    record_evidence_link,
    record_source_artifact,
    record_source_span,
    record_text_surface,
)
from helpers import make_client


def _import_documents(
    client: TestClient,
    project_id: str,
    *,
    count: int,
    needle: int | None = None,
    filename: str = "documents.csv",
) -> tuple[int, dict[str, int]]:
    rows = ["title,body,group,rank,alongside"]
    for index in range(count):
        title = f"Needle {index:03d}" if index == needle else f"Document {index:03d}"
        rows.append(
            f"{title},{'B' * 4096},{'keep' if index % 2 == 0 else 'drop'},"
            f"{index},PRIVATE-{index:03d}"
        )
    response = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": (filename, "\n".join(rows), "text/csv")},
    )
    assert response.status_code == 200, response.text
    sheet_id = int(response.json()["sheet_id"])
    project = client.app.state.workspace.get(project_id)
    columns = {row["name"]: int(row["id"]) for row in project.columns(sheet_id)}
    # Document View intentionally accepts text only when it has a real annotation
    # layer. One current annotation makes this imported read-only body column a
    # supported document source; the large source values themselves remain base
    # import cells and must never be returned by the descriptor endpoint.
    first_row = project.visible_row_ids(sheet_id)[0]
    body_text = "B" * 4096
    surface_hash = _text_hash(body_text)
    surface = record_text_surface(
        project,
        surface_kind="cell",
        content_hash=surface_hash,
        offset_unit="unicode_codepoint",
        text_sheet_id=sheet_id,
        text_row_id=first_row,
        text_column_id=columns["body"],
    )
    artifact = record_source_artifact(
        project, artifact_kind="text", media_type="text/plain"
    )
    span = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="text",
        char_start=0,
        char_end=1,
        quote="B",
        text_layer_hash=surface_hash,
        text_surface_id=surface["id"],
    )
    _values, refs = project.get_values_with_refs(
        sheet_id, columns["alongside"], row_ids=[first_row]
    )
    record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref=refs[first_row],
        spans=[{"span_id": span["id"], "rank": 0, "span_role": "annotation"}],
        sheet_id=sheet_id,
        row_id=first_row,
        column_id=columns["alongside"],
        layer_family="integration",
        producer={"kind": "integration.annotation"},
    )
    project.db.commit()
    return sheet_id, columns


def _browse(
    client: TestClient,
    project_id: str,
    sheet_id: int,
    columns: dict[str, int],
    **params,
):
    response = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/documents",
        params={
            "source_column_id": columns["body"],
            "title_column_id": columns["title"],
            **params,
        },
    )
    assert response.status_code == 200, response.text
    return response


def test_document_browse_is_bounded_projection_and_searches_beyond_first_page(
    tmp_path,
) -> None:
    client = make_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Large documents"}).json()[
        "id"
    ]
    sheet_id, columns = _import_documents(client, project_id, count=230, needle=221)

    first = _browse(client, project_id, sheet_id, columns, limit=25)
    payload = first.json()
    assert payload["schema_version"] == "frisket.document_page.v1"
    assert len(payload["items"]) == 25
    assert payload["next_cursor"]
    assert [item["ordinal"] for item in payload["items"]] == list(range(1, 26))
    # Neither the large source body nor an unrelated column crosses this API.
    assert "BBBBBBBBBBBB" not in first.text
    assert "PRIVATE-" not in first.text
    assert set(payload["items"][0]) == {
        "row_id",
        "ordinal",
        "title",
        "title_truncated",
        "source_kind",
        "source_label",
        "source_label_truncated",
        "character_count",
    }

    found = _browse(client, project_id, sheet_id, columns, q="Needle 221").json()
    assert [item["title"] for item in found["items"]] == ["Needle 221"]
    assert found["items"][0]["character_count"] == 4096


def test_document_browse_preserves_scope_filter_sort_and_cursor_order(tmp_path) -> None:
    client = make_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Scoped docs"}).json()["id"]
    sheet_id, columns = _import_documents(client, project_id, count=12)
    project = client.app.state.workspace.get(project_id)
    scope = project.visible_row_ids(sheet_id)[:10]
    params = {
        "filter": json.dumps({"group": {"eq": "keep"}}),
        "sort": json.dumps([{"column": "rank", "dir": "desc"}]),
        "scope_row_ids": ",".join(str(row_id) for row_id in scope),
        "limit": 2,
    }

    first = _browse(client, project_id, sheet_id, columns, **params).json()
    second = _browse(
        client,
        project_id,
        sheet_id,
        columns,
        **params,
        cursor=first["next_cursor"],
    ).json()
    assert [item["title"] for item in first["items"] + second["items"]] == [
        "Document 008",
        "Document 006",
        "Document 004",
        "Document 002",
    ]
    assert [item["ordinal"] for item in first["items"] + second["items"]] == [
        1,
        2,
        3,
        4,
    ]
    previous = _browse(
        client,
        project_id,
        sheet_id,
        columns,
        **params,
        cursor=second["previous_cursor"],
    ).json()
    assert previous["items"] == first["items"]


def test_document_cursor_rejects_cross_project_and_sheet_replay(tmp_path) -> None:
    client = make_client(tmp_path)
    first_project = client.post("/api/projects", json={"name": "First"}).json()["id"]
    second_project = client.post("/api/projects", json={"name": "Second"}).json()["id"]
    first_sheet, first_columns = _import_documents(client, first_project, count=3)
    second_sheet, second_columns = _import_documents(client, second_project, count=3)
    extra_sheet, extra_columns = _import_documents(
        client, first_project, count=3, filename="other.csv"
    )
    assert extra_sheet != first_sheet
    assert (
        client.app.state.workspace.get(first_project).storage_identity
        != client.app.state.workspace.get(second_project).storage_identity
    )
    cursor = _browse(client, first_project, first_sheet, first_columns, limit=1).json()[
        "next_cursor"
    ]

    for project_id, sheet_id, columns in (
        (second_project, second_sheet, second_columns),
        (first_project, extra_sheet, extra_columns),
    ):
        response = client.get(
            f"/api/projects/{project_id}/sheets/{sheet_id}/documents",
            params={
                "source_column_id": columns["body"],
                "title_column_id": columns["title"],
                "cursor": cursor,
                "limit": 1,
            },
        )
        assert response.status_code == 400, response.text
        assert "restart" in response.json()["detail"].lower()


def test_document_browse_missing_and_hidden_sheets_are_not_found(tmp_path) -> None:
    client = make_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Hidden docs"}).json()["id"]
    sheet_id, columns = _import_documents(client, project_id, count=1)
    params = {
        "source_column_id": columns["body"],
        "title_column_id": columns["title"],
    }

    missing = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id + 1000}/documents",
        params=params,
    )
    assert missing.status_code == 404, missing.text

    project = client.app.state.workspace.get(project_id)
    project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (sheet_id,))
    project.db.commit()
    hidden = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/documents", params=params
    )
    assert hidden.status_code == 404, hidden.text


def test_document_browse_parent_scope_and_cursor_are_isolated(tmp_path) -> None:
    client = make_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Child docs"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    parent_sheet = project.add_sheet("Parents")
    parent_name = project.add_column(parent_sheet, "name")
    parent_a, parent_b = project.add_rows(
        parent_sheet,
        [{"name": "A"}, {"name": "B"}],
        {"name": parent_name},
    )
    child_sheet = project.add_sheet("Children", parent_sheet_id=parent_sheet)
    source = project.add_column(child_sheet, "file", type="file")
    title = project.add_column(child_sheet, "title", type="text")
    digest = project.add_blob(
        b"document", filename="document.pdf", mime="application/pdf"
    )
    file_value = {"blob": digest, "filename": "document.pdf", "mime": "application/pdf"}
    project.add_rows(
        child_sheet,
        [
            {"file": file_value, "title": "A one"},
            {"file": file_value, "title": "A two"},
            {"file": file_value, "title": "B one"},
            {"file": file_value, "title": "B two"},
        ],
        {"file": source, "title": title},
        parent_row_ids=[parent_a, parent_a, parent_b, parent_b],
    )
    base = {
        "source_column_id": source,
        "title_column_id": title,
        "limit": 1,
    }

    first_a = client.get(
        f"/api/projects/{project_id}/sheets/{child_sheet}/documents",
        params={**base, "parent_row_id": parent_a},
    )
    assert first_a.status_code == 200, first_a.text
    assert [item["title"] for item in first_a.json()["items"]] == ["A one"]
    cursor_a = first_a.json()["next_cursor"]

    replay_b = client.get(
        f"/api/projects/{project_id}/sheets/{child_sheet}/documents",
        params={**base, "parent_row_id": parent_b, "cursor": cursor_a},
    )
    assert replay_b.status_code == 400, replay_b.text
    assert "restart" in replay_b.json()["detail"].lower()

    first_b = client.get(
        f"/api/projects/{project_id}/sheets/{child_sheet}/documents",
        params={**base, "parent_row_id": parent_b},
    )
    assert first_b.status_code == 200, first_b.text
    assert [item["title"] for item in first_b.json()["items"]] == ["B one"]
