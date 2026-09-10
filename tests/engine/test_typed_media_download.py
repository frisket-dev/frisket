import asyncio
from dataclasses import replace
import threading

import pytest

from frisket.actions.core import RegisteredAction, map_rows
from frisket.actions.media_download import MediaDownloadParams
from frisket.actions.media_download_types import DownloadedFiles, MediaDownloader
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.media_download import AdmittedMediaDownloader
from frisket.engine.executor.row_file_stage import RowFileStager
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.ops.ytdlp import DownloadedMedia, SidecarFile


class RenamedParams(ActionParams):
    address: ColumnRef[str]


async def derived_download(
    params: RenamedParams, row: Row, downloader: MediaDownloader
) -> RowResult[DownloadedFiles]:
    return RowResult(
        output=await downloader.download(params.address.read(row) + "?derived=1")
    )


async def discard_download(
    params: MediaDownloadParams, row: Row, downloader: MediaDownloader
) -> RowResult[DownloadedFiles]:
    await downloader.download(params.source.read(row))
    return RowResult(output=DownloadedFiles(video=None))


@pytest.mark.parametrize("discard", [False, True])
def test_unaccepted_download_never_backfills_channels(tmp_path, monkeypatch, discard):
    if discard:
        original = ACTION_REGISTRY.get("media.ytdlp_download")
        custom = RegisteredAction(
            original.action_id,
            replace(
                original.definition,
                run=map_rows(discard_download, active_outputs=lambda _p: ("video",)),
                _example_params=(),
            ),
        )
        monkeypatch.setattr(
            ACTION_REGISTRY,
            "_actions",
            {**ACTION_REGISTRY._actions, original.action_id: custom},
        )
    monkeypatch.setattr(
        "frisket.ops.ytdlp.download_media",
        lambda *_a, **_k: DownloadedMedia(
            b"video",
            "video/mp4",
            "a.mp4",
            metadata={"provider": "youtube", "channel_id": "UC-must-not-publish"},
        ),
    )
    project = Project.create(tmp_path / "unaccepted.frisket")
    try:
        sheet = project.add_sheet("Data")
        columns = {
            name: project.add_column(sheet, name, type="text")
            for name in ("url", "channel_id")
        }
        project.add_rows(sheet, [{"url": "https://example.test/media"}], columns)
        body = {
            "action_id": "media.ytdlp_download",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "url"},
            "idempotency_key": "unaccepted",
        }
        router = ModelRouter()
        quote = run_action_spec(project, body, project_id="unaccepted", router=router)
        assert quote.status == "needs_confirmation", quote.errors
        body["confirmation"] = quote.errors[0].details["promise_set_hash"]
        if not discard:
            from frisket.engine.executor import actions
            from frisket.engine.store.runs import RunResultStore

            cancelled = False
            original_complete = RunResultStore.complete_row_effect_checkpoint
            original_factory = actions._default_map_runner_factory

            def complete(self, *args, **kwargs):
                nonlocal cancelled
                returned = original_complete(self, *args, **kwargs)
                cancelled = True
                return returned

            def factory(*args, **kwargs):
                runner = original_factory(*args, **kwargs)
                runner.should_cancel = lambda _run_id: cancelled
                return runner

            monkeypatch.setattr(
                RunResultStore, "complete_row_effect_checkpoint", complete
            )
            monkeypatch.setattr(actions, "_default_map_runner_factory", factory)
        result = run_action_spec(project, body, project_id="unaccepted", router=router)
        assert result.status == ("completed" if discard else "cancelled"), (
            result.model_dump(mode="json")
        )
        assert list(project.get_values(sheet, columns["channel_id"]).values()) == [None]
        assert project.db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 0
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert not any(e.ref.get("kind") == "row_file_output" for e in receipt.evidence)
        assert any(e.ref.get("kind") == "row_file_call" for e in receipt.evidence)
    finally:
        project.close()


@pytest.mark.parametrize(
    ("proxy", "override", "expected"),
    [
        (None, None, None),
        ("https://proxy.test", None, 1),
        ("https://proxy.test", "3", 3),
        ("https://proxy.test", "bad", 1),
        ("https://proxy.test", "0", 1),
    ],
)
def test_proxy_concurrency_policy(monkeypatch, proxy, override, expected):
    from frisket.engine.executor.media_download import download_max_concurrency

    monkeypatch.setattr("frisket.ops.media_proxy.resolve_media_proxy", lambda: proxy)
    monkeypatch.delenv("FRISKET_MEDIA_PROXY_MAX_CONCURRENCY", raising=False)
    if override is not None:
        monkeypatch.setenv("FRISKET_MEDIA_PROXY_MAX_CONCURRENCY", override)
    assert download_max_concurrency() == expected


