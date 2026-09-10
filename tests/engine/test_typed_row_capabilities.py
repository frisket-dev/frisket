from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel

import frisket.engine.executor.map_rows_action as map_module
import frisket.engine.executor.media_metadata_read as reader_module
import frisket.ops.media_metadata as metadata
from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    map_rows,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    ColumnRef,
    MediaMetadataReader,
    Row,
    RowResult,
    SheetRows,
)
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.map_rows_action import (
    build_typed_map_rows_plan,
    run_typed_map_rows_action,
)
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import MediaBlobStore, media_cell
from frisket.execution.attempt_authority import UnroutedOnlyAuthority


class LabelParams(ActionParams):
    asset: ColumnRef[Any]
    reread: bool = False
    behavior: Literal["ok", "fail", "omit"] = "ok"


class LabelOutput(BaseModel):
    label: str | None = None
    inactive: str | None = None


async def label_asset(
    params: LabelParams, row: Row, reader: MediaMetadataReader
) -> RowResult[LabelOutput]:
    envelope = await reader.read(params.asset.read(row), refresh=params.reread)
    if params.behavior == "fail":
        raise RuntimeError("label handler failed after borrowing the reader")
    if params.behavior == "omit":
        return RowResult(output=LabelOutput(inactive="must not escape"))
    return RowResult(
        output=LabelOutput(
            label=None if envelope is None else envelope["normalized"]["filename"],
            inactive="must not escape",
        )
    )


REGISTRY = ActionRegistry(
    [
        ActionNamespace(
            "test",
            actions=[
                action(
                    name="asset_label",
                    title="Asset label",
                    description="Name a blob reference using an admitted reader.",
                    category=ActionCategory.CONVERT,
                    run=map_rows(label_asset, active_outputs=lambda params: ("label",)),
                )
            ],
        )
    ]
)


@pytest.fixture
def source(tmp_path, monkeypatch):
    # Image cache admission requires a successful EXIF adapter, not a retryable
    # missing-dependency envelope. Keep the real parser and cache policy while
    # replacing only the optional local tool transport.
    monkeypatch.setattr(
        metadata, "_resolve_exiftool_path", lambda: Path("/fake/exiftool")
    )
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: None)
    monkeypatch.setattr(metadata, "_tool_version", lambda *_args: "13.59")
    monkeypatch.setattr(
        metadata,
        "_run_bounded",
        lambda *_args, **_kwargs: metadata._CommandResult(
            returncode=0,
            stdout=b'[{"File:FileType":"PNG","File:MIMEType":"image/png"}]',
            stderr=b"",
        ),
    )
    project = Project.create(tmp_path / "row-capabilities.frisket")
    try:
        sheet = project.add_sheet("assets")
        column = project.add_column(sheet, "asset", type="file")
        digest = project.add_blob(
            b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", 13, 7),
            filename="original.png",
            mime="image/png",
        )
        rows = project.add_rows(
            sheet,
            [
                {"asset": media_cell(digest, filename=name, mime="image/png")}
                for name in ("first.png", "second.png")
            ],
            {"asset": column},
        )
        yield project, sheet, rows, digest
    finally:
        project.close()


@pytest.fixture
def admitted_readers(monkeypatch):
    instances = []

    class ObservedReader(reader_module.AdmittedMediaMetadataReader):
        def __init__(self, project, **kwargs):
            super().__init__(project, **kwargs)
            self.preview = kwargs["preview"]
            self.calls = []
            self.close_calls = 0
            instances.append(self)

        async def read(self, cell, *, refresh=False):
            self.calls.append((cell, refresh))
            return await super().read(cell, refresh=refresh)

        async def aclose(self):
            self.close_calls += 1
            await super().aclose()

    monkeypatch.setattr(reader_module, "AdmittedMediaMetadataReader", ObservedReader)
    return instances


