from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor import run_action_spec
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.executor.document_convert import _BoundDocumentConverter
from frisket.engine.store import Project
from frisket.engine.store.evidence import list_cell_evidence, resolve_evidence_viewer


PROJECT_ID = "project-to-markdown-text-spans"


def _to_markdown_action(
    *,
    sheet_id: int,
    row_ids: list[int],
    input_columns: list[str] | None = None,
    idempotency_key: str = "media_to_markdown@sha256:text-spans",
) -> dict[str, Any]:
    return {
        "action_id": "media.to_markdown",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            "row_ids": row_ids,
        },
        "params": {
            "source": (input_columns or ["doc"])[0],
            "engine": "markitdown",
        },
        "output_names": {"markdown": "markdown"},
        "idempotency_key": idempotency_key,
    }


def _counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "source_artifacts",
            "source_spans",
            "evidence_links",
            "evidence_link_spans",
        )
    }


def _seed_project(tmp_path: Path) -> dict[str, Any]:
    project = Project.create(
        tmp_path / "to-markdown-text-spans.frisket",
        name="Text spans",
    )
    sheet_id = project.add_sheet("Documents")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "doc": project.add_column(sheet_id, "doc", type="file"),
    }

    # Row A: converts to non-trivial markdown, exercises the span shape.
    blob_a = project.add_blob(
        b"<html><body><h1>A</h1></body></html>",
        filename="a.html",
        mime="text/html",
        source_url="https://docs.example/a.html",
    )
    # Row B: converts to empty markdown (e.g. a blank/whitespace-only doc).
    blob_b = project.add_blob(
        b"<html><body></body></html>",
        filename="b.html",
        mime="text/html",
        source_url="https://docs.example/b.html",
    )

    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Doc A",
                "doc": media_cell(blob_a, mime="text/html", filename="a.html"),
            },
            {
                "title": "Doc B",
                "doc": media_cell(blob_b, mime="text/html", filename="b.html"),
            },
        ],
        cols,
    )
    return {
        "project": project,
        "sheet_id": sheet_id,
        "row_ids": row_ids,
        "blobs": {"a": blob_a, "b": blob_b},
    }


_MARKDOWN_BY_BLOB = {
    "a": "# A\n\nSome converted body text about widgets.",
    "b": "",
}


def _fake_markitdown(blobs: dict[str, str]):
    # `_resolve_input` gives an html-mime blob an ".html" extension via a
    # scratch copy, so `path.name` is no longer
    # the blob digest -- hash the scratch copy's bytes instead (identical to
    # the original blob content) and match against the seeded digests.
    by_digest = {digest: label for label, digest in blobs.items()}

    async def fake_convert(
        self: _BoundDocumentConverter, path: Path, scratch: Path
    ) -> str:
        del self, scratch
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        label = by_digest[digest]
        return _MARKDOWN_BY_BLOB[label]

    return fake_convert


def _markdown_column_id(project: Project, sheet_id: int) -> int:
    row = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='markdown'",
        (sheet_id,),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seeded = _seed_project(tmp_path)
    project: Project = seeded["project"]
    monkeypatch.setattr(
        _BoundDocumentConverter,
        "_convert_markitdown",
        _fake_markitdown(seeded["blobs"]),
    )
    before = _counts(project)
    result = run_action_spec(
        project,
        _to_markdown_action(sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"]),
        project_id=PROJECT_ID,
    )
    assert result.status == "completed", result.errors
    seeded["before_counts"] = before
    seeded["result"] = result
    return seeded


def test_markdown_row_writes_quote_free_source_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _run(tmp_path, monkeypatch)
    project: Project = seeded["project"]
    sheet_id = seeded["sheet_id"]
    row_a = seeded["row_ids"][0]
    try:
        markdown_column_id = _markdown_column_id(project, sheet_id)
        cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_a,
            column_id=markdown_column_id,
            project_id=PROJECT_ID,
        )
        assert len(cell_evidence["links"]) == 1
        assert cell_evidence["links"][0]["role"] == "source_provenance"
        assert cell_evidence["links"][0]["snippet"] is None
        link_stable_id = cell_evidence["links"][0]["stable_id"]

        viewer = resolve_evidence_viewer(project, link_stable_id, project_id=PROJECT_ID)
        assert len(viewer["artifacts"]) == 1
        artifact = viewer["artifacts"][0]
        assert artifact["artifact_kind"] == "file"
        assert artifact["media_type"] == "text/html"

        spans = artifact["spans"]
        assert len(spans) == 1
        span = spans[0]
        assert span["span_kind"] == "whole"
        assert span["quote"] is None
        assert span["snippet"] is None
        assert "char_start" not in span["selector"]
        assert span["text_layer_hash"] is None
    finally:
        project.close()


