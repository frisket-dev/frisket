from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.ops.integrations.hosted_error import HostedEngineError
from frisket.ops.ytdlp import (
    YOUTUBE_PROVIDER_BLOCKED_CODE,
    YOUTUBE_PROVIDER_BLOCKED_MESSAGE,
    DownloadedMedia,
)

PROJECT_ID = "project-media-youtube-download"
YOUTUBE_WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
TIKTOK_VIDEO = "https://www.tiktok.com/@creator/video/7123456789"

# These actions now use the typed-host tests below rather than legacy Op harness cases.
CASES = []


def run_action_with_confirmation(project, action, **kwargs):
    kwargs.setdefault("router", ModelRouter())
    result = run_action_spec(project, action, **kwargs)
    if result.status == "needs_confirmation":
        result = run_action_spec(
            project,
            {**action, "confirmation": result.errors[0].details["promise_set_hash"]},
            **kwargs,
        )
    return result


def _youtube_action(
    *,
    sheet_id,
    row_ids=None,
    input_columns=None,
    media_type="audio",
    output_name="media",
    format_selector=None,
    extra_opts=None,
    idempotency_key="download-proof",
    output_intent=None,
):
    from frisket.actions.media_download_types import download_outputs

    return {
        "action_id": "media.ytdlp_download",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "params": {
            "source": (input_columns or ["url"])[0],
            "media_type": media_type,
            "format_selector": format_selector,
            "extra_opts": extra_opts,
        },
        "output_names": {
            key: output_name if key == media_type else output_name + "_" + key
            for key in download_outputs(media_type, extra_opts)
        },
        "replace_existing": bool(output_intent),
        "idempotency_key": idempotency_key,
    }


def _seed_youtube_project(tmp_path: Path) -> tuple[Project, int, list[int]]:
    project = Project.create(
        tmp_path / "media-youtube-download.frisket", name="YouTube v1"
    )
    sheet_id, row_ids = _seed_sheet(project)
    return project, sheet_id, row_ids


def _seed_sheet(
    project: Project, *, urls: list[str] | None = None
) -> tuple[int, list[int]]:
    sheet_id = project.add_sheet("Videos")
    columns = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "url": project.add_column(sheet_id, "url", type="link"),
    }
    urls = urls if urls is not None else [YOUTUBE_WATCH, "https://youtu.be/dQw4w9WgXcQ"]
    row_ids = project.add_rows(
        sheet_id,
        [
            {"title": f"Episode {index + 1}", "url": url}
            for index, url in enumerate(urls)
        ],
        columns,
    )
    return sheet_id, row_ids


def _columns(project: Project, sheet_id: int) -> dict[str, Any]:
    return {
        str(column["name"]): column
        for column in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }


def _receipt(project: Project, receipt_id: str):
    from frisket.contracts.action import Receipt

    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    return Receipt.model_validate(json.loads(row["body"]))


def _png():
    import io
    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, format="PNG")
    return stream.getvalue()


def test_typed_download_confirmation_identity_and_stale_replay(tmp_path, monkeypatch):
    from frisket.ops import ytdlp

    project, sheet_id, _ = _seed_youtube_project(tmp_path)
    calls = []
    monkeypatch.setattr(
        ytdlp,
        "download_media",
        lambda url, **_: (
            calls.append(url) or DownloadedMedia(b"audio", "audio/wav", "a.wav")
        ),
    )
    try:
        action = _youtube_action(sheet_id=sheet_id)
        router = ModelRouter()
        quote = run_action_spec(project, action, project_id=PROJECT_ID, router=router)
        assert quote.status == "needs_confirmation"
        assert calls == []
        wrong = run_action_spec(
            project,
            {**action, "confirmation": "0" * 64},
            project_id=PROJECT_ID,
            router=router,
        )
        assert wrong.status == "needs_confirmation"
        assert calls == []
        result = run_action_with_confirmation(
            project, action, project_id=PROJECT_ID, router=router
        )
        assert result.status == "completed", result.errors
        assert len(calls) == 2
        replay = run_action_with_confirmation(
            project, action, project_id=PROJECT_ID, router=router
        )
        assert replay.receipt_id == result.receipt_id
        assert len(calls) == 2
        conflict = run_action_with_confirmation(
            project,
            _youtube_action(sheet_id=sheet_id, media_type="video"),
            project_id=PROJECT_ID,
            router=router,
        )
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"
        project.db.execute(
            "UPDATE columns SET hidden=1 WHERE sheet_id=? AND name='media'", (sheet_id,)
        )
        project.db.commit()
        stale = run_action_with_confirmation(
            project, action, project_id=PROJECT_ID, router=router
        )
        assert stale.status == "failed"
        assert stale.errors[0].code == "stale_replay"
        assert len(calls) == 2
    finally:
        project.close()


