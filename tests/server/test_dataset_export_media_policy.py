"""Media columns flatten into fixed reference columns, never bytes.

Default media_policy=references expands a media column into a base display
column plus the fixed sibling schema, enriched from the cell envelope and the
blobs table. No blob bytes, base64, or local filesystem paths are exported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.server.exports.plan import build_sheet_export_plan, iter_export_row_batches
from frisket.server.exports.rowset import ExportError
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.server.app import create_app

MEDIA_SIBLINGS = [
    "media.blob",
    "media.mime_type",
    "media.filename",
    "media.size_bytes",
    "media.source_url",
    "media.duration_seconds",
    "media.width",
    "media.height",
    "media.pages",
]


def _project(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post("/api/projects", json={"name": "Media"}).json()["id"]
    return client.app.state.workspace.get(pid)


def _first_row(project, plan) -> dict[str, Any]:
    for batch in iter_export_row_batches(project, plan):
        for row in batch.rows:
            return dict(zip(plan.column_names, row))
    raise AssertionError("no rows")


def _add_audio_blob(project) -> tuple[str, bytes]:
    data = b"ID3 fake mp3 payload bytes" * 4
    digest = project.add_blob(
        data,
        filename="clip.mp3",
        mime="audio/mpeg",
        source_url="https://example.org/clip.mp3",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 12.5}
        ),
    )
    return digest, data


def test_audio_media_cell_exports_fixed_reference_columns(tmp_path: Path) -> None:
    project = _project(tmp_path)
    sheet_id = project.add_sheet("clips")
    cols = {
        "title": project.add_column(sheet_id, "title"),
        "media": project.add_column(sheet_id, "media", type="audio"),
    }
    digest, data = _add_audio_blob(project)
    cell = media_cell(digest, mime="audio/mpeg", filename="clip.mp3")
    project.add_rows(sheet_id, [{"title": "A", "media": cell}], cols)

    plan = build_sheet_export_plan(project, sheet_id)
    assert plan.column_names == ["title", "media", *MEDIA_SIBLINGS]

    row = _first_row(project, plan)
    assert row["media"] == "clip.mp3"  # display = filename
    assert row["media.blob"] == digest  # content hash, not bytes/path
    assert row["media.mime_type"] == "audio/mpeg"
    assert row["media.filename"] == "clip.mp3"
    assert row["media.size_bytes"] == len(data)
    assert row["media.source_url"] == "https://example.org/clip.mp3"
    assert row["media.duration_seconds"] == 12.5
    # never a local filesystem path
    with project.materialize_blob(digest) as path:
        assert str(path) not in {str(v) for v in row.values()}


def test_sparse_media_cell_enriches_from_blobs(tmp_path: Path) -> None:
    project = _project(tmp_path)
    sheet_id = project.add_sheet("clips")
    cols = {"media": project.add_column(sheet_id, "media", type="audio")}
    digest, data = _add_audio_blob(project)
    # only the blob hash on the cell; everything else must come from blobs
    project.add_rows(sheet_id, [{"media": {"blob": digest}}], cols)

    plan = build_sheet_export_plan(project, sheet_id)
    row = _first_row(project, plan)
    assert row["media.blob"] == digest
    assert row["media.mime_type"] == "audio/mpeg"
    assert row["media.filename"] == "clip.mp3"
    assert row["media.size_bytes"] == len(data)
    assert row["media.source_url"] == "https://example.org/clip.mp3"


def test_url_string_media_cell_uses_source_url(tmp_path: Path) -> None:
    project = _project(tmp_path)
    sheet_id = project.add_sheet("pics")
    cols = {"media": project.add_column(sheet_id, "media", type="image")}
    project.add_rows(sheet_id, [{"media": "https://example.org/p.png"}], cols)

    plan = build_sheet_export_plan(project, sheet_id)
    row = _first_row(project, plan)
    assert row["media.source_url"] == "https://example.org/p.png"
    assert row["media"] == "https://example.org/p.png"  # display falls to source_url
    assert row["media.blob"] is None


def test_media_sibling_collision_with_user_column_fails(tmp_path: Path) -> None:
    project = _project(tmp_path)
    sheet_id = project.add_sheet("clips")
    # a user column literally named "media.blob" collides with the generated sibling
    project.add_column(sheet_id, "media", type="audio")
    project.add_column(sheet_id, "media.blob")

    with pytest.raises(ExportError) as excinfo:
        build_sheet_export_plan(project, sheet_id)
    assert excinfo.value.code == "export_column_collision"
    assert excinfo.value.details.get("name") == "media.blob"


def test_media_policy_omit_drops_media_columns(tmp_path: Path) -> None:
    project = _project(tmp_path)
    sheet_id = project.add_sheet("clips")
    cols = {
        "title": project.add_column(sheet_id, "title"),
        "media": project.add_column(sheet_id, "media", type="audio"),
    }
    digest, _data = _add_audio_blob(project)
    project.add_rows(sheet_id, [{"title": "A", "media": {"blob": digest}}], cols)

    plan = build_sheet_export_plan(project, sheet_id, media_policy="omit")
    assert plan.column_names == ["title"]
    row = _first_row(project, plan)
    assert row == {"title": "A"}