@pytest.mark.parametrize("media_type", ["audio", "video"])
@pytest.mark.parametrize("sidecars", [False, True])
def test_typed_download_publication_and_replay(
    tmp_path, monkeypatch, media_type, sidecars
):
    calls = []

    def download(url, **kwargs):
        calls.append((url, kwargs))
        return DownloadedMedia(
            b"media bytes",
            media_type + "/mp4",
            "clip.mp4",
            12,
            metadata={
                "provider": "youtube",
                "channel_id": "UC-actual",
                "channel_title": "Actual",
            },
            sidecars=[
                SidecarFile(
                    "subtitles",
                    b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n",
                    "text/vtt",
                    "clip.en.vtt",
                ),
                SidecarFile(
                    "info_json",
                    b'{"title":"clip"}',
                    "application/json",
                    "clip.info.json",
                ),
            ]
            if sidecars
            else [],
        )

    monkeypatch.setattr("frisket.ops.ytdlp.download_media", download)
    project = Project.create(tmp_path / "download.frisket")
    try:
        sheet = project.add_sheet("Data")
        cols = {
            name: project.add_column(sheet, name, type="text")
            for name in ("url", "channel_id", "channel_title")
        }
        project.add_rows(
            sheet,
            [{"url": "https://example.test/video", "channel_title": "Manual"}],
            cols,
        )
        body = {
            "action_id": "media.ytdlp_download",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {
                "source": "url",
                "media_type": media_type,
                "extra_opts": {
                    "writesubtitles": True,
                    "writeinfojson": True,
                    "writethumbnail": True,
                },
            },
            "output_names": {media_type: "media"},
            "idempotency_key": "download-proof",
        }
        router = ModelRouter()
        quote = run_action_spec(project, body, project_id="download", router=router)
        assert quote.status == "needs_confirmation", quote.errors
        assert not calls
        body["confirmation"] = quote.errors[0].details["promise_set_hash"]
        result = run_action_spec(project, body, project_id="download", router=router)
        expected_status = "completed" if sidecars else "partial"
        assert result.status == expected_status, result.model_dump(mode="json")
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        occurrences = [
            e.ref for e in receipt.evidence if e.ref.get("kind") == "row_file_output"
        ]
        main = next(e for e in occurrences if e["output_key"] == media_type)
        assert main["facts"]["url"] == calls[0][0]
        assert len(occurrences) == (3 if sidecars else 1)
        assert (
            next(c for c in project.columns(sheet) if c["name"] == "media")["type"]
            == media_type
        )
        assert list(project.get_values(sheet, cols["channel_id"]).values()) == [
            "UC-actual"
        ]
        assert list(project.get_values(sheet, cols["channel_title"]).values()) == [
            "Manual"
        ]
        assert (
            run_action_spec(project, body, project_id="download", router=router).status
            == expected_status
        )
        assert len(calls) == 1
        project.undo()
        assert list(project.get_values(sheet, cols["channel_id"]).values()) == [None]
        assert list(project.get_values(sheet, cols["channel_title"]).values()) == [
            "Manual"
        ]
    finally:
        project.close()


def test_downloader_is_reusable_with_renamed_params_and_derived_url(
    tmp_path, monkeypatch
):
    original = ACTION_REGISTRY.get("media.ytdlp_download")
    custom = RegisteredAction(
        original.action_id,
        replace(
            original.definition,
            run=map_rows(derived_download, active_outputs=lambda _p: ("video",)),
            _example_params=(),
        ),
    )
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, original.action_id: custom},
    )
    calls = []
    monkeypatch.setattr(
        "frisket.ops.ytdlp.download_media",
        lambda url, **_: (
            calls.append(url) or DownloadedMedia(b"video", "video/mp4", "a.mp4")
        ),
    )
    project = Project.create(tmp_path / "derived.frisket")
    try:
        sheet = project.add_sheet("Data")
        col = project.add_column(sheet, "url", type="text")
        project.add_rows(sheet, [{"url": "https://example.test/media"}], {"url": col})
        body = {
            "action_id": original.action_id,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"address": "url"},
            "idempotency_key": "derived",
        }
        router = ModelRouter()
        quote = run_action_spec(project, body, project_id="derived", router=router)
        assert quote.status == "needs_confirmation", quote.errors
        body["confirmation"] = quote.errors[0].details["promise_set_hash"]
        result = run_action_spec(project, body, project_id="derived", router=router)
        assert result.status == "completed", result.model_dump(mode="json")
        assert calls == ["https://example.test/media?derived=1"]
    finally:
        project.close()


