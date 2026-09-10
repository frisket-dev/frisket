"""The two HTTP surfaces the annotated-text reader needs.

`SheetMeta.annotatedTextColumnIds` is the availability signal that lets a
text-only sheet (a Paste import has no media column at all) offer the Document
view; `GET .../cells/{row}/{col}/annotations` is the per-cell layer payload the
marks are drawn from. Both wrap store functions that are already tested in
isolation (tests/engine/test_text_annotations_query.py), so what is pinned here
is the WIRING: the field is always present, it is the narrow signal rather than
`cited_column_ids`, and the route is addressed by cell like `cell_evidence`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    _text_hash,
    record_evidence_link,
    record_source_artifact,
    record_source_span,
    record_text_surface,
)
from frisket.server.app import create_app


PROJECT_ID = "annotations"
BODY_TEXT = "Ada Lovelace met Charles Babbage."


def _seed(ws: Path) -> dict[str, Any]:
    """One annotated text sheet + one plain sheet, with NO media column
    anywhere: the Paste-import shape the reader has to be reachable from."""
    project = Project.create(ws / f"{PROJECT_ID}.frisket", name="Annotations")
    try:
        sheet_id = project.add_sheet("Pasted rows")
        body = project.add_column(sheet_id, "body", type="text")
        ents = project.add_column(sheet_id, "entities", type="json")
        row_ids = project.add_rows(
            sheet_id,
            [{"body": BODY_TEXT, "entities": [{"text": "Ada Lovelace"}]}],
            {"body": body, "entities": ents},
        )
        row_id = row_ids[0]

        surface_hash = _text_hash(BODY_TEXT)
        surface = record_text_surface(
            project,
            surface_kind="cell",
            content_hash=surface_hash,
            offset_unit="unicode_codepoint",
            text_sheet_id=sheet_id,
            text_row_id=row_id,
            text_column_id=body,
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
                quote=BODY_TEXT[start:end],
                text_layer_hash=surface_hash,
                text_surface_id=surface["id"],
                metadata={"entity_type": entity_type},
            )
            for start, end, entity_type in [(0, 12, "person"), (17, 32, "person")]
        ]
        _values, refs = project.get_values_with_refs(sheet_id, ents, row_ids=[row_id])
        record_evidence_link(
            project,
            subject_kind="cell",
            subject_ref=refs[row_id],
            spans=[
                {"span_id": span["id"], "rank": i, "span_role": "annotation"}
                for i, span in enumerate(spans)
            ],
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=ents,
            layer_family="entities",
            producer={"kind": "map.ner", "engine": "gliner"},
        )

        plain_sheet_id = project.add_sheet("Plain")
        plain_col = project.add_column(plain_sheet_id, "x", type="text")
        plain_row = project.add_rows(
            plain_sheet_id, [{"x": "no marks"}], {"x": plain_col}
        )[0]

        project.db.commit()
        return {
            "sheet_id": sheet_id,
            "body": body,
            "entities": ents,
            "row_id": row_id,
            "plain_sheet_id": plain_sheet_id,
            "plain_col": plain_col,
            "plain_row": plain_row,
        }
    finally:
        project.close()


def _client(ws: Path) -> TestClient:
    return TestClient(create_app(ws, router=ModelRouter(cache=None, cache_mode="off")))


def _sheets(client: TestClient) -> dict[int, dict[str, Any]]:
    response = client.get(f"/api/projects/{PROJECT_ID}/sheets")
    assert response.status_code == 200, response.text
    return {int(s["id"]): s for s in response.json()}


def test_sheet_list_names_the_annotated_SOURCE_column(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed(ws)
    by_id = _sheets(_client(ws))

    # The BODY column (whose text the marks index), not the entities column the
    # layer's evidence link is subject to — that one is what cited_column_ids
    # reports, and gating the reader on it would offer the Document view on
    # every sheet that has any evidence at all.
    assert by_id[ids["sheet_id"]]["annotated_text_column_ids"] == [ids["body"]]
    assert by_id[ids["sheet_id"]]["cited_column_ids"] == [ids["entities"]]


def test_unannotated_sheet_reports_an_empty_list_not_a_missing_key(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed(ws)
    by_id = _sheets(_client(ws))

    assert "annotated_text_column_ids" in by_id[ids["plain_sheet_id"]]
    assert by_id[ids["plain_sheet_id"]]["annotated_text_column_ids"] == []


def test_cell_annotations_route_returns_positioned_spans(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed(ws)
    client = _client(ws)

    response = client.get(
        f"/api/projects/{PROJECT_ID}/cells/{ids['row_id']}/{ids['body']}/annotations"
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    # The reader draws over the string the route returns, at the offsets the
    # route returns — never a re-located quote.
    assert payload["text"] == BODY_TEXT
    assert payload["offset_unit"] == "utf16_code_unit"
    assert payload["sheet_id"] == ids["sheet_id"]
    (layer,) = payload["layers"]
    assert layer["positioned"] is True
    assert layer["layer_family"] == "entities"
    assert layer["producer"]["kind"] == "map.ner"
    assert [(s["start"], s["end"], s["quote"]) for s in layer["spans"]] == [
        (0, 12, "Ada Lovelace"),
        (17, 32, "Charles Babbage"),
    ]


def test_cell_with_no_layers_is_a_200_with_an_empty_list(tmp_path: Path) -> None:
    """ "Nothing annotated this text" is an answer, not a 404 — the reader shows
    the text with no marks rather than an error."""
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed(ws)
    client = _client(ws)

    response = client.get(
        f"/api/projects/{PROJECT_ID}/cells/{ids['plain_row']}/{ids['plain_col']}/annotations"
    )
    assert response.status_code == 200, response.text
    assert response.json()["layers"] == []
    assert response.json()["text"] == "no marks"


def test_unknown_cell_is_a_typed_404(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(ws)
    client = _client(ws)

    response = client.get(f"/api/projects/{PROJECT_ID}/cells/999999/888888/annotations")
    assert response.status_code == 404
    # bare_json ActionError envelope, same as cell_evidence's own miss.
    assert response.json()["code"] == "cell_not_found"