def _bound(source, *, behavior="ok", reread=True):
    _, sheet, rows, _ = source
    request = ActionRequest(
        action_id="test.asset_label",
        scope=SheetRows(sheet_id=sheet, row_ids=tuple(rows)),
        params={"asset": "asset", "reread": reread, "behavior": behavior},
        output_names={"label": "Asset label"},
        idempotency_key=f"asset-label-{behavior}-{reread}",
    )
    return BoundTypedActionRequest.bind(REGISTRY.get(request.action_id), request)


def _runner(project, router):
    return MapRunner(project, router, authority=UnroutedOnlyAuthority(project))


@pytest.mark.parametrize("behavior", ["ok", "fail", "omit"])
def test_custom_action_preview_borrows_one_reader_and_closes_on_row_errors(
    source, admitted_readers, behavior
):
    project, sheet, rows, digest = source
    bound = _bound(source, behavior=behavior)
    plan = build_typed_map_rows_plan(project, bound)
    before = [dict(column) for column in project.columns(sheet)]
    preview = asyncio.run(
        _runner(
            project, ModelRouter(cache=None, cache_mode="off", use_env_keys=False)
        ).preview(plan.spec_dict(), program=plan.program)
    )

    assert len(admitted_readers) == 1
    reader = admitted_readers[0]
    assert reader.preview and reader.closed and reader.close_calls == 1
    assert len(reader.calls) == 2
    assert all(
        cell["blob"] == digest and refresh is True for cell, refresh in reader.calls
    )
    assert {cell["filename"] for cell, _ in reader.calls} == {"first.png", "second.png"}
    assert not reader._futures and not reader._completed_templates
    assert MediaBlobStore(project).media_metadata_cache(digest) is None
    assert [dict(column) for column in project.columns(sheet)] == before
    assert set(preview.values) == set(rows)
    assert all(set(cells) == {"Asset label"} for cells in preview.values.values())
    if behavior == "ok":
        assert [preview.values[row]["Asset label"]["value"] for row in rows] == [
            "first.png",
            "second.png",
        ]
    else:
        expected = (
            "handler failed"
            if behavior == "fail"
            else "active output fields are missing"
        )
        assert all(
            expected in cells["Asset label"]["error"]
            for cells in preview.values.values()
        )


def test_custom_action_durable_run_uses_same_capability_and_only_active_publication(
    source, admitted_readers, monkeypatch
):
    project, sheet, rows, digest = source
    publications = []
    original_prepare = map_module._TypedMapRowsProgram._prepare_publication

    def capture(
        self, produced, *, topic_reader=None, output_columns=None, row_files=None
    ):
        publication = original_prepare(
            self,
            produced,
            topic_reader=topic_reader,
            output_columns=output_columns,
            row_files=row_files,
        )
        publications.append(publication)
        return publication

    monkeypatch.setattr(
        map_module._TypedMapRowsProgram, "_prepare_publication", capture
    )
    result = run_typed_map_rows_action(
        project,
        "project-1",
        _bound(source, reread=False),
        ModelRouter(cache=None, cache_mode="off", use_env_keys=False),
        _runner,
    )

    assert result.status == "completed", result.errors
    assert len(admitted_readers) == 1
    reader = admitted_readers[0]
    assert not reader.preview and reader.closed and reader.close_calls == 1
    assert len(reader.calls) == 2
    assert all(
        cell["blob"] == digest and refresh is False for cell, refresh in reader.calls
    )
    cached = MediaBlobStore(project).media_metadata_cache(digest)
    assert cached is not None and metadata.media_metadata_cache_compatible(cached)
    assert [column["name"] for column in project.columns(sheet)] == [
        "asset",
        "Asset label",
    ]
    assert len(publications) == len(rows)
    assert all(
        set(publication.cells) == {"Asset label"} for publication in publications
    )
    assert sorted(
        publication.trace_data["output"]["label"] for publication in publications
    ) == ["first.png", "second.png"]
    assert all(
        set(publication.trace_data["output"]) == {"label"}
        for publication in publications
    )
