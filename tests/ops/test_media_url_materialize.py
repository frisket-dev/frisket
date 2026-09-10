"""media.fetch_url executor tests.

REDESIGN (2026-06-27): media.fetch_url moved from a hand-rolled per-row plain
action to a recipe-based reserved-MapRunner op (the same base as
media.transcribe / media.ytdlp_download). These tests assert the NEW
reserved-MapRunner SHAPE — a run-based receipt (run_id, per-row results, partial
status from run stats), a single "file" output column, and reservation-based
idempotency replay — while still asserting the SEMANTIC outcome: each row's URL
is fetched, the blob is stored, downloaded rows carry media cells, failed rows
carry none, and the action is partial when some rows fail / failed when all do.

What deliberately changed vs the old hand-rolled shape (and why each is the
reserved-MapRunner shape, not a weakening):
  - No more `{output}_status` / `{output}_error` companion columns: per-row
    outcomes now live in the run `results` table, exactly like transcribe. We
    instead assert the run-based receipt status (partial/failed) and the absence
    of those columns.
  - Output column type is statically "file" (the media cell still carries its
    mime); the old post-download type inference is gone.
  - An existing output column name is now an `output_column_exists` precheck
    error (like youtube_download), replacing the old
    `output_column_type_mismatch` / existing-column reuse path.
  - Per-row download attempts run as a concurrent MapRunner map, so we assert the
    SET of fetched URLs (each fetched exactly once), not their call order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from blob_store_helpers import local_blob_path
from frisket.contracts.action import (
    ActionCatalog,
    Receipt,
)
from frisket.actions.system import root_action_catalog
from frisket.actions.types import ActionRequest
from frisket.actions.file_fetch import FetchParams
import pytest
from pydantic import ValidationError
from frisket.engine.executor import run_action_spec
from frisket.engine.executor import file_fetch as fetch_url_recipe
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.ops.ytdlp import DownloadedMedia
from action_test_helpers import counts as _counts


PROJECT_ID = "project-media-fetch-url"
DIRECT_MP3 = "https://cdn.example/audio/episode.mp3"
HTML_PAGE = "https://example.org/story"
YOUTUBE_WATCH = "https://www.youtube.com/watch?v=fixture123"

# The runs/results tables join the standard set: a reserved-MapRunner action
# creates a run + a map op + per-row results, not a single edit op.
_COUNT_TABLES = ("columns", "ops", "receipts", "blobs", "runs", "results")


def _fetch_url_action(
    *,
    sheet_id: int,
    row_ids: list[int] | None = None,
    input_columns: list[str] | None = None,
    output_name: str = "media",
    idempotency_key: str = "media_fetch_url@sha256:first",
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    assert capabilities is None
    return {
        "action_id": "media.fetch_url",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            **({"row_ids": row_ids} if row_ids is not None else {}),
        },
        "params": {"source": (input_columns or ["url"])[0]},
        "output_names": {"media": output_name},
        "idempotency_key": idempotency_key,
    }


def _seed_url_project(tmp_path: Path) -> tuple[Project, int, list[int]]:
    project = Project.create(tmp_path / "media-fetch-url.frisket", name="Fetch URLs")
    sheet_id = project.add_sheet("Feed")
    columns = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "url": project.add_column(sheet_id, "url", type="link"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {"title": "Audio", "url": DIRECT_MP3},
            {"title": "YouTube", "url": YOUTUBE_WATCH},
            {"title": "HTML", "url": HTML_PAGE},
            {"title": "Bad", "url": "ftp://example.org/file.mp3"},
        ],
        columns,
    )
    return project, sheet_id, row_ids


def _columns(project: Project, sheet_id: int) -> dict[str, Any]:
    return {
        str(column["name"]): column
        for column in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?",
            (sheet_id,),
        ).fetchall()
    }


def _receipt(project: Project, receipt_id: str) -> Receipt:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    return Receipt.model_validate(json.loads(row["body"]))


def _run_with_exact_confirmation(
    project: Project,
    action: dict[str, Any],
):
    """Exercise the real challenge -> exact-hash retry for execution tests."""

    challenge_action = {**action}
    challenge_action.pop("confirmation", None)
    challenge = run_action_spec(project, challenge_action, project_id=PROJECT_ID)
    if challenge.status != "needs_confirmation":
        return challenge
    promise_set_hash = challenge.errors[0].details.get("promise_set_hash")
    assert isinstance(promise_set_hash, str) and promise_set_hash
    return run_action_spec(
        project,
        {**challenge_action, "confirmation": promise_set_hash},
        project_id=PROJECT_ID,
    )


def _patch_downloaders(monkeypatch) -> tuple[list[str], list[str]]:
    direct_seen: list[str] = []
    youtube_seen: list[str] = []

    def fake_download_url(url: str, **kwargs: Any) -> tuple[bytes, str, str, None]:
        direct_seen.append(url)
        if url == HTML_PAGE:
            return b"<html><title>Story</title></html>", "text/html", "story.html", None
        return b"ID3fake", "audio/mpeg", "episode.mp3", None

    def fake_ytdlp(url: str, **kwargs: Any) -> DownloadedMedia:  # noqa: ARG001
        youtube_seen.append(url)
        return DownloadedMedia(
            data=b"RIFFyoutube",
            mime="audio/wav",
            filename="youtube.wav",
            duration_seconds=4.5,
            metadata={"title": "YouTube Fixture", "extractor": "Youtube"},
        )

    # Patch on the recipe module's enclosures/ytdlp module objects (the same
    # frisket.enclosures / frisket.ytdlp modules the recipe calls by attribute
    # inside its per-row execute).
    monkeypatch.setattr(fetch_url_recipe.enclosures, "download_url", fake_download_url)
    monkeypatch.setattr(fetch_url_recipe.ytdlp, "download_media", fake_ytdlp)
    return direct_seen, youtube_seen


def test_media_fetch_url_materializes_direct_youtube_and_html_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    catalog = ActionCatalog.model_validate(
        root_action_catalog().model_dump(mode="json")
    )
    entry = next(item for item in catalog.actions if item.kind == "media.fetch_url")
    assert entry.execution_mode == "per_row"
    assert entry.async_mode == "queued"
    assert entry.writes_project is True
    assert entry.required_capabilities == ["project:write", "external:media_download"]
    assert entry.ui_hints["source_requirements"][0]["accepted_column_types"] == [
        "link",
        "text",
    ]
    # Reserved-MapRunner error surface: an existing output column is now an
    # output_column_exists precheck (replacing output_column_type_mismatch), and
    # reservation idempotency adds idempotency_in_progress.
    assert {error.code for error in entry.errors} >= {
        "invalid_input_ref",
        "invalid_params",
        "output_column_exists",
        "stale_replay",
        "idempotency_conflict",
        "idempotency_in_progress",
    }

    project, sheet_id, row_ids = _seed_url_project(tmp_path)
    action = _fetch_url_action(sheet_id=sheet_id)
    assert FetchParams.model_validate(action["params"]).source.root == "url"
    ActionRequest.model_validate(action)
    with pytest.raises(ValidationError):
        ActionRequest.model_validate(
            {key: value for key, value in action.items() if key != "idempotency_key"}
        )
    with pytest.raises(ValidationError):
        ActionRequest.model_validate({**action, "capabilities": ["project:write"]})

    direct_seen, youtube_seen = _patch_downloaders(monkeypatch)

    try:
        before = _counts(project, _COUNT_TABLES)
        result = _run_with_exact_confirmation(
            project,
            action,
        )
        assert result.status == "partial", result.errors
        assert result.action.kind == "media.fetch_url"
        assert result.receipt_id is not None
        assert result.run_id is not None
        assert [output.name for output in result.outputs] == ["media"]
        # Concurrent MapRunner map: assert each URL was fetched exactly once
        # (set/count), not call order.
        assert sorted(direct_seen) == sorted([DIRECT_MP3, HTML_PAGE])
        assert youtube_seen == [YOUTUBE_WATCH]

        columns = _columns(project, sheet_id)
        assert columns["media"]["type"] == "file"
        # The hand-rolled status/error companion columns are gone; per-row
        # outcomes live in the run results table.
        assert "media_status" not in columns
        assert "media_error" not in columns

        media_values = project.get_values(sheet_id, int(columns["media"]["id"]))
        assert media_values[row_ids[0]]["mime"] == "audio/mpeg"
        assert media_values[row_ids[1]]["mime"] == "audio/wav"
        assert set(media_values[row_ids[1]]) == {"blob", "mime", "filename"}
        assert (
            MediaBlobStore(project)
            .display_metadata(media_values[row_ids[1]]["blob"])
            .get("duration_seconds")
            == 4.5
        )
        assert media_values[row_ids[2]]["mime"] == "text/html"
        # The ftp:// row failed: no media cell written.
        assert media_values.get(row_ids[3]) is None

        # The run recorded a per-row error for the failed row instead of a
        # status/error column.
        run_id = int(result.run_id)
        failed_rows = {
            int(row["row_id"])
            for row in project.db.execute(
                "SELECT row_id FROM results "
                "WHERE run_id=? AND outcome IN ('model_error', 'invalid_output', 'row_error')",
                (run_id,),
            ).fetchall()
        }
        assert failed_rows == {row_ids[3]}

        html_blob = project.db.execute(
            "SELECT source_url, filename, mime FROM blobs WHERE hash=?",
            (media_values[row_ids[2]]["blob"],),
        ).fetchone()
        assert html_blob is not None
        assert html_blob["source_url"] == HTML_PAGE
        assert html_blob["filename"] == "story.html"
        assert html_blob["mime"] == "text/html"

        receipt = _receipt(project, result.receipt_id)
        assert receipt.status == "partial"
        assert receipt.run_id == run_id
        # provider_use semantics preserved: 2 direct httpx requests + 1 yt-dlp
        # request (the invalid ftp row attempts no network).
        assert sorted(
            (item["provider"], item["service"], item["operation_call_count"])
            for item in receipt.provider_use
        ) == [("direct_url", "httpx", 2), ("youtube", "yt-dlp", 1)]
        assert all(
            item["external_api"] and item["cost_actual"] == 0
            for item in receipt.provider_use
        )
        refs = [
            item.ref for item in receipt.inputs + receipt.outputs + receipt.evidence
        ]
        occurrences = [ref for ref in refs if ref["kind"] == "row_file_output"]
        assert len(occurrences) == 3
        assert {ref["facts"]["url"] for ref in occurrences} == {
            DIRECT_MP3,
            HTML_PAGE,
            YOUTUBE_WATCH,
        }
        html_ref = next(ref for ref in occurrences if ref["facts"]["url"] == HTML_PAGE)
        assert html_ref["media_kind"] == "file"
        assert html_ref["may_feed"] == ["media.to_markdown"]
        output_ref = next(ref for ref in refs if ref["kind"] == "map_result_column")
        assert output_ref["type"] == "file"
        assert output_ref["run_id"] == run_id

        before_replay = _counts(project, _COUNT_TABLES)
        direct_seen.clear()
        youtube_seen.clear()
        replay = run_action_spec(project, action, project_id=PROJECT_ID)
        assert replay.status == "partial"
        assert replay.receipt_id == result.receipt_id
        assert [output.name for output in replay.outputs] == ["media"]
        assert replay.outputs[0].column_id == int(columns["media"]["id"])
        # Replay is a pure receipt replay: no new columns/ops/blobs/runs/results
        # and no re-download.
        assert _counts(project, _COUNT_TABLES) == before_replay
        assert before_replay["receipts"] == before["receipts"] + 1
        assert direct_seen == []
        assert youtube_seen == []

        conflict = run_action_spec(
            project,
            _fetch_url_action(
                sheet_id=sheet_id,
                output_name="other_media",
                idempotency_key="media_fetch_url@sha256:first",
            ),
            project_id=PROJECT_ID,
        )
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"

        local_blob_path(project, media_values[row_ids[0]]["blob"]).unlink()
        stale = run_action_spec(project, action, project_id=PROJECT_ID)
        assert stale.status == "failed"
        assert stale.errors[0].code == "stale_replay"
    finally:
        project.close()


def test_media_fetch_url_rejects_existing_output_column_before_network(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # Reserved-MapRunner shape: an existing output column name is refused by the
    # precheck (output_column_exists) before any network call — replacing the old
    # output_column_type_mismatch / existing-column reuse path.
    project, sheet_id, _row_ids = _seed_url_project(tmp_path)
    project.add_column(sheet_id, "media", type="audio")
    direct_seen, youtube_seen = _patch_downloaders(monkeypatch)

    try:
        result = _run_with_exact_confirmation(
            project,
            _fetch_url_action(
                sheet_id=sheet_id,
                output_name="media",
                idempotency_key="media_fetch_url@sha256:exists",
            ),
        )
        assert result.status == "failed"
        assert result.errors[0].code == "output_column_exists"
        assert direct_seen == []
        assert youtube_seen == []
    finally:
        project.close()


def test_media_fetch_url_all_failures_mark_action_failed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # All rows fail -> the run is failed and the action surfaces url_fetch_failed,
    # with no provider_use claimed for attempts that never happened. The semantic
    # invariant (every-row failure => failed, not partial) is preserved in the
    # run-based shape.
    project, sheet_id, row_ids = _seed_url_project(tmp_path)
    direct_seen, youtube_seen = _patch_downloaders(monkeypatch)

    try:
        result = _run_with_exact_confirmation(
            project,
            _fetch_url_action(
                sheet_id=sheet_id,
                row_ids=[row_ids[3]],
                output_name="media",
                idempotency_key="media_fetch_url@sha256:all-failed",
            ),
        )
        assert result.status == "failed"
        assert result.errors[0].code == "external_rows_failed"
        # The ftp:// row never reaches a downloader.
        assert direct_seen == []
        assert youtube_seen == []

        columns = _columns(project, sheet_id)
        assert columns["media"]["type"] == "file"
        assert "media_status" not in columns
        assert "media_error" not in columns
        # The single targeted row failed, so it carries no media cell.
        media_values = project.get_values(sheet_id, int(columns["media"]["id"]))
        assert media_values.get(row_ids[3]) is None
        assert all(value is None for value in media_values.values())

        assert result.receipt_id is not None
        receipt = _receipt(project, result.receipt_id)
        assert receipt.status == "failed"
        assert receipt.run_id is not None
        # No network was attempted, so no provider use is reported.
        assert receipt.provider_use == []
        output_ref = next(
            item.ref
            for item in receipt.outputs
            if item.ref["kind"] == "map_result_column"
        )
        assert output_ref["type"] == "file"
    finally:
        project.close()
