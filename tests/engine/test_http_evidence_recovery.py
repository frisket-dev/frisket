from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
    record_text_surface,
)
from frisket.server.app import create_app


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _counts(project) -> tuple[int, ...]:
    return tuple(
        project.db.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM source_artifacts),"
            "(SELECT COUNT(*) FROM source_spans),"
            "(SELECT COUNT(*) FROM evidence_links),"
            "(SELECT COUNT(*) FROM citation_texts)"
        ).fetchone()
    )


def test_manual_text_recovery_is_a_read_only_viewer_preview(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "Recovery"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Sources")
    column_id = project.add_column(sheet_id, "body", type="text")
    original = "Ada wrote this."
    row_id = project.add_rows(sheet_id, [{"body": original}], {"body": column_id})[0]
    _values, refs = project.get_values_with_refs(sheet_id, column_id, row_ids=[row_id])
    surface = record_text_surface(
        project,
        surface_kind="cell",
        content_hash=_hash(original),
        offset_unit="unicode_codepoint",
        text_sheet_id=sheet_id,
        text_row_id=row_id,
        text_column_id=column_id,
        value_ref=refs[row_id],
    )
    text_artifact = record_source_artifact(
        project,
        artifact_kind="text",
        media_type="text/plain",
        source_sheet_id=sheet_id,
        source_row_id=row_id,
        source_column_id=column_id,
        captured_text_native=True,
        metadata={"captured_text": original},
    )
    text_span = record_source_span(
        project,
        artifact_id=text_artifact["id"],
        span_kind="text",
        char_start=0,
        char_end=3,
        quote="Ada",
        text_layer_hash=_hash(original),
        text_surface_id=surface["id"],
    )
    pdf_hash = project.add_blob(b"%PDF-1.4", "source.pdf", "application/pdf")
    media_artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        blob_hash=pdf_hash,
        page_count=1,
    )
    media_span = record_source_span(
        project,
        artifact_id=media_artifact["id"],
        span_kind="region",
        page_start=1,
        page_end=1,
        bbox=[
            {
                "space": "page_normalized",
                "x0": 0.1,
                "y0": 0.2,
                "x1": 0.3,
                "y1": 0.4,
            }
        ],
        quote="Ada",
    )
    link = record_evidence_link(
        project,
        subject_kind="test",
        subject_ref={"kind": "test"},
        spans=[{"span_id": text_span["id"]}, {"span_id": media_span["id"]}],
    )
    endpoint = f"/api/projects/{project_id}/evidence/links/{link['stable_id']}/viewer"

    project.apply_edits(
        [
            {
                "row_id": row_id,
                "column_id": column_id,
                "value": "🚀 Ada and Ada",
            }
        ]
    )
    counts_before = _counts(project)

    ordinary = client.get(endpoint)
    located = client.get(endpoint, params={"locate_current": "true"})

    assert ordinary.status_code == located.status_code == 200
    ordinary_payload = ordinary.json()
    assert ordinary_payload["link"]["status"] == "stale"
    assert ordinary_payload["link"]["stale_reason"] == "source_changed"
    ordinary_text = next(
        artifact
        for artifact in ordinary_payload["artifacts"]
        if artifact["id"] == text_artifact["id"]
    )
    assert ordinary_text["text_context_status"] == "stale"
    assert ordinary_text["text_context"] is None
    assert "recovery_status" not in ordinary_text

    located_artifacts = {
        artifact["id"]: artifact for artifact in located.json()["artifacts"]
    }
    located_text = located_artifacts[text_artifact["id"]]
    assert located_text["recovery_status"] == "located"
    assert located_text["text_context"] == {
        "text": "🚀 Ada and Ada",
        "offset_unit": "utf16_code_unit",
        "ranges": [
            {"span_id": text_span["stable_id"], "start": 3, "end": 6},
            {"span_id": text_span["stable_id"], "start": 11, "end": 14},
        ],
    }
    located_media = located_artifacts[media_artifact["id"]]
    assert located_media["recovery_status"] == "unsupported"
    assert located_media["pages"][0]["regions"][0]["bbox"] == [
        {
            "space": "page_normalized",
            "x0": 0.1,
            "y0": 0.2,
            "x1": 0.3,
            "y1": 0.4,
        }
    ]
    assert _counts(project) == counts_before
    assert (
        project.db.execute(
            "SELECT status FROM evidence_links WHERE id=?", (link["id"],)
        ).fetchone()[0]
        == "active"
    )

    project.apply_edits(
        [{"row_id": row_id, "column_id": column_id, "value": "Grace only"}]
    )
    missing = client.get(endpoint, params={"locate_current": "true"}).json()
    missing_text = next(
        artifact
        for artifact in missing["artifacts"]
        if artifact["id"] == text_artifact["id"]
    )
    assert missing_text["recovery_status"] == "not_found"
    assert missing_text["text_context"] == {
        "text": "Grace only",
        "offset_unit": "utf16_code_unit",
        "ranges": [],
    }
