from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from frisket.engine.store import Project
from frisket.upgrades.project.typed_storage_migration import (
    migrate_legacy_citation_texts,
)
from frisket.engine.store.citation_text import read_text_snapshot
from frisket.engine.store.evidence import (
    record_citation,
    record_source_artifact,
    record_source_span,
    record_text_surface,
    resolve_citation_viewer,
)
from frisket.engine.store.transactions import write_transaction


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _native_citation(
    project: Project, *, sheet_id: int, row_id: int, column_id: int, text: str
) -> tuple[dict, dict]:
    _values, refs = project.get_values_with_refs(sheet_id, column_id, row_ids=[row_id])
    content_hash = _hash(text)
    with write_transaction(project.db):
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
    with write_transaction(project.db):
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
    with write_transaction(project.db):
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
    with write_transaction(project.db):
        return record_citation(
            project,
            subject_kind="test",
            subject_ref={"kind": "test"},
            spans=[{"span_id": span["id"]} for span in spans],
        )


def test_native_context_reads_current_text_then_becomes_stale_without_copy(
    make_project,
) -> None:
    project = make_project("native-citation")
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
    current = resolve_citation_viewer(project, link["id"])["artifacts"][0]
    assert current["text_context_status"] == "available"
    assert current["text_context"]["text"] == "Ada wrote this."

    project.apply_edits(
        [{"row_id": row_id, "column_id": column_id, "value": "Changed later."}]
    )
    stale = resolve_citation_viewer(project, link["id"])["artifacts"][0]
    assert stale["text_context_status"] == "stale"
    assert stale["text_context"] is None
    assert stale["spans"][0]["quote"] == "Ada wrote this."
    assert project.db.execute("SELECT COUNT(*) FROM citation_texts").fetchone()[0] == 0

    viewer = resolve_citation_viewer(project, link["id"])
    assert viewer["link"]["status"] == "stale"
    assert viewer["link"]["stale_reason"] == "source_changed"


def test_synthesized_context_is_saved_once_and_remains_viewable(make_project) -> None:
    project = make_project("composite-citation")
    text = "Subject: Ada 🚀"
    content_hash = _hash(text)
    with write_transaction(project.db):
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
        with write_transaction(project.db):
            artifact = record_source_artifact(
                project,
                artifact_kind="text",
                media_type="text/plain",
                metadata={"captured_text": text, "index": index},
            )
        artifacts.append(artifact)
        with write_transaction(project.db):
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
    viewer = resolve_citation_viewer(project, link["id"])
    assert [item["text_context_status"] for item in viewer["artifacts"]] == [
        "available",
        "available",
    ]
    assert [item["text_context"]["text"] for item in viewer["artifacts"]] == [
        text,
        text,
    ]
    assert viewer["link"]["text_layer_hash_mismatch"] is False
    assert all(item["metadata"]["captured_text_frozen"] for item in artifacts)


def test_snapshot_repairs_malformed_unicode_before_hashing_and_keeps_anchors(
    make_project,
) -> None:
    project = make_project("unicode-citation")
    raw_text = "before \ud800 after 🚀"
    text = "before \ufffd after 🚀"
    content_hash = _hash(text)
    with write_transaction(project.db):
        surface = record_text_surface(
            project,
            surface_kind="composite",
            content_hash=content_hash,
            offset_unit="unicode_codepoint",
            surface_ref={"identity": {"kind": "test-unicode"}},
        )
    with write_transaction(project.db):
        artifact = record_source_artifact(
            project,
            artifact_kind="text",
            media_type="text/plain",
            metadata={"captured_text": raw_text},
        )
    with write_transaction(project.db):
        spans = [
            record_source_span(
                project,
                artifact_id=artifact["id"],
                span_kind="text",
                char_start=start,
                char_end=end,
                quote=text[start:end],
                text_layer_hash=content_hash,
                text_surface_id=surface["id"],
            )
            for start, end in ((0, 6), (9, 14))
        ]

    viewer = resolve_citation_viewer(project, _link(project, spans)["id"])
    context = viewer["artifacts"][0]["text_context"]

    assert artifact["metadata"]["captured_text_hash"] == content_hash
    assert read_text_snapshot(project.db, content_hash) == text
    assert context["text"] == text
    assert context["ranges"] == [
        {"span_id": spans[0]["stable_id"], "start": 0, "end": 6},
        {"span_id": spans[1]["stable_id"], "start": 9, "end": 14},
    ]


def test_rendered_json_cell_surface_is_not_compared_as_native_text(
    make_project,
) -> None:
    project = make_project("rendered-json-citation")
    sheet_id = project.add_sheet("Sources")
    column_id = project.add_column(sheet_id, "segments", type="json")
    row_id = project.add_rows(
        sheet_id,
        [{"segments": [{"text": "Ada"}]}],
        {"segments": column_id},
    )[0]
    _values, refs = project.get_values_with_refs(sheet_id, column_id, row_ids=[row_id])
    rendered = "1. Ada"
    content_hash = _hash(rendered)
    with write_transaction(project.db):
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
    with write_transaction(project.db):
        artifact = record_source_artifact(
            project,
            artifact_kind="text",
            media_type="text/plain",
            source_sheet_id=sheet_id,
            source_row_id=row_id,
            source_column_id=column_id,
            metadata={
                "captured_text": rendered,
                "captured_source": {"column_type": "json", "value_ref": refs[row_id]},
            },
        )
    with write_transaction(project.db):
        span = record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="text",
            char_start=3,
            char_end=6,
            quote="Ada",
            text_layer_hash=content_hash,
            text_surface_id=surface["id"],
        )
    link = _link(project, [span])

    viewer = resolve_citation_viewer(project, link["id"])

    assert viewer["link"]["text_layer_hash_mismatch"] is False
    assert viewer["artifacts"][0]["text_context"]["text"] == rendered


def test_viewer_keeps_independent_native_contexts_for_multiple_artifacts(
    make_project,
) -> None:
    project = make_project("batched-citation")
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
    viewer = resolve_citation_viewer(project, link["id"])

    assert len(viewer["artifacts"]) == 2
    assert [artifact["text_context"]["text"] for artifact in viewer["artifacts"]] == [
        "first",
        "second",
    ]
    assert all(
        artifact["text_context_status"] == "available"
        for artifact in viewer["artifacts"]
    )


def test_legacy_inline_context_migrates_to_deduplicated_snapshot(make_project) -> None:
    project = make_project("legacy-citation")
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


def test_snapshot_collision_and_mutation_are_rejected(make_project) -> None:
    project = make_project("citation-collision")
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
