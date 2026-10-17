from __future__ import annotations

import hashlib
import tracemalloc
from types import SimpleNamespace

from fastapi.testclient import TestClient

from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
    record_text_surface,
    resolve_evidence_viewer,
)
from frisket.engine.store.text_annotations import resolve_text_annotations
from frisket.server.app import create_app


def _hash(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _native(tmp_path, *, megabytes=1):
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Passages"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("documents")
    body = project.add_column(sheet, "body", type="text")
    entities = project.add_column(sheet, "entities", type="json")
    text = (
        "opening\0"
        + "a" * (megabytes * 1024 * 1024)
        + "😀 Ada Lovelace met Babbage. tail"
    )
    start = text.index("Ada Lovelace")
    [row] = project.add_rows(
        sheet,
        [
            {
                "body": text,
                "entities": [
                    {
                        "text": "Ada Lovelace",
                        "type": "person",
                        "fingerprint": "entity:ada",
                        "raw": "x" * (5 * 1024 * 1024),
                    }
                ],
            }
        ],
        {"body": body, "entities": entities},
    )
    surface = record_text_surface(
        project,
        surface_kind="cell",
        content_hash=_hash(text),
        offset_unit="unicode_codepoint",
        text_sheet_id=sheet,
        text_row_id=row,
        text_column_id=body,
    )
    artifact = record_source_artifact(
        project,
        artifact_kind="text",
        media_type="text/plain",
        source_sheet_id=sheet,
        source_row_id=row,
        source_column_id=body,
        captured_text_native=True,
        metadata={"captured_text": text},
    )
    spans = [
        record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="text",
            char_start=left,
            char_end=right,
            quote=text[left:right],
            text_layer_hash=_hash(text),
            text_surface_id=surface["id"],
            metadata={"entity_type": "person"},
        )
        for left, right in ((start, start + 12), (start + 5, start + 24))
    ]
    _, refs = project.get_values_with_refs(sheet, entities, [row])
    link = record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref=refs[row],
        sheet_id=sheet,
        row_id=row,
        column_id=entities,
        layer_family="entities",
        spans=[{"span_id": span["id"], "span_role": "annotation"} for span in spans],
    )
    return SimpleNamespace(
        client=client,
        pid=pid,
        project=project,
        sheet=sheet,
        body=body,
        entities=entities,
        row=row,
        text=text,
        start=start,
        surface=surface,
        artifact=artifact,
        spans=spans,
        link=link,
    )