@pytest.mark.parametrize(
    "options", [{"exec": "bad"}, {"retries": 999}, {"socket_timeout": 0}]
)
def test_options_rejected_before_execution(options):
    with pytest.raises(ValueError):
        MediaDownloadParams(source="url", extra_opts=options)


def test_cannot_forge_download_with_serialized_blob():
    with pytest.raises(ValueError):
        DownloadedFiles(
            video={
                "blob": "sha256:" + "a" * 64,
                "mime": "video/mp4",
                "filename": "a.mp4",
            }
        )


def test_preview_requires_consent_before_downloading(tmp_path, monkeypatch):
    from frisket.server.workspace import Workspace
    from frisket.server.services.action_preview_runs import ActionPreviewRunService

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("Preview", project_id="preview")
    project = workspace.get("preview")
    sheet = project.add_sheet("Data")
    col = project.add_column(sheet, "url", type="text")
    project.add_rows(sheet, [{"url": "https://example.test/media"}], {"url": col})
    service = ActionPreviewRunService(workspace)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("preview must not download or start a job")

    monkeypatch.setattr("frisket.ops.ytdlp.download_media", unexpected)
    monkeypatch.setattr(service._registry, "start", unexpected)
    response = service.start_preview(
        "preview",
        {
            "action_id": "media.ytdlp_download",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "url"},
            "idempotency_key": "preview",
        },
    )
    assert response.status_code == 402, response.payload
    assert response.payload["error"]["code"] == "cost_gate"
    assert response.payload["error"]["needs_confirmation"] is True
    assert response.payload["error"]["details"]["promise_set_hash"]


def test_channel_backfill_rechecks_current_source(tmp_path):
    from frisket.engine.executor.media_download import publish_download_channel_facts

    project = Project.create(tmp_path / "changed.frisket")
    try:
        sheet = project.add_sheet("Data")
        cols = {
            name: project.add_column(sheet, name, type="text")
            for name in ("url", "channel_id")
        }
        row_id = project.add_rows(sheet, [{"url": "https://example.test/old"}], cols)[0]
        values, refs = project.get_values_with_refs(
            sheet, cols["url"], row_ids=[row_id]
        )
        captured = {
            "column_id": cols["url"],
            "value": values[row_id],
            "value_ref": refs[row_id],
        }
        project.apply_edits(
            [
                {
                    "row_id": row_id,
                    "column_id": cols["url"],
                    "value": "https://example.test/new",
                }
            ]
        )
        publish_download_channel_facts(
            project,
            {
                "facts": {
                    "row_id": row_id,
                    "sheet_id": sheet,
                    "sources": {"url": captured},
                    "channel": {"channel_id": "UC-old"},
                }
            },
            run_id=1,
            op_id=1,
            row_id=row_id,
        )
        assert list(project.get_values(sheet, cols["channel_id"]).values()) == [None]
    finally:
        project.close()


def test_cancelled_worker_settles_before_scope_closes(tmp_path, monkeypatch):
    started, stopped = threading.Event(), threading.Event()

    def download(_url, *, should_cancel, **_):
        started.set()
        while not should_cancel():
            stopped.wait(0.005)
        stopped.set()
        raise RuntimeError("cancelled child")

    monkeypatch.setattr("frisket.ops.ytdlp.download_media", download)
    project = Project.create(tmp_path / "cancel.frisket")
    stager = RowFileStager(project)
    owner = AdmittedMediaDownloader(project, stager)

    async def run():
        task = asyncio.create_task(
            owner.bind_row({}, sheet_id="s", row_id=1, sources={}).download(
                "https://example.test/v"
            )
        )
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
        assert not owner._tasks
        await owner.aclose()

    try:
        asyncio.run(run())
    finally:
        stager.close()
        project.close()
