from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from frisket.engine.store import Project
from frisket.engine.store.citation_text import (
    migrate_legacy_citation_texts,
    read_text_snapshot,
    resolve_current_native_texts,
)
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
    record_text_surface,
    resolve_evidence_viewer,
)


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _native_citation(
    project: Project, *, sheet_id: int, row_id: int, column_id: int, text: str
) -> tuple[dict, dict]:
    _values, refs = project.get_values_with_refs(sheet_id, column_id, row_ids=[row_id])
    content_hash = _hash(text)
    surface = record_text_surface(
        project,
        surface_kind="cell",
        content_hash=content_hash,
        offset_unit="unicode_codepoint",
        text_sheet_id=sheet_id,
        text_row_id=row_id,
        text_column_id=column_id,
        value_ref=refs[row_id],
    )
    artifact = record_source_artifact(
        project,
        artifact_kind="text",
        media_type="text/plain",
        source_sheet_id=sheet_id,
        source_row_id=row_id,
        source_column_id=column_id,
        captured_text_native=True,
        metadata={"captured_text": text},
    )
    span = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="text",
        char_start=0,
        char_end=len(text),
        quote=text,
        text_layer_hash=content_hash,
        text_surface_id=surface["id"],
    )
    return artifact, span


def _link(project: Project, spans: list[dict]) -> dict:
    return record_evidence_link(
        project,
        subject_kind="test",
        subject_ref={"kind": "test"},
        spans=[{"span_id": span["id"]} for span in spans],
    )


def test_native_context_reads_current_text_then_becomes_stale_without_copy(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "native-citation.frisket")
    sheet_id = project.add_sheet("Sources")
    column_id = project.add_column(sheet_id, "body", type="text")
    row_id = project.add_rows(
        sheet_id, [{"body": "Ada wrote this."}], {"body": column_id}
    )[0]
    artifact, span = _native_citation(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=column_id,
        text="Ada wrote this.",
    )
    link = _link(project, [span])

    assert "captured_text" not in artifact["metadata"]
    assert artifact["metadata"]["captured_text_hash"] == _hash("Ada wrote this.")
    assert "captured_text_frozen" not in artifact["metadata"]
    assert project.db.execute("SELECT COUNT(*) FROM citation_texts").fetchone()[0] == 0
    current = resolve_evidence_viewer(project, link["id"])["artifacts"][0]
    assert current["text_context_status"] == "available"
    assert current["text_context"]["text"] == "Ada wrote this."

    project.apply_edits(
        [{"row_id": row_id, "column_id": column_id, "value": "Changed later."}]
    )
    stale = resolve_evidence_viewer(project, link["id"])["artifacts"][0]
    assert stale["text_context_status"] == "stale"
    assert stale["text_context"] is None
    assert stale["spans"][0]["quote"] == "Ada wrote this."
    assert project.db.execute("SELECT COUNT(*) FROM citation_texts").fetchone()[0] == 0

    viewer = resolve_evidence_viewer(project, link["id"])
    assert viewer["link"]["status"] == "stale"
    assert viewer["link"]["stale_reason"] == "source_changed"
    assert resolve_current_native_texts(project.db, viewer["artifacts"]) == {
        artifact["id"]: "Changed later."
    }


def test_synthesized_context_is_saved_once_and_remains_viewable(tmp_path) -> None:
    project = Project.create(tmp_path / "composite-citation.frisket")
    text = "Subject: Ada 🚀"
    content_hash = _hash(text)
    surface = record_text_surface(
        project,
        surface_kind="composite",
        content_hash=content_hash,
        offset_unit="unicode_codepoint",
        surface_ref={"identity": {"kind": "test-template", "id": 1}},
    )
    artifacts = []
    spans = []
    for index in range(2):
        artifact = record_source_artifact(
            project,
            artifact_kind="text",
            media_type="text/plain",
            metadata={"captured_text": text, "index": index},
        )
        artifacts.append(artifact)
        spans.append(
            record_source_span(
                project,
                artifact_id=artifact["id"],
                span_kind="text",
                char_start=9,
                char_end=12,
                quote="Ada",
                text_layer_hash=content_hash,
                text_surface_id=surface["id"],
            )
        )
    link = _link(project, spans)

    assert project.db.execute("SELECT COUNT(*) FROM citation_texts").fetchone()[0] == 1
    viewer = resolve_evidence_viewer(project, link["id"])
    assert [item["text_context_status"] for item in viewer["artifacts"]] == [
        "available",
        "available",
    ]
    assert [item["text_context"]["text"] for item in viewer["artifacts"]] == [
        text,
        text,
    ]
    assert all(item["metadata"]["captured_text_frozen"] for item in artifacts)


def test_viewer_batches_native_context_resolution_for_multiple_artifacts(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "batched-citation.frisket")
    sheet_id = project.add_sheet("Sources")
    column_id = project.add_column(sheet_id, "body", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [{"body": "first"}, {"body": "second"}],
        {"body": column_id},
    )
    artifacts_and_spans = [
        _native_citation(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            text=text,
        )
        for row_id, text in zip(row_ids, ("first", "second"), strict=True)
    ]
    link = _link(project, [item[1] for item in artifacts_and_spans])
    statements: list[str] = []
    project.db.set_trace_callback(statements.append)
    try:
        viewer = resolve_evidence_viewer(project, link["id"])
    finally:
        project.db.set_trace_callback(None)

    assert len(viewer["artifacts"]) == 2
    context_queries = [
        sql
        for sql in statements
        if sql.startswith("WITH requested(sheet_id,row_id,column_id")
    ]
    assert len(context_queries) == 1


def test_legacy_inline_context_migrates_to_deduplicated_snapshot(tmp_path) -> None:
    project = Project.create(tmp_path / "legacy-citation.frisket")
    legacy = json.dumps({"captured_text": "saved history", "kept": True})
    project.db.executemany(
        "INSERT INTO source_artifacts "
        "(stable_id,artifact_kind,media_type,metadata) VALUES (?,?,?,?)",
        [
            ("source_artifact:legacy-1", "text", "text/plain", legacy),
            ("source_artifact:legacy-2", "text", "text/plain", legacy),
        ],
    )

    migrate_legacy_citation_texts(project.db)

    metadata = [
        json.loads(row[0])
        for row in project.db.execute(
            "SELECT metadata FROM source_artifacts ORDER BY id"
        )
    ]
    assert metadata == [
        {
            "captured_text_frozen": True,
            "captured_text_hash": _hash("saved history"),
            "kept": True,
        },
        {
            "captured_text_frozen": True,
            "captured_text_hash": _hash("saved history"),
            "kept": True,
        },
    ]
    assert project.db.execute("SELECT COUNT(*) FROM citation_texts").fetchone()[0] == 1
    assert read_text_snapshot(project.db, _hash("saved history")) == "saved history"


def test_snapshot_collision_and_mutation_are_rejected(tmp_path) -> None:
    project = Project.create(tmp_path / "citation-collision.frisket")
    content_hash = _hash("wanted")
    project.db.execute(
        "INSERT INTO citation_texts(content_hash,text) VALUES (?,?)",
        (content_hash, "different"),
    )
    with pytest.raises(sqlite3.IntegrityError, match="hash collision"):
        record_source_artifact(
            project,
            artifact_kind="text",
            media_type="text/plain",
            metadata={"captured_text": "wanted"},
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        project.db.execute(
            "UPDATE citation_texts SET text='changed' WHERE content_hash=?",
            (content_hash,),
        )
