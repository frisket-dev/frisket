"""Portable table downloads retain structured results and their stored media."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import tempfile
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from frisket.engine.store.media_blobs import media_cell


def _seed(client: TestClient, filename: str = "../../clip.mp3"):
    pid = client.post("/api/projects", json={"name": "Portable"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("Recordings")
    cols = {
        "title": project.add_column(sheet, "title"),
        "media": project.add_column(sheet, "media", type="audio"),
        "transcript": project.add_column(sheet, "transcript"),
        "segments": project.add_column(
            sheet, "segments", type="json", default_hidden=True
        ),
    }
    audio = b"ID3 example audio" * 100
    digest = project.add_blob(audio, filename=filename, mime="audio/mpeg")
    segments = [{"start": 0.2, "end": 4.5, "text": "Hello, world."}]
    rows = project.add_rows(
        sheet,
        [
            {
                "title": "keep",
                "media": media_cell(digest, filename=filename, mime="audio/mpeg"),
                "transcript": "Hello, world.\nSecond line.",
                "segments": segments,
            },
            {"title": "skip", "media": "https://example.org/external.mp3"},
        ],
        cols,
    )
    return pid, project, sheet, rows, digest, audio, segments


def _download(client, pid, sheets, format_="csv", **kwargs):
    return client.get(
        f"/api/projects/{pid}/exports/sheets",
        params=[
            *(("sheet_id", sid) for sid in sheets),
            ("format", format_),
            ("include_attachments", "true"),
            *kwargs.items(),
        ],
    )


def _csv(archive, name="Recordings.csv"):
    return list(csv.DictReader(io.StringIO(archive.read(name).decode("utf-8-sig"))))


@pytest.mark.parametrize("filename", ["../../clip.mp3", "音声.mp3"])
def test_csv_attachment_export_preserves_transcript_and_timings(
    replay_client, filename
):
    pid, project, sheet, rows, digest, audio, segments = _seed(replay_client, filename)
    response = _download(replay_client, pid, [sheet])
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        exported = _csv(archive)
        path = exported[0]["media.path"]
        assert path.startswith(f"attachments/{digest}/")
        assert ".." not in Path(path).parts
        assert Path(path).suffix == ".mp3"
        assert archive.read(path) == audio
        assert exported[0]["media.filename"] == filename
        assert exported[0]["transcript"] == "Hello, world.\nSecond line."
        assert json.loads(exported[0]["segments"]) == segments
        assert exported[1]["media.path"] == ""
        assert exported[1]["media.source_url"] == "https://example.org/external.mp3"


def test_selected_sheets_deduplicate_files_and_retain_pdf_pages(replay_client):
    pid, project, sheet, rows, digest, audio, segments = _seed(replay_client)
    other = project.add_sheet("Pages")
    col = project.add_column(other, "pdf", type="file")
    pdf = project.add_blob(
        b"%PDF original", filename="clip.mp3", mime="application/pdf"
    )
    project.add_rows(
        other,
        [
            {"pdf": {"blob": pdf, "page": 2}},
            {"pdf": {"blob": pdf, "page": 5}},
            {"pdf": {"blob": digest, "filename": "other.mp3"}},
        ],
        {"pdf": col},
    )
    response = _download(replay_client, pid, [sheet, other])
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        records, pages = _csv(archive), _csv(archive, "Pages.csv")
        assert (
            len(
                [name for name in archive.namelist() if name.startswith("attachments/")]
            )
            == 2
        )
        assert pages[2]["pdf.path"] == records[0]["media.path"]
        assert pages[0]["pdf.path"] == pages[1]["pdf.path"]
        assert [row["pdf.page"] for row in pages[:2]] == ["2", "5"]
        assert archive.read(pages[0]["pdf.path"]) == b"%PDF original"


@pytest.mark.parametrize("format_", ["csv", "xlsx"])
def test_attachment_export_respects_current_view(replay_client, format_):
    pid, project, sheet, rows, digest, audio, segments = _seed(replay_client)
    response = _download(
        replay_client,
        pid,
        [sheet],
        format_,
        filter=json.dumps({"title": {"eq": "skip"}}),
    )
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert not any(name.startswith("attachments/") for name in archive.namelist())
        if format_ == "csv":
            assert [row["title"] for row in _csv(archive)] == ["skip"]
        else:
            with archive.open("Portable.xlsx") as source:
                workbook = load_workbook(source, read_only=True)
                values = list(workbook.active.values)
                assert len(values) == 2
                assert values[1][0] == "skip"
                assert "media.path" in values[0]
                workbook.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_attachment_fails_without_leaving_temp_archive(
    replay_client, tmp_path, monkeypatch, damage
):
    pid, project, sheet, rows, digest, audio, segments = _seed(replay_client)
    with project.materialize_blob(digest) as path:
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"damaged")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    response = _download(replay_client, pid, [sheet])
    assert response.status_code == 409, response.text
    assert "attachment" in response.json()["detail"].lower()
    assert not list(tmp_path.glob("frisket-sheet-export-*"))


def test_attachment_path_header_collision_is_rejected(replay_client):
    pid, project, sheet, rows, digest, audio, segments = _seed(replay_client)
    project.add_column(sheet, "media.path")
    response = _download(replay_client, pid, [sheet])
    assert response.status_code == 400, response.text
    assert "media.path" in response.json()["detail"]


@pytest.mark.parametrize("format_", ["csv", "xlsx"])
def test_selected_rows_package_only_their_attachments(replay_client, format_):
    pid, project, sheet, rows, digest, audio, segments = _seed(replay_client)
    unrelated = project.add_blob(
        b"other audio", filename="other.mp3", mime="audio/mpeg"
    )
    cols = {str(col["name"]): int(col["id"]) for col in project.columns(sheet)}
    project.add_rows(
        sheet, [{"title": "unrelated", "media": {"blob": unrelated}}], cols
    )
    response = _download(
        replay_client, pid, [sheet], format_, scope_row_ids=str(rows[0])
    )
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        if format_ == "csv":
            values = _csv(archive)
        else:
            with archive.open("Portable.xlsx") as source:
                workbook = load_workbook(source, read_only=True)
                items = list(workbook.active.values)
                values = [dict(zip(items[0], row)) for row in items[1:]]
                workbook.close()
        assert len(values) == 1
        assert values[0]["transcript"] == "Hello, world.\nSecond line."
        assert json.loads(values[0]["segments"]) == segments
        names = [name for name in archive.namelist() if name.startswith("attachments/")]
        assert names == [values[0]["media.path"]]
        assert archive.read(names[0]) == audio
        assert archive.getinfo(names[0]).compress_type == zipfile.ZIP_STORED


def test_archive_is_removed_when_download_disconnects(replay_client):
    from frisket.server.routes.exports import _ClosingFileResponse
    from frisket.server.services.sheet_export import (
        SheetDatasetExportService,
        SheetDatasetFile,
    )

    pid, project, sheet, rows, digest, audio, segments = _seed(replay_client)
    service = SheetDatasetExportService(replay_client.app.state.workspace)
    export = service.export(
        pid,
        [sheet],
        format_="csv",
        filter_=None,
        sort=None,
        formula_policy="escape",
        include_attachments=True,
    )
    assert isinstance(export, SheetDatasetFile)
    assert export.path.is_file()
    response = _ClosingFileResponse(export)

    async def send(message):
        if message["type"] == "http.response.body":
            raise ConnectionError("client disconnected")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(ConnectionError, match="client disconnected"):
        asyncio.run(
            response({"type": "http", "method": "GET", "headers": []}, receive, send)
        )
    assert not export.path.exists()
    assert not export.path.parent.exists()