def test_markdown_support_span_claims_no_surface_and_is_not_a_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole-output support quote needs neither offsets nor a reader layer."""
    from frisket.engine.store.text_annotations import resolve_text_annotations

    seeded = _run(tmp_path, monkeypatch)
    project: Project = seeded["project"]
    sheet_id = seeded["sheet_id"]
    row_a = seeded["row_ids"][0]
    try:
        markdown_column_id = _markdown_column_id(project, sheet_id)
        surface = project.db.execute(
            "SELECT surface_kind, text_column_id FROM text_surfaces "
            "WHERE text_row_id=? AND surface_kind='cell'",
            (row_a,),
        ).fetchone()
        assert surface is None

        res = resolve_text_annotations(
            project, sheet_id=sheet_id, row_id=row_a, column_id=markdown_column_id
        )
        assert res["layers"] == []  # support span is never a reader layer
    finally:
        project.close()


def test_provenance_span_does_not_copy_the_derived_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _run(tmp_path, monkeypatch)
    project: Project = seeded["project"]
    sheet_id = seeded["sheet_id"]
    row_a = seeded["row_ids"][0]
    try:
        markdown_column_id = _markdown_column_id(project, sheet_id)
        stored_markdown = project.get_values(
            sheet_id, markdown_column_id, row_ids=[row_a]
        )[row_a]
        assert stored_markdown == _MARKDOWN_BY_BLOB["a"]

        cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_a,
            column_id=markdown_column_id,
            project_id=PROJECT_ID,
        )
        viewer = resolve_evidence_viewer(
            project, cell_evidence["links"][0]["stable_id"], project_id=PROJECT_ID
        )
        span = viewer["artifacts"][0]["spans"][0]
        assert span["quote"] is None
        assert span["snippet"] is None
        assert "char_start" not in span["selector"]
        assert span["text_layer_hash"] is None
    finally:
        project.close()


def test_zero_content_row_writes_no_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _run(tmp_path, monkeypatch)
    project: Project = seeded["project"]
    sheet_id = seeded["sheet_id"]
    row_b = seeded["row_ids"][1]
    try:
        markdown_column_id = _markdown_column_id(project, sheet_id)
        cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_b,
            column_id=markdown_column_id,
            project_id=PROJECT_ID,
        )
        assert cell_evidence["links"] == []
        no_artifact = project.db.execute(
            "SELECT COUNT(*) FROM source_artifacts WHERE source_row_id=?",
            (row_b,),
        ).fetchone()[0]
        assert no_artifact == 0
    finally:
        project.close()


def test_evidence_link_binds_to_markdown_output_cell_not_input_doc_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _run(tmp_path, monkeypatch)
    project: Project = seeded["project"]
    sheet_id = seeded["sheet_id"]
    row_a = seeded["row_ids"][0]
    try:
        markdown_column_id = _markdown_column_id(project, sheet_id)
        cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_a,
            column_id=markdown_column_id,
            project_id=PROJECT_ID,
        )
        assert len(cell_evidence["links"]) == 1
        link = cell_evidence["links"][0]
        assert link["status"] == "active"

        doc_column_id = project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='doc'", (sheet_id,)
        ).fetchone()["id"]
        doc_cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_a,
            column_id=int(doc_column_id),
            project_id=PROJECT_ID,
        )
        assert doc_cell_evidence["links"] == []
    finally:
        project.close()


def test_evidence_counts_match_multi_row_expectations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _run(tmp_path, monkeypatch)
    project: Project = seeded["project"]
    try:
        after = _counts(project)
        before = seeded["before_counts"]
        # 1 row (A) writes evidence; B (empty markdown) writes nothing.
        assert after["source_artifacts"] == before["source_artifacts"] + 1
        assert after["source_spans"] == before["source_spans"] + 1
        assert after["evidence_links"] == before["evidence_links"] + 1
        assert after["evidence_link_spans"] == before["evidence_link_spans"] + 1
    finally:
        project.close()


def test_inline_text_input_writes_text_artifact_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(
        tmp_path / "to-markdown-text-input.frisket",
        name="Inline text input",
    )
    try:
        sheet_id = project.add_sheet("Pages")
        cols = {
            "title": project.add_column(sheet_id, "title", type="text"),
            "raw_html": project.add_column(sheet_id, "raw_html", type="text"),
        }
        row_ids = project.add_rows(
            sheet_id,
            [{"title": "Inline", "raw_html": "<html><body><h1>Hi</h1></body></html>"}],
            cols,
        )

        async def fake_convert(
            self: _BoundDocumentConverter, path: Path, scratch: Path
        ) -> str:
            del self, scratch, path
            return "# Hi"

        monkeypatch.setattr(
            _BoundDocumentConverter, "_convert_markitdown", fake_convert
        )
        result = run_action_spec(
            project,
            _to_markdown_action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                input_columns=["raw_html"],
                idempotency_key="media_to_markdown@sha256:inline-text",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors

        markdown_column_id = _markdown_column_id(project, sheet_id)
        cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_ids[0],
            column_id=markdown_column_id,
            project_id=PROJECT_ID,
        )
        assert len(cell_evidence["links"]) == 1
        viewer = resolve_evidence_viewer(
            project, cell_evidence["links"][0]["stable_id"], project_id=PROJECT_ID
        )
        artifact = viewer["artifacts"][0]
        assert artifact["artifact_kind"] == "text"
        assert artifact["artifact_ref"]["blob"] is None
        span = artifact["spans"][0]
        assert span["span_kind"] == "whole"
        assert span["quote"] is None
        assert span["text_layer_hash"] is None
    finally:
        project.close()


def test_replay_does_not_duplicate_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed_project(tmp_path)
    project: Project = seeded["project"]
    monkeypatch.setattr(
        _BoundDocumentConverter,
        "_convert_markitdown",
        _fake_markitdown(seeded["blobs"]),
    )
    try:
        action = _to_markdown_action(
            sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"]
        )
        first = run_action_spec(project, action, project_id=PROJECT_ID)
        assert first.status == "completed"
        after_first = _counts(project)

        replay = run_action_spec(project, action, project_id=PROJECT_ID)
        assert replay.status == "completed"
        assert replay.receipt_id == first.receipt_id
        assert _counts(project) == after_first
    finally:
        project.close()
