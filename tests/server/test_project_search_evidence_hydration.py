from __future__ import annotations

import io
from dataclasses import dataclass

import pytest

from fastapi import FastAPI
from PIL import Image
from starlette.testclient import TestClient

from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.search_index import drain_index
from frisket.server.routes.project_research import register_project_search_routes
from frisket.server.services.project_search import ProjectSearchService
from frisket.server.services.selector_choices_capabilities import SelectorCapabilities
from tests.engine.test_temporal_transcript_inheritance import _seed_source


@dataclass
class _Workspace:
    project: Project
    embedding_runtime: None = None
    edition: str = "local"

    def get(self, project_id: str) -> Project:
        assert project_id == "project"
        return self.project


def _client(project: Project) -> TestClient:
    app = FastAPI()
    register_project_search_routes(
        app,
        service=ProjectSearchService(_Workspace(project)),  # type: ignore[arg-type]
        capabilities_for=lambda _request, _pid: SelectorCapabilities(),
    )
    return TestClient(app)


def _seed_ocr(project: Project) -> None:
    sheet_id = project.add_sheet("Scans")
    file_column_id = project.add_column(sheet_id, "scan", "file")
    text_column_id = project.add_column(sheet_id, "ocr_text", "text", ai_generated=True)
    output = io.BytesIO()
    Image.new("RGB", (100, 60), color="white").save(output, format="PNG")
    blob_hash = project.add_blob(
        output.getvalue(), filename="scan.png", mime="image/png"
    )
    row_id = project.add_rows(
        sheet_id,
        [
            {
                "scan": {
                    "blob": blob_hash,
                    "filename": "scan.png",
                    "mime": "image/png",
                },
                "ocr_text": "China trade policy",
            }
        ],
        {"scan": file_column_id, "ocr_text": text_column_id},
    )[0]
    artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="image/png",
        blob_hash=blob_hash,
        filename="scan.png",
        source_sheet_id=sheet_id,
        source_row_id=row_id,
        source_column_id=file_column_id,
        metadata={"engine": "fixture", "page_images": {}},
    )
    region = record_source_span(
        project,
        artifact_id=int(artifact["id"]),
        span_kind="region",
        page_start=1,
        page_end=1,
        quote="China",
        bbox=[{"x0": 0.1, "y0": 0.2, "x1": 0.8, "y1": 0.3}],
    )
    trade_region = record_source_span(
        project,
        artifact_id=int(artifact["id"]),
        span_kind="region",
        page_start=1,
        page_end=1,
        quote="trade policy",
        bbox=[{"x0": 0.1, "y0": 0.3, "x1": 0.8, "y1": 0.4}],
    )
    _values, refs = project.get_values_with_refs(
        sheet_id, text_column_id, row_ids=[row_id]
    )
    record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref=refs[row_id],
        spans=[{"span_id": int(region["id"])}, {"span_id": int(trade_region["id"])}],
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=text_column_id,
        link_role="media_ocr_grounding",
        producer={"action_kind": "media.ocr"},
    )