def test_annotation_tail_jump_clips_marks_and_keeps_fingerprint_without_large_values(
    tmp_path,
):
    fx = _native(tmp_path)
    occurrence = f"{fx.spans[0]['id']}:{fx.link['id']}"
    endpoint = f"/api/projects/{fx.pid}/cells/{fx.row}/{fx.body}/annotations"
    response = fx.client.get(
        endpoint, params={"occurrence_id": occurrence, "limit": 64}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    window = body["window"]
    assert body["text"] == fx.text[window["start"] : window["end"]]
    assert len(response.content) < 20_000
    mark = body["layers"][0]["spans"][0]
    assert mark["source_start"] == fx.start
    assert (
        mark["start"]
        == len(fx.text[window["start"] : fx.start].encode("utf-16-le")) // 2
    )
    assert mark["metadata"]["entity_fingerprint"] == "entity:ada"
    partial = fx.client.get(
        endpoint,
        params={
            "start": fx.start + 4,
            "limit": 10,
            "revision": window["retrieval_revision"],
        },
    ).json()
    clipped = partial["layers"][0]["spans"][0]
    assert clipped["start"] == 0
    assert clipped["end"] == 8
    assert clipped["source_start"] == fx.start
    assert clipped["quote"] == "Ada Lovelace"
    assert len(partial["layers"][0]["spans"]) == 2
    tracemalloc.start()
    try:
        resolve_text_annotations(
            fx.project,
            sheet_id=fx.sheet,
            row_id=fx.row,
            column_id=fx.body,
            occurrence_id=occurrence,
            limit=64,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024


def test_large_viewer_quote_is_explicit_excerpt_and_context_stays_bounded(tmp_path):
    fx = _native(tmp_path, megabytes=5)
    entire = record_source_span(
        fx.project,
        artifact_id=fx.artifact["id"],
        span_kind="text",
        char_start=0,
        char_end=len(fx.text),
        quote=fx.text,
        snippet=fx.text,
        text_layer_hash=_hash(fx.text),
        text_surface_id=fx.surface["id"],
    )
    link = record_evidence_link(
        fx.project,
        subject_kind="test",
        subject_ref={"kind": "test"},
        spans=[{"span_id": entire["id"]}, {"span_id": fx.spans[0]["id"]}],
    )
    endpoint = f"/api/projects/{fx.pid}/evidence/links/{link['stable_id']}/viewer"
    response = fx.client.get(
        endpoint,
        params={
            "text_artifact_id": fx.artifact["id"],
            "text_span_id": fx.spans[0]["stable_id"],
            "text_limit": 100,
        },
    )
    assert response.status_code == 200, response.text
    artifact = response.json()["artifacts"][0]
    context = artifact["text_context"]
    assert context["evidence_link_id"] == link["stable_id"]
    assert context["artifact_stable_id"] == fx.artifact["stable_id"]
    assert "Ada Lovelace" in context["text"]
    assert len(context["ranges"]) == 2
    assert artifact["spans"][0]["quote_truncated"] is True
    assert artifact["spans"][0]["snippet_truncated"] is True
    assert artifact["spans"][0]["quote"] == fx.text[:2048]
    assert len(response.content) < 40_000
    tracemalloc.start()
    try:
        resolve_evidence_viewer(
            fx.project,
            link["id"],
            text_artifact_id=fx.artifact["id"],
            text_span_id=fx.spans[0]["stable_id"],
            text_limit=100,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024


def test_source_edits_reject_revision_and_keep_legacy_hash_staleness(tmp_path):
    fx = _native(tmp_path)
    endpoint = f"/api/projects/{fx.pid}/evidence/links/{fx.link['stable_id']}/viewer"
    context = fx.client.get(endpoint).json()["artifacts"][0]["text_context"]
    revision = context["window"]["retrieval_revision"]
    fx.project.apply_edits(
        [{"row_id": fx.row, "column_id": fx.body, "value": "changed"}]
    )
    stale = fx.client.get(endpoint).json()
    assert stale["link"]["status"] == "stale"
    assert stale["link"]["text_layer_hash_mismatch"] is True
    assert stale["artifacts"][0]["text_context"] is None
    assert (
        fx.client.get(
            endpoint,
            params={
                "text_artifact_id": fx.artifact["id"],
                "text_start": 1,
                "text_revision": revision,
            },
        ).status_code
        == 409
    )
    annotations = f"/api/projects/{fx.pid}/cells/{fx.row}/{fx.body}/annotations"
    assert (
        fx.client.get(
            annotations, params={"start": 1, "revision": revision}
        ).status_code
        == 409
    )
    assert (
        fx.client.get(
            annotations, params={"occurrence_id": "999999:999999"}
        ).status_code
        == 400
    )


def test_frozen_transcript_window_keeps_local_utf16_timestamps(tmp_path):
    fx = _native(tmp_path)
    text = "a" * 100_000 + "😀 spoken words tail"
    start = text.index("spoken")
    surface = record_text_surface(
        fx.project,
        surface_kind="composite",
        content_hash=_hash(text),
        offset_unit="unicode_codepoint",
        surface_ref={"identity": {"kind": "frozen-transcript"}},
    )
    segments = [
        {"span_id": "offscreen", "start": 0, "end": 5, "start_ms": 0, "end_ms": 10},
        {
            "span_id": "spoken",
            "start": start,
            "end": start + 12,
            "start_ms": 100,
            "end_ms": 200,
            "speaker": "Reporter",
        },
    ]
    artifact = record_source_artifact(
        fx.project,
        artifact_kind="av",
        media_type="audio/wav",
        metadata={
            "captured_text": text,
            "timestamped_transcript": {
                "schema_version": "frisket.timestamped_text_context.v1",
                "offset_unit": "unicode_codepoint",
                "evidence_link_stable_id": "link-test",
                "artifact_stable_id": "artifact-test",
                "segments": segments,
            },
        },
    )
    span = record_source_span(
        fx.project,
        artifact_id=artifact["id"],
        span_kind="temporal",
        start_ms=100,
        end_ms=200,
        char_start=start,
        char_end=start + 12,
        quote=text[start : start + 12],
        text_layer_hash=_hash(text),
        text_surface_id=surface["id"],
    )
    link = record_evidence_link(
        fx.project,
        subject_kind="test",
        subject_ref={"kind": "test"},
        spans=[{"span_id": span["id"]}],
    )
    endpoint = f"/api/projects/{fx.pid}/evidence/links/{link['stable_id']}/viewer"
    response = fx.client.get(
        endpoint,
        params={
            "text_artifact_id": artifact["id"],
            "text_start": start - 2,
            "text_limit": 10,
        },
    )
    assert response.status_code == 200, response.text
    context = response.json()["artifacts"][0]["text_context"]
    assert context["window"]["retrieval_revision"] is None
    [segment] = context["transcript"]["segments"]
    assert segment["source_start"] == start
    assert segment["start"] == 3
    assert segment["end"] == len(context["text"].encode("utf-16-le")) // 2
    assert segment["speaker"] == "Reporter"
    assert segment["start_ms"] == 100


def test_large_locate_current_is_bounded_and_never_reports_partial_scan_as_no_match(
    tmp_path,
):
    fx = _native(tmp_path, megabytes=5)
    endpoint = f"/api/projects/{fx.pid}/evidence/links/{fx.link['stable_id']}/viewer"
    fx.project.apply_edits(
        [{"row_id": fx.row, "column_id": fx.body, "value": "extra prefix " + fx.text}]
    )
    response = fx.client.get(
        endpoint, params={"locate_current": True, "text_artifact_id": fx.artifact["id"]}
    )
    assert response.status_code == 200, response.text
    artifact = response.json()["artifacts"][0]
    assert artifact["recovery_status"] == "located"
    assert artifact["text_context"]["recovered"] is True
    assert "Ada Lovelace" in artifact["text_context"]["text"]
    assert len(response.content) < 40_000
    fx.project.apply_edits(
        [{"row_id": fx.row, "column_id": fx.body, "value": "x" * (5 * 1024 * 1024)}]
    )
    response = fx.client.get(endpoint, params={"locate_current": True}).json()
    artifact = response["artifacts"][0]
    assert artifact["recovery_status"] == "incomplete"
    assert artifact["recovery_scanned_characters"] < 2 * 1024 * 1024
    assert "bounded search" in artifact["recovery_notice"]
