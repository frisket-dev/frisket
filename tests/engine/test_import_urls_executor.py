from __future__ import annotations

from pathlib import Path
from typing import Any
from contextlib import closing
import json

import pytest

from frisket.engine.executor import ExecutorDeps, UrlImportLimits, run_action_spec
from frisket.ops import url_import as import_family
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.executor import import_blob_stage, table_action


@pytest.fixture(autouse=True)
def stub_probe(monkeypatch):
    monkeypatch.setattr(
        import_blob_stage,
        "probe_for_ingest",
        lambda path, **kwargs: {
            "kind": "file",
            "size_bytes": Path(path).stat().st_size,
        },
    )


def _action(urls: list[str], *, key: str) -> dict[str, Any]:
    return {
        "action_id": "import.urls",
        "scope": {"kind": "project"},
        "sheet_name": key,
        "params": {"urls": urls},
        "idempotency_key": key,
    }


def test_import_urls_action_processes_every_url_beyond_the_legacy_ceiling(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen: list[str] = []
    urls = [f"https://cdn.example/{index}.mp3" for index in range(101)]

    def fake_download(url: str, **kwargs: Any) -> tuple[bytes, str, str, None]:
        seen.append(url)
        return f"ID3{url}".encode(), "audio/mpeg", "episode.mp3", None

    monkeypatch.setattr(import_family, "download_url", fake_download)
    project = Project.create(tmp_path / "urls.frisket", name="Many URLs")
    try:
        result = run_action_spec(
            project,
            _action(urls, key="import_urls@sha256:many"),
            project_id="project-many-urls",
        )

        assert result.status == "completed", result.errors
        assert seen == urls
        rows = next(output for output in result.outputs if output.kind == "rows")
        assert len(rows.row_ids or []) == len(urls)
        assert project.row_count(int(rows.sheet_id)) == len(urls)
    finally:
        project.close()


def test_typed_url_table_names_receipt_and_replay(tmp_path, monkeypatch):
    seen = []

    def download(url):
        seen.append(url)
        return (
            (b"image", "image/png", "image.png", None)
            if url.endswith("png")
            else (b"", None, None, "blocked")
        )

    monkeypatch.setattr(import_family, "download_url", download)
    with closing(Project.create(tmp_path / "typed.frisket")) as project:
        request = _action(
            [" https://example.com/image.png ", "https://example.com/bad"], key="typed"
        )
        request["output_names"] = {
            "media": "Picture",
            "url": "Source",
            "error": "Problem",
        }
        result = run_action_spec(project, request, project_id="p")
        assert result.status == "completed", result.errors
        sheet = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        columns = {column["name"]: column for column in project.columns(sheet)}
        assert set(columns) == {"Source", "Picture", "size", "Problem"}
        assert columns["Picture"]["type"] == "image"
        assert columns["size"]["format"] == "filesize"
        receipt = json.loads(ReceiptStore(project).body_by_id(result.receipt_id))
        assert next(
            item["ref"]
            for item in receipt["inputs"]
            if item["ref"].get("kind") == "url_list"
        ) == {
            "kind": "url_list",
            "url_count": 2,
            "downloaded": 1,
            "failed": 1,
        }
        blob = next(
            item["ref"]
            for item in receipt["evidence"]
            if item["ref"]["kind"] == "imported_blob"
        )
        assert blob["column_id"] == columns["Picture"]["id"]
        assert blob["source_url"] == "https://example.com/image.png"
        assert blob["provider"] == "direct"
        replay = run_action_spec(project, request, project_id="p")
        assert replay.receipt_id == result.receipt_id
        assert len(seen) == 2
        conflict = run_action_spec(
            project, {**request, "output_names": {"media": "Different"}}, project_id="p"
        )
        assert conflict.status == "failed"
        assert "idempotency" in conflict.errors[0].code
        assert len(seen) == 2
        collision = run_action_spec(
            project, {**request, "idempotency_key": "new-key"}, project_id="p"
        )
        assert collision.errors[0].code == "duplicate_sheet_name"
        assert len(seen) == 2
        assert project.row_count(sheet) == 2


@pytest.mark.parametrize("fail_write", [False, True])
def test_url_staging_cleanup_on_bad_output_or_atomic_write_failure(
    tmp_path, monkeypatch, fail_write
):
    stages = []
    original = import_blob_stage.AdmittedImportBlobStager.stage_acquired_url

    def stage(self, *args, **kwargs):
        handle = original(self, *args, **kwargs)
        stages.append(self.publication_plan([(0, "media", handle)]).blobs[0].path)
        return handle

    monkeypatch.setattr(
        import_blob_stage.AdmittedImportBlobStager, "stage_acquired_url", stage
    )
    monkeypatch.setattr(
        import_family,
        "download_url",
        lambda url: (b"bytes", "application/pdf", "doc.pdf", None),
    )
    if fail_write:

        def fail(*args, **kwargs):
            raise RuntimeError("injected transaction failure")

        from frisket.engine.store import import_blobs

        monkeypatch.setattr(import_blobs, "publish_import_blobs", fail)
    with closing(Project.create(tmp_path / "cleanup.frisket")) as project:
        request = _action(["https://example.com/doc"], key="cleanup")
        if not fail_write:
            request["output_names"] = {"absent": "Invalid"}
        result = run_action_spec(project, request, project_id="p")
        assert result.status == "failed"
        assert stages and all(not path.exists() for path in stages)
        for table in ("sheets", "rows", "cells", "blobs", "ops", "receipts"):
            assert (
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )


def test_all_failed_urls_keep_order_and_a_file_column(tmp_path):
    with closing(Project.create(tmp_path / "failed.frisket")) as project:
        result = run_action_spec(
            project, _action(["first", "second"], key="invalid"), project_id="p"
        )
        assert result.status == "completed", result.errors
        sheet = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        assert project.row_count(sheet) == 2
        assert (
            next(
                column for column in project.columns(sheet) if column["name"] == "media"
            )["type"]
            == "file"
        )
        assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0


def test_url_import_preview_refuses_without_acquisition(tmp_path, monkeypatch):
    from frisket.engine.executor import resolve_map_preview

    def forbidden(*args, **kwargs):
        pytest.fail("preview must not acquire URLs")

    monkeypatch.setattr(import_family, "download_url", forbidden)
    monkeypatch.setattr(import_family.ytdlp, "download_media", forbidden)
    with closing(Project.create(tmp_path / "preview.frisket")) as project:
        result = resolve_map_preview(
            project, _action(["https://example.com/doc"], key="preview")
        )
        assert result.code == "unsupported_action_kind"
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0


def test_mixed_media_remains_file_with_ytdlp_acquisition_metadata(
    tmp_path, monkeypatch
):
    from frisket.ops.ytdlp import DownloadedMedia
    from frisket.engine.store.media_blobs import MediaBlobStore

    monkeypatch.setattr(
        import_family,
        "download_url",
        lambda url: (b"pdf", "application/pdf", "doc.pdf", None),
    )
    monkeypatch.setattr(
        import_family.ytdlp,
        "download_media",
        lambda url: DownloadedMedia(
            b"video",
            "video/mp4",
            "movie.mp4",
            duration_seconds=7,
            metadata={"title": "Video title"},
        ),
    )
    with closing(Project.create(tmp_path / "mixed.frisket")) as project:
        result = run_action_spec(
            project,
            _action(
                [
                    "https://example.com/doc",
                    "https://www.youtube.com/watch?v=abc12345678",
                ],
                key="mixed",
            ),
            project_id="p",
        )
        assert result.status == "completed", result.errors
        sheet = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        columns = {column["name"]: column for column in project.columns(sheet)}
        assert columns["media"]["type"] == "file"
        assert "error" not in columns
        values = list(project.get_values(sheet, columns["media"]["id"]).values())
        assert [value["mime"] for value in values] == ["application/pdf", "video/mp4"]
        assert MediaBlobStore(project).acquisition_metadata(values[1]["blob"]) == {
            "title": "Video title",
            "duration_seconds": 7,
        }


@pytest.mark.parametrize("split_calls", [False, True])
def test_custom_params_capability_uses_actual_arguments_and_quota(
    tmp_path, monkeypatch, split_calls
):
    from frisket.actions.core import (
        ActionCategory,
        ActionNamespace,
        ActionRegistry,
        action,
        create_sheet,
    )
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionParams, ActionRequest, DynamicTableResult
    from frisket.actions.url_import_types import UrlImporter

    class RenamedParams(ActionParams):
        addresses: list[str]

    def produce(params, reader):
        if split_calls:
            first = reader.read(params.addresses)
            second = reader.read(["https://example.com/derived"])
            return DynamicTableResult(
                schema=first.schema, rows=[*first.rows, *second.rows]
            )
        return reader.read([*params.addresses, "https://example.com/derived"])

    # This function's concrete annotations deliberately use unrelated Params.
    produce.__annotations__ = {
        "params": RenamedParams,
        "reader": UrlImporter,
        "return": DynamicTableResult,
    }
    registered = ActionRegistry(
        (
            ActionNamespace(
                "custom",
                actions=(
                    action(
                        name="urls",
                        title="URLs",
                        description="Actual arguments",
                        category=ActionCategory.CONVERT,
                        run=create_sheet(produce),
                    ),
                ),
            ),
        )
    ).get("custom.urls")
    seen = []

    def download(url):
        seen.append(url)
        return b"pdf", "application/pdf", "doc.pdf", None

    monkeypatch.setattr(import_family, "download_url", download)
    with closing(Project.create(tmp_path / "custom.frisket")) as project:
        request = ActionRequest(
            action_id="custom.urls",
            scope={"kind": "project"},
            sheet_name="Custom",
            params={"addresses": ["https://example.com/request"]},
            output_names={"media": "Document"},
            idempotency_key="custom",
        )
        bound = BoundTypedActionRequest.bind(registered, request)
        refused = table_action.run_typed_create_sheet_action(
            project,
            "p",
            bound,
            deps=ExecutorDeps(url_import_limits=UrlImportLimits(max_urls=1)),
        )
        assert refused.errors[0].code == "url_limit_exceeded"
        assert seen == (["https://example.com/request"] if split_calls else [])
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
        assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
        seen.clear()
        result = table_action.run_typed_create_sheet_action(
            project,
            "p",
            bound,
            deps=ExecutorDeps(url_import_limits=UrlImportLimits(max_urls=2)),
        )
        assert result.status == "completed", result.errors
        assert seen == ["https://example.com/request", "https://example.com/derived"]
        receipt = json.loads(ReceiptStore(project).body_by_id(result.receipt_id))
        assert (
            sum(
                item["ref"]["downloaded"]
                for item in receipt["inputs"]
                if item["ref"].get("kind") == "url_list"
            )
            == 2
        )
        assert registered.catalog_entry()["required_capabilities"] == [
            "project:write",
            "external:media_download",
        ]


def test_import_urls_action_uses_the_injected_limit_before_network_or_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen: list[str] = []

    def fake_download(url: str, **kwargs: Any) -> tuple[bytes, str, str, None]:
        seen.append(url)
        return b"ID3fixture", "audio/mpeg", "episode.mp3", None

    monkeypatch.setattr(import_family, "download_url", fake_download)
    project = Project.create(tmp_path / "bounded.frisket", name="Bounded URLs")
    try:
        exact = ["https://cdn.example/one.mp3", "https://cdn.example/two.mp3"]
        accepted = run_action_spec(
            project,
            _action(exact, key="import_urls@sha256:exact-bound"),
            project_id="project-bounded-urls",
            deps=ExecutorDeps(url_import_limits=UrlImportLimits(max_urls=2)),
        )
        assert accepted.status == "completed", accepted.errors
        assert seen == exact
        before = {
            table: int(
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in ("sheets", "rows", "cells", "blobs", "ops", "receipts")
        }

        refused = run_action_spec(
            project,
            _action(
                [
                    *exact,
                    "https://cdn.example/three.mp3",
                ],
                key="import_urls@sha256:over-bound",
            ),
            project_id="project-bounded-urls",
            deps=ExecutorDeps(url_import_limits=UrlImportLimits(max_urls=2)),
        )

        assert refused.status == "failed"
        assert refused.errors[0].code == "url_limit_exceeded"
        assert "2" in refused.errors[0].message
        assert seen == exact
        after = {
            table: int(
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in ("sheets", "rows", "cells", "blobs", "ops", "receipts")
        }
        assert after == before
    finally:
        project.close()