def _search(
    client: TestClient, text: str, *, lexical_syntax: str = "plain"
) -> dict[str, object]:
    response = client.post(
        "/api/projects/project/search",
        json={
            "scope": {"kind": "project"},
            "query": {"kind": "text", "text": text, "lexical_syntax": lexical_syntax},
            "mode": "lexical",
            "group_by": "source",
            "limit": 10,
            "ranking": {"rerank": "off"},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_search_route_hydrates_transcript_and_ocr_from_its_pinned_snapshot(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "located-evidence.frisket")
    try:
        _seed_source(project)
        _seed_ocr(project)
        assert drain_index(project) >= 2

        client = _client(project)
        transcript = _search(client, "Second sentence")
        ocr = _search(client, "China")

        transcript_hits = [
            group["primary"]
            for group in transcript["groups"]
            if group["primary"]["source"]["source_kind"] == "transcript"
        ]
        ocr_hits = [
            group["primary"]
            for group in ocr["groups"]
            if group["primary"]["source"]["source_kind"] == "ocr_text"
        ]
        assert len(transcript_hits) == 1
        assert "sentence" in transcript_hits[0]["snippet"].lower()
        second_span = project.db.execute(
            "SELECT stable_id FROM source_spans WHERE quote='Second sentence.'"
        ).fetchone()
        assert second_span is not None
        assert transcript_hits[0]["navigation"]["span_id"] == second_span["stable_id"]
        assert len(ocr_hits) == 1
        assert "china" in ocr_hits[0]["snippet"].lower()
    finally:
        project.close()


def test_search_identifies_document_and_all_matching_phrase_spans(tmp_path) -> None:
    project = Project.create(tmp_path / "phrase.frisket")
    try:
        _seed_ocr(project)
        drain_index(project)
        payload = _search(_client(project), "China trade")
        hits = [
            group["primary"]
            for group in payload["groups"]
            if group["primary"]["source"]["source_kind"] == "ocr_text"
        ]
        assert len(hits) == 1
        hit = hits[0]
        assert hit["source_name"] == "scan.png"
        expected = {
            row["stable_id"]
            for row in project.db.execute(
                "SELECT stable_id FROM source_spans WHERE quote IN ('China','trade policy')"
            )
        }
        assert set(hit["navigation"]["span_ids"]) == expected
        assert hit["navigation"]["span_id"] in expected
    finally:
        project.close()


@pytest.mark.parametrize("position", ["beginning", "end"])
def test_search_api_bounds_long_token_context_without_changing_absolute_hit(
    tmp_path, position
):
    project = Project.create(tmp_path / "bounded-snippet.frisket")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        query = f"beacon{position}"
        context = f"😀 Ω café é & <script>plain text</script> {query} Ω 😀"
        if position == "beginning":
            text = context + " " + "unbrokentoken" * 80_660
        else:
            text = "longtoken" * 2_330_169 + " " + context
        [row] = project.add_rows(sheet, [{"body": text}], {"body": column})
        drain_index(project)
        payload = _search(_client(project), query)
        [group] = payload["groups"]
        hit = group["primary"]
        snippet = hit["snippet"]
        assert len(snippet) <= 520
        assert len(snippet.encode("utf-8")) <= 4096
        assert f"<b>{query}</b>" in snippet
        assert snippet.count("<b>") == snippet.count("</b>") == 1
        assert "FRISKET_SEARCH_HIT" not in snippet
        assert "…" in snippet
        assert "😀" in snippet
        # The raw-text wire contract is rendered through escaped React text nodes.
        assert "<script>plain text</script>" in snippet
        assert "&lt;script" not in snippet
        location = hit["location"]
        assert location["precision"] == "exact"
        expected_start = text.index(query)
        assert location["text"]["start"] == expected_start
        assert location["text"]["end"] == expected_start + len(query)
        assert hit["navigation"]["text_range"] == location["text"]
        assert hit["binding"]["row_id"] == row
        assert hit["binding"]["column_id"] == column
    finally:
        project.close()


def test_search_api_caps_a_matched_token_excerpt_without_shortening_its_locator(
    tmp_path,
):
    project = Project.create(tmp_path / "matched-token.frisket")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body")
        token = "massivematch" + "x" * 5000
        text = "😀 prefix " + token + " Ω tail"
        project.add_rows(sheet, [{"body": text}], {"body": column})
        drain_index(project)
        [group] = _search(_client(project), "massivematch*", lexical_syntax="fts")[
            "groups"
        ]
        hit = group["primary"]
        snippet = hit["snippet"]
        assert len(snippet) <= 520
        assert len(snippet.encode("utf-8")) <= 4096
        assert "<b>massivematch" in snippet
        assert snippet.endswith("</b>…")
        assert snippet.count("<b>") == snippet.count("</b>") == 1
        location = hit["location"]
        assert location["precision"] == "exact"
        assert location["text"]["start"] == len("😀 prefix ")
        assert location["text"]["end"] == len("😀 prefix ") + len(token)
        assert hit["navigation"]["text_range"] == location["text"]
    finally:
        project.close()