def test_typed_download_catalog_and_inactive_output_names():
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest

    registered = ACTION_REGISTRY.get("media.ytdlp_download")
    catalog = registered.catalog_entry()
    assert catalog["execution_mode"] == "per_row"
    assert catalog["required_capabilities"] == [
        "project:write",
        "external:media_download",
    ]
    request = ActionRequest.model_validate(_youtube_action(sheet_id=1))
    assert (
        BoundTypedActionRequest.bind(registered, request).params.media_type == "audio"
    )
    with pytest.raises(ValueError):
        BoundTypedActionRequest.bind(
            registered, request.model_copy(update={"output_names": {"video": "media"}})
        )


def test_failed_download_leaves_no_running_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import frisket.ops.ytdlp as youtube_ops

    project, sheet_id, _row_ids = _seed_youtube_project(tmp_path)

    def failing_download(url: str, **kwargs: Any) -> DownloadedMedia:  # noqa: ARG001
        raise RuntimeError("yt-dlp unavailable")

    monkeypatch.setattr(youtube_ops, "download_media", failing_download)
    try:
        failing = run_action_with_confirmation(
            project,
            _youtube_action(
                sheet_id=sheet_id,
                idempotency_key="media_ytdlp_download@sha256:failed",
            ),
            project_id=PROJECT_ID,
        )
        assert failing.status == "failed"
        assert failing.errors[0].code == "external_rows_failed"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE status='running'"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_media_youtube_cloud_block_surfaces_stable_non_secret_row_diagnosis(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import frisket.ops.ytdlp as youtube_ops

    project, sheet_id, _ = _seed_youtube_project(tmp_path)

    def provider_blocked(url: str, **kwargs: Any) -> DownloadedMedia:  # noqa: ARG001
        raise HostedEngineError(
            code=YOUTUBE_PROVIDER_BLOCKED_CODE,
            message=YOUTUBE_PROVIDER_BLOCKED_MESSAGE,
            retryable=False,
        )

    monkeypatch.setattr(youtube_ops, "download_media", provider_blocked)
    try:
        result = run_action_with_confirmation(
            project,
            _youtube_action(
                sheet_id=sheet_id,
                idempotency_key="media_ytdlp_download@sha256:provider-blocked",
            ),
            project_id=PROJECT_ID,
        )

        assert result.status == "failed"
        assert result.errors[0].code == "external_rows_failed"
        row_errors = project.db.execute(
            "SELECT error, error_code FROM results WHERE run_id=? ORDER BY row_id",
            (result.run_id,),
        ).fetchall()
        assert len(row_errors) == 2
        assert {row["error_code"] for row in row_errors} == {
            YOUTUBE_PROVIDER_BLOCKED_CODE
        }
        assert {row["error"] for row in row_errors} == {
            YOUTUBE_PROVIDER_BLOCKED_MESSAGE
        }
        assert all("cloud-hosted egress IPs" in row["error"] for row in row_errors)
    finally:
        project.close()


def test_media_download_action_supports_tiktok_without_youtube_backfill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import frisket.ops.ytdlp as media_ops

    project = Project.create(tmp_path / "media-tiktok.frisket", name="TikTok")
    sheet_id = project.add_sheet("Videos")
    columns = {
        "url": project.add_column(sheet_id, "url", type="link"),
        "channel_id": project.add_column(sheet_id, "channel_id", type="text"),
        "channel_title": project.add_column(sheet_id, "channel_title", type="text"),
    }
    row_id = project.add_rows(sheet_id, [{"url": TIKTOK_VIDEO}], columns)[0]

    def fake_download(url: str, **_kwargs: Any) -> DownloadedMedia:
        assert url == TIKTOK_VIDEO
        return DownloadedMedia(
            data=b"fake tiktok video",
            mime="video/mp4",
            filename="tiktok.mp4",
            metadata={
                "provider": "tiktok",
                "extractor": "TikTok",
                "channel_id": "tiktok-creator-id",
                "channel_title": "TikTok Creator",
            },
        )

    monkeypatch.setattr(media_ops, "download_media", fake_download)

    try:
        result = run_action_with_confirmation(
            project,
            _youtube_action(
                sheet_id=sheet_id,
                media_type="video",
                idempotency_key="media_ytdlp_download@sha256:tiktok",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        receipt = _receipt(project, result.receipt_id)
        assert (
            next(p for p in receipt.provider_use if p.get("external_api"))["provider"]
            == "tiktok"
        )
        assert (
            next(p for p in receipt.provider_use if p.get("external_api"))[
                "operation_call_count"
            ]
            == 1
        )
        assert (
            project.get_values(sheet_id, columns["channel_id"], row_ids=[row_id])[
                row_id
            ]
            is None
        )
        assert (
            project.get_values(sheet_id, columns["channel_title"], row_ids=[row_id])[
                row_id
            ]
            is None
        )
    finally:
        project.close()


def test_media_download_action_passes_arbitrary_http_domain_to_ytdlp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import frisket.ops.ytdlp as media_ops

    url = "http://127.0.0.1:8080/custom/video"
    project = Project.create(tmp_path / "media-arbitrary.frisket", name="Arbitrary")
    sheet_id = project.add_sheet("Videos")
    columns = {"url": project.add_column(sheet_id, "url", type="link")}
    project.add_rows(sheet_id, [{"url": url}], columns)

    def fake_download(candidate: str, **_kwargs: Any) -> DownloadedMedia:
        assert candidate == url
        return DownloadedMedia(
            data=b"custom video",
            mime="video/mp4",
            filename="custom.mp4",
            metadata={"provider": "generic", "extractor": "Generic"},
        )

    monkeypatch.setattr(media_ops, "download_media", fake_download)

    try:
        result = run_action_with_confirmation(
            project,
            _youtube_action(
                sheet_id=sheet_id,
                media_type="video",
                idempotency_key="media_ytdlp_download@sha256:arbitrary",
            ),
            project_id=PROJECT_ID,
        )

        assert result.status == "completed", result.errors
        receipt = _receipt(project, result.receipt_id)
        assert (
            next(p for p in receipt.provider_use if p.get("external_api"))["provider"]
            == "generic"
        )
        assert (
            next(p for p in receipt.provider_use if p.get("external_api"))[
                "operation_call_count"
            ]
            == 1
        )
    finally:
        project.close()


def test_media_download_overwrite_existing_intent_reuses_ai_column_but_protects_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An ``overwrite_existing`` output intent (the frontend's confirm button)
    reuses an AI-generated collision; a SOURCE column collision stays blocked
    even under the intent — same rule as map.template's."""
    import frisket.ops.ytdlp as youtube_ops

    project, sheet_id, _row_ids = _seed_youtube_project(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_download(url: str, **kwargs: Any) -> DownloadedMedia:
        calls.append({"url": url, **kwargs})
        return DownloadedMedia(
            data=f"fake media bytes {len(calls)}".encode("utf-8"),
            mime="audio/wav",
            filename=f"clip-{len(calls)}.wav",
            duration_seconds=1.25,
            metadata={"title": f"Clip {len(calls)}", "extractor": "Youtube"},
        )

    monkeypatch.setattr(youtube_ops, "download_media", fake_download)

    try:
        first = run_action_with_confirmation(
            project,
            _youtube_action(
                sheet_id=sheet_id,
                idempotency_key="media_ytdlp_download@sha256:ov-1",
            ),
            project_id=PROJECT_ID,
        )
        assert first.status == "completed", first.errors
        media_column = project.db.execute(
            "SELECT ai_generated FROM columns WHERE sheet_id=? AND name='media'",
            (sheet_id,),
        ).fetchone()
        assert media_column is not None and media_column["ai_generated"] == 1

        blocked = run_action_with_confirmation(
            project,
            _youtube_action(
                sheet_id=sheet_id,
                idempotency_key="media_ytdlp_download@sha256:ov-2",
            ),
            project_id=PROJECT_ID,
        )
        assert blocked.status == "failed"
        assert blocked.errors[0].code == "output_column_exists"

        overwritten = run_action_with_confirmation(
            project,
            _youtube_action(
                sheet_id=sheet_id,
                idempotency_key="media_ytdlp_download@sha256:ov-3",
                output_intent=[{"kind": "overwrite_existing"}],
            ),
            project_id=PROJECT_ID,
        )
        assert overwritten.status == "completed", overwritten.errors
        assert [output.name for output in overwritten.outputs] == ["media"]

        protected = run_action_with_confirmation(
            project,
            _youtube_action(
                sheet_id=sheet_id,
                output_name="url",
                idempotency_key="media_ytdlp_download@sha256:ov-4",
                output_intent=[{"kind": "overwrite_existing"}],
            ),
            project_id=PROJECT_ID,
        )
        assert protected.status == "failed"
        assert protected.errors[0].code == "output_column_exists"
    finally:
        project.close()


def test_media_download_action_keeps_row_level_failures_for_bad_urls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import frisket.ops.ytdlp as youtube_ops

    project = Project.create(
        tmp_path / "media-youtube-download-mixed.frisket", name="YouTube v1 mixed"
    )
    sheet_id, row_ids = _seed_sheet(
        project, urls=[YOUTUBE_WATCH, "not a youtube watch url"]
    )
    calls: list[str] = []

    def fake_download(url: str, **kwargs: Any) -> DownloadedMedia:  # noqa: ARG001
        calls.append(url)
        return DownloadedMedia(
            data=b"valid media",
            mime="audio/wav",
            filename="valid.wav",
            duration_seconds=2.0,
            metadata={"title": "Valid", "extractor": "Youtube"},
        )

    monkeypatch.setattr(youtube_ops, "download_media", fake_download)

    try:
        all_invalid = run_action_with_confirmation(
            project,
            _youtube_action(
                sheet_id=sheet_id,
                row_ids=[row_ids[1]],
                output_name="invalid_only",
                idempotency_key="media_ytdlp_download@sha256:all-invalid",
            ),
            project_id=PROJECT_ID,
        )
        assert all_invalid.status == "failed"
        assert all_invalid.errors[0].code == "external_rows_failed"
        assert calls == []

        result = run_action_with_confirmation(
            project,
            _youtube_action(
                sheet_id=sheet_id,
                idempotency_key="media_ytdlp_download@sha256:mixed",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "partial", result.errors
        assert result.receipt_id is not None
        assert calls == [YOUTUBE_WATCH]

        run = project.db.execute(
            "SELECT * FROM runs WHERE id=?", (result.run_id,)
        ).fetchone()
        assert run is not None
        assert run["total_rows"] == 2
        assert run["completed_rows"] == 2
        assert run["failed_rows"] == 1

        receipt = _receipt(project, result.receipt_id)
        assert receipt.status == "partial"
        assert (
            next(p for p in receipt.provider_use if p.get("external_api"))[
                "operation_call_count"
            ]
            == 1
        )
        errors = project.db.execute(
            "SELECT row_id,error_code FROM results WHERE run_id=? AND error_code IS NOT NULL",
            (result.run_id,),
        ).fetchall()
        assert [(r["row_id"], r["error_code"]) for r in errors] == [
            (row_ids[1], "invalid_url")
        ]
    finally:
        project.close()


def test_media_download_extra_opts_validation_probe():
    from frisket.actions.media_download import MediaDownloadParams

    assert MediaDownloadParams(
        source="url", extra_opts={"writesubtitles": True}
    ).extra_opts == {"writesubtitles": True}
    with pytest.raises(ValueError):
        MediaDownloadParams(source="url", extra_opts={"exec": "forbidden"})


def test_media_download_extra_opts_reaches_downloader_through_executor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import frisket.ops.ytdlp as youtube_ops
    from frisket.ops.ytdlp import SidecarFile

    project, sheet_id, _row_ids = _seed_youtube_project(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_download(url: str, **kwargs: Any) -> DownloadedMedia:
        calls.append({"url": url, **kwargs})
        return DownloadedMedia(
            data=f"fake media bytes {len(calls)}".encode("utf-8"),
            mime="audio/wav",
            filename=f"clip-{len(calls)}.wav",
            duration_seconds=1.25,
            metadata={"title": f"Clip {len(calls)}", "extractor": "Youtube"},
            sidecars=[
                SidecarFile(
                    kind="subtitles",
                    data=(b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nfixture subs\n"),
                    mime="text/vtt",
                    filename=f"clip-{len(calls)}.en.vtt",
                )
            ],
        )

    monkeypatch.setattr(youtube_ops, "download_media", fake_download)
    try:
        action = _youtube_action(
            sheet_id=sheet_id,
            extra_opts={"writesubtitles": True, "retries": 5},
            idempotency_key="media_ytdlp_download@sha256:extra-opts-executor",
        )
        result = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        assert len(calls) == 2
        # The recipe backend-expands a writesubtitles-only request to also ask
        # for automatic captions (writeautomaticsub), so both flags reach the
        # downloader even though only writesubtitles was in the action's
        # extra_opts.
        assert calls[0]["extra_opts"] == {
            "writesubtitles": True,
            "retries": 5,
            "writeautomaticsub": True,
        }
        assert calls[1]["extra_opts"] == {
            "writesubtitles": True,
            "retries": 5,
            "writeautomaticsub": True,
        }
    finally:
        project.close()


def test_media_download_receipt_evidence_records_extra_opts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import frisket.ops.ytdlp as youtube_ops
    from frisket.ops.ytdlp import SidecarFile

    project, sheet_id, _row_ids = _seed_youtube_project(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_download(url: str, **kwargs: Any) -> DownloadedMedia:
        calls.append({"url": url, **kwargs})
        extra_opts = kwargs.get("extra_opts") or {}
        sidecars = []
        if extra_opts.get("writesubtitles") or extra_opts.get("writeautomaticsub"):
            sidecars.append(
                SidecarFile(
                    kind="subtitles",
                    data=(b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nfixture subs\n"),
                    mime="text/vtt",
                    filename=f"clip-{len(calls)}.en.vtt",
                )
            )
        return DownloadedMedia(
            data=f"fake media bytes {len(calls)}".encode("utf-8"),
            mime="audio/wav",
            filename=f"clip-{len(calls)}.wav",
            duration_seconds=1.25,
            metadata={"title": f"Clip {len(calls)}", "extractor": "Youtube"},
            sidecars=sidecars,
        )

    monkeypatch.setattr(youtube_ops, "download_media", fake_download)

    def _engine_ref(receipt):
        calls = [
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "row_file_call"
        ]
        assert len(calls) == 2
        return calls[0]

    try:
        action = _youtube_action(
            sheet_id=sheet_id,
            extra_opts={"writesubtitles": True},
            idempotency_key="media_ytdlp_download@sha256:extra-opts-evidence",
        )
        result = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors

        receipt = _receipt(project, result.receipt_id)
        assert _engine_ref(receipt)["extra_opts"] == {
            "writesubtitles": True,
            "writeautomaticsub": True,
        }

        replay = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
        )
        assert replay.status == "completed"
        assert replay.receipt_id == result.receipt_id
        replay_receipt = _receipt(project, replay.receipt_id)
        assert _engine_ref(replay_receipt)["extra_opts"] == {
            "writesubtitles": True,
            "writeautomaticsub": True,
        }
    finally:
        project.close()

    no_opts_project, no_opts_sheet_id, _ = _seed_youtube_project(
        tmp_path / "no-extra-opts"
    )
    try:
        no_opts_action = _youtube_action(
            sheet_id=no_opts_sheet_id,
            idempotency_key="media_ytdlp_download@sha256:no-extra-opts",
        )
        no_opts_result = run_action_with_confirmation(
            no_opts_project,
            no_opts_action,
            project_id=PROJECT_ID,
        )
        assert no_opts_result.status == "completed", no_opts_result.errors
        no_opts_receipt = _receipt(no_opts_project, no_opts_result.receipt_id)
        no_opts_engine_ref = _engine_ref(no_opts_receipt)
        # Runs without extra_opts keep the same evidence shape as unset
        # format_selector: the key is present, valued None.
        assert no_opts_engine_ref["extra_opts"] is None
        assert no_opts_engine_ref["format_selector"] is None
    finally:
        no_opts_project.close()


def test_media_download_sidecar_columns_and_receipts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import frisket.ops.ytdlp as youtube_ops
    from frisket.ops.ytdlp import SidecarFile

    project, sheet_id, row_ids = _seed_youtube_project(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_download(url: str, **kwargs: Any) -> DownloadedMedia:
        calls.append({"url": url, **kwargs})
        # The second row's video has no subtitles/thumbnail available (a
        # toggle-on-but-nothing-produced row): the run must still complete,
        # leaving those cells empty for that row.
        sidecars = (
            [
                SidecarFile(
                    kind="thumbnail",
                    data=_png(),
                    mime="image/png",
                    filename=f"clip-{len(calls)}.png",
                ),
                SidecarFile(
                    kind="subtitles",
                    data=(
                        f"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nsubs {len(calls)}\n"
                    ).encode("utf-8"),
                    mime="text/vtt",
                    filename=f"clip-{len(calls)}.en.vtt",
                ),
            ]
            if len(calls) == 1
            else []
        )
        return DownloadedMedia(
            data=f"fake media bytes {len(calls)}".encode("utf-8"),
            mime="audio/wav",
            filename=f"clip-{len(calls)}.wav",
            duration_seconds=1.25,
            metadata={"title": f"Clip {len(calls)}", "extractor": "Youtube"},
            sidecars=sidecars,
        )

    monkeypatch.setattr(youtube_ops, "download_media", fake_download)

    try:
        action = _youtube_action(
            sheet_id=sheet_id,
            extra_opts={"writethumbnail": True, "writesubtitles": True},
            idempotency_key="media_ytdlp_download@sha256:sidecars-on",
        )
        result = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
        )
        # Row 2's subtitles toggle was on but yt-dlp produced no track for it,
        # so that cell carries a per-row note (not silence) and the run goes
        # partial rather than completed — see the results-table assertion.
        assert result.status == "partial", result.errors
        assert len(calls) == 2

        output_names = [output.name for output in result.outputs]
        assert output_names == [
            "media",
            "media_thumbnail",
            "media_subtitles",
            "media_subtitles_text",
        ]

        columns = _columns(project, sheet_id)
        assert columns["media_thumbnail"]["type"] == "image"
        assert columns["media_subtitles"]["type"] == "file"
        assert columns["media_subtitles_text"]["type"] == "text"
        assert "media_info" not in columns

        thumbnail_values = project.get_values(
            sheet_id, int(columns["media_thumbnail"]["id"])
        )
        subtitle_values = project.get_values(
            sheet_id, int(columns["media_subtitles"]["id"])
        )
        subtitle_text_values = project.get_values(
            sheet_id, int(columns["media_subtitles_text"]["id"])
        )
        # Row 1 got a thumbnail/subtitles blob; row 2's toggle was on but
        # yt-dlp produced nothing for it. The thumbnail toggle has no
        # requested-but-absent note; the subtitles toggle does — its cells
        # stay empty AND carry a per-row note instead of silence.
        assert thumbnail_values[row_ids[0]]["blob"]
        assert thumbnail_values[row_ids[0]]["mime"] == "image/png"
        assert subtitle_values[row_ids[0]]["blob"]
        assert subtitle_values[row_ids[0]]["mime"] == "text/vtt"
        assert subtitle_text_values[row_ids[0]] == "subs 1"
        assert thumbnail_values[row_ids[1]] is None
        assert subtitle_values[row_ids[1]] is None
        assert subtitle_text_values[row_ids[1]] is None

        subtitle_error_row = project.db.execute(
            "SELECT error, outcome FROM results WHERE row_id=? AND column_id=?",
            (row_ids[1], int(columns["media_subtitles"]["id"])),
        ).fetchone()
        assert subtitle_error_row is not None
        assert subtitle_error_row["outcome"] == "model_error"
        assert "no manual or automatic captions" in (subtitle_error_row["error"] or "")

        receipt = _receipt(project, result.receipt_id)
        refs = [
            item.ref for item in receipt.inputs + receipt.outputs + receipt.evidence
        ]
        output_col_refs = {
            ref["name"]: ref for ref in refs if ref["kind"] == "map_result_column"
        }
        assert set(output_col_refs) == {
            "media",
            "media_thumbnail",
            "media_subtitles",
            "media_subtitles_text",
        }

        blob_refs = [ref for ref in refs if ref["kind"] == "row_file_output"]
        # Row 1: media + thumbnail + subtitles = 3 blob refs. Row 2: media
        # only (its sidecars were never produced) = 1 blob ref.
        assert len(blob_refs) == 4

        before_replay_calls = len(calls)
        replay = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
        )
        assert replay.status == "partial"
        assert replay.receipt_id == result.receipt_id
        assert len(calls) == before_replay_calls
    finally:
        project.close()

    # Toggles off -> no sidecar columns are created at all.
    off_project, off_sheet_id, _off_row_ids = _seed_youtube_project(
        tmp_path / "sidecars-off"
    )

    def fake_download_no_toggles(url: str, **kwargs: Any) -> DownloadedMedia:
        return DownloadedMedia(
            data=b"fake media bytes",
            mime="audio/wav",
            filename="clip.wav",
            duration_seconds=1.25,
            metadata={"title": "Clip", "extractor": "Youtube"},
        )

    monkeypatch.setattr(youtube_ops, "download_media", fake_download_no_toggles)
    try:
        off_action = _youtube_action(
            sheet_id=off_sheet_id,
            idempotency_key="media_ytdlp_download@sha256:sidecars-off",
        )
        off_result = run_action_with_confirmation(
            off_project,
            off_action,
            project_id=PROJECT_ID,
        )
        assert off_result.status == "completed", off_result.errors
        assert [output.name for output in off_result.outputs] == ["media"]
        off_columns = _columns(off_project, off_sheet_id)
        assert "media_thumbnail" not in off_columns
        assert "media_subtitles" not in off_columns
        assert "media_subtitles_text" not in off_columns
        assert "media_info" not in off_columns
    finally:
        off_project.close()


def test_media_download_subtitles_text_column(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The subtitles sidecar also produces a plain-text column, gated by the
    same writesubtitles/writeautomaticsub toggle as the raw file column, so
    YouTube captions are a free alternative to media.transcribe."""
    import frisket.ops.ytdlp as youtube_ops
    from frisket.ops.ytdlp import SidecarFile

    auto_caption_vtt = (
        "WEBVTT\n"
        "Kind: captions\n"
        "Language: en\n"
        "\n"
        "00:00:00.000 --> 00:00:02.000 align:start position:0%\n"
        "Hello<00:00:01.000><c> there</c>\n"
        "\n"
        "00:00:02.000 --> 00:00:04.000 align:start position:0%\n"
        "Hello there\n"
        "this is a caption\n"
    ).encode("utf-8")

    project, sheet_id, row_ids = _seed_youtube_project(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_download(url: str, **kwargs: Any) -> DownloadedMedia:
        calls.append({"url": url, **kwargs})
        return DownloadedMedia(
            data=f"fake media bytes {len(calls)}".encode("utf-8"),
            mime="audio/wav",
            filename=f"clip-{len(calls)}.wav",
            duration_seconds=1.25,
            metadata={"title": f"Clip {len(calls)}", "extractor": "Youtube"},
            sidecars=[
                SidecarFile(
                    kind="subtitles",
                    data=auto_caption_vtt,
                    mime="text/vtt",
                    filename=f"clip-{len(calls)}.en.vtt",
                ),
            ],
        )

    monkeypatch.setattr(youtube_ops, "download_media", fake_download)

    try:
        action = _youtube_action(
            sheet_id=sheet_id,
            extra_opts={"writesubtitles": True},
            idempotency_key="media_ytdlp_download@sha256:subtitles-text-on",
        )
        result = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors

        output_names = [output.name for output in result.outputs]
        assert output_names == ["media", "media_subtitles", "media_subtitles_text"]

        columns = _columns(project, sheet_id)
        assert columns["media_subtitles_text"]["type"] == "text"

        text_values = project.get_values(
            sheet_id, int(columns["media_subtitles_text"]["id"])
        )
        # The rolling auto-caption duplicate ("Hello there") collapses via
        # consecutive-only dedupe; the raw file column is untouched.
        assert text_values[row_ids[0]] == "Hello there\nthis is a caption"
        assert text_values[row_ids[1]] == "Hello there\nthis is a caption"

        subtitle_values = project.get_values(
            sheet_id, int(columns["media_subtitles"]["id"])
        )
        assert subtitle_values[row_ids[0]]["blob"]
        assert subtitle_values[row_ids[0]]["mime"] == "text/vtt"

        receipt = _receipt(project, result.receipt_id)
        refs = [
            item.ref for item in receipt.inputs + receipt.outputs + receipt.evidence
        ]
        output_col_refs = {
            ref["name"]: ref for ref in refs if ref["kind"] == "map_result_column"
        }
        assert set(output_col_refs) == {
            "media",
            "media_subtitles",
            "media_subtitles_text",
        }
    finally:
        project.close()

    # Toggles off -> the text column does not appear at all, same as the raw
    # subtitles file column.
    off_project, off_sheet_id, _off_row_ids = _seed_youtube_project(
        tmp_path / "subtitles-text-off"
    )

    def fake_download_no_toggles(url: str, **kwargs: Any) -> DownloadedMedia:
        return DownloadedMedia(
            data=b"fake media bytes",
            mime="audio/wav",
            filename="clip.wav",
            duration_seconds=1.25,
            metadata={"title": "Clip", "extractor": "Youtube"},
        )

    monkeypatch.setattr(youtube_ops, "download_media", fake_download_no_toggles)
    try:
        off_action = _youtube_action(
            sheet_id=off_sheet_id,
            idempotency_key="media_ytdlp_download@sha256:subtitles-text-off",
        )
        off_result = run_action_with_confirmation(
            off_project,
            off_action,
            project_id=PROJECT_ID,
        )
        assert off_result.status == "completed", off_result.errors
        assert [output.name for output in off_result.outputs] == ["media"]
        off_columns = _columns(off_project, off_sheet_id)
        assert "media_subtitles" not in off_columns
        assert "media_subtitles_text" not in off_columns
    finally:
        off_project.close()


def test_extra_opts_rejects_unbounded_and_nested_values():
    """Allowed numeric knobs are bounded (no worker-hang DoS) and values must be
    scalars/flat lists — keys-only allowlisting isn't enough."""
    from pydantic import ValidationError

    from frisket.actions.media_download import MediaDownloadParams

    base = dict(source="url")

    # Safe values pass.
    MediaDownloadParams(**base, extra_opts={"writesubtitles": True, "retries": 5})
    MediaDownloadParams(**base, extra_opts={"subtitleslangs": ["en", "es"]})

    for bad in (
        {"sleep_interval": 10_000_000},  # worker-hang
        {"max_sleep_interval": 999},  # over cap
        {"retries": 99999},  # over cap
        {"socket_timeout": 0},  # under min
        {"ratelimit": 1},  # crawl-slow floor
        {"subtitleslangs": {"x": 1}},  # nested, not scalars
        {"format_sort": {"a": "b"}},  # nested value
    ):
        try:
            MediaDownloadParams(**base, extra_opts=bad)
        except ValidationError:
            continue
        raise AssertionError(f"extra_opts should have rejected {bad!r}")
