"""Actual-argument media confinement and invocation-owned image staging."""

import asyncio
import io
import json
from contextlib import asynccontextmanager, closing
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from frisket.actions.row_media import ExtractFacesParams, VideoFramesParams
from frisket.actions.row_media_types import Frame, FrameCount, FrameInterval
from frisket.actions.types import ColumnRef, Row, RowError, StagedImage
from frisket.engine.executor import row_media_read
from frisket.engine.executor.row_file_stage import RowFileStager
from frisket.engine.executor.row_media_read import (
    AdmittedFaceExtractor,
    AdmittedFrameExtractor,
)
from frisket.engine.sandbox.shim import SandboxResult
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import media_cell


def jpeg_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (32, 24), (80, 100, 120)).save(stream, format="JPEG")
    return stream.getvalue()


@asynccontextmanager
async def bound_media(
    project, *, kind="video", payload=None, cancelled=None, limits=None, preview=False
):
    sheet = project.add_sheet("Media")
    column = project.add_column(sheet, "renamed", type=kind)
    digest = project.add_blob(
        payload or jpeg_bytes(), filename="source.jpg", mime="image/jpeg"
    )
    value = media_cell(digest, mime="image/jpeg", filename="source.jpg")
    row_id = project.add_rows(sheet, [{"renamed": value}], {"renamed": column})[0]
    row = Row({"renamed": value})
    stager = RowFileStager(project, preview=preview)
    cls = AdmittedFrameExtractor if kind == "video" else AdmittedFaceExtractor
    owner = cls(project, stager, cancelled=cancelled, execution_limits=limits)
    bound = owner.bind_row(
        row,
        sheet_id=sheet,
        row_id=row_id,
        sources={"renamed": {"column_id": column, "value": value}},
    )
    try:
        yield owner, bound, row, stager
    finally:
        await owner.aclose()
        stager.close()


@pytest.fixture
def project(tmp_path):
    with closing(Project.create(tmp_path / "media.frisket")) as project:
        yield project


def fake_media(monkeypatch):
    calls = []

    async def sandbox(command, **kwargs):
        calls.append((command, kwargs))
        if Path(command[0]).name == "ffprobe":
            return SandboxResult(0, json.dumps({"format": {"duration": "12.0"}}), "")
        if Path(command[0]).name == "ffmpeg":
            Path(command[-1]).write_bytes(jpeg_bytes())
            return SandboxResult(0, "", "")
        payload = json.loads(kwargs["stdin_data"])
        crop = Path(payload["out_dir"]) / "face0.jpg"
        crop.write_bytes(jpeg_bytes())
        return SandboxResult(
            0,
            json.dumps(
                {"faces": [{"x": 4, "y": 8, "w": 20, "h": 16, "crop": str(crop)}]}
            ),
            "",
        )

    monkeypatch.setattr(row_media_read, "run_sandboxed", sandbox)
    monkeypatch.setattr(row_media_read, "_ffmpeg_binary", lambda name: f"/bin/{name}")
    return calls


@pytest.mark.parametrize(
    "sampling,timestamps",
    [(FrameCount(count=2), [3.0, 9.0]), (FrameInterval(seconds=5), [0.0, 5.0, 10.0])],
)
def test_actual_sampling_stages_images_without_durable_writes(
    project, monkeypatch, sampling, timestamps
):
    calls = fake_media(monkeypatch)

    async def run():
        async with bound_media(project) as (_, reader, row, stager):
            before = project.db.execute("SELECT count(*) FROM blobs").fetchone()[0]
            frames = await reader.extract(
                row, ColumnRef("renamed"), sampling=sampling, max_dimension=720
            )
            assert [frame.t for frame in frames] == timestamps
            assert all(isinstance(frame.image, StagedImage) for frame in frames)
            assert (
                project.db.execute("SELECT count(*) FROM blobs").fetchone()[0] == before
            )
            for index, frame in enumerate(frames):
                issued = stager._files[frame.image]
                assert issued[3]["timestamp"] == timestamps[index]
                assert issued[3]["source"]["source_column"] == "renamed"
                assert issued[3]["max_dimension"] == 720
            assert (
                len({stager._files[frame.image][2][0].blob_hash for frame in frames})
                == 1
            )
            assert len({id(frame.image) for frame in frames}) == len(frames)

    asyncio.run(run())
    assert len(calls) == len(timestamps) + 1
    assert calls[0][1]["policy"].confine.write == ()
    assert all(call[1]["policy"].confine.exec_binary for call in calls)
    assert all(
        "force_original_aspect_ratio=decrease" in " ".join(call[0])
        for call in calls[1:]
    )


def test_face_geometry_and_engine_are_actual_host_facts(project, monkeypatch):
    calls = fake_media(monkeypatch)

    async def run():
        async with bound_media(project, kind="image") as (_, reader, row, stager):
            faces = await reader.extract(row, ColumnRef("renamed"))
            (face,) = faces
            assert (face.x, face.y, face.w, face.h) == (4, 8, 20, 16)
            assert stager._files[face.face][3]["bbox"] == {
                "x": 4,
                "y": 8,
                "w": 20,
                "h": 16,
            }
            assert (
                stager._files[face.face][3]["model_sha256"]
                == row_media_read._YUNET_MODEL_SHA256
            )
            face.x = 999
            assert stager._files[face.face][3]["bbox"]["x"] == 4

    asyncio.run(run())
    assert len(calls) == 1
    assert len(calls[0][1]["policy"].confine.read) == 2


@pytest.mark.parametrize(
    "mutation", ["row", "source", "value", "hidden", "closed", "cancelled"]
)
def test_actual_reader_refuses_unadmitted_or_revoked_calls(
    project, monkeypatch, mutation
):
    calls = fake_media(monkeypatch)
    cancelled = False

    async def run():
        nonlocal cancelled
        async with bound_media(project, cancelled=lambda: cancelled) as (
            owner,
            reader,
            row,
            _,
        ):
            source = ColumnRef("renamed")
            if mutation == "row":
                row = Row(dict(row.values))
            elif mutation == "source":
                source = ColumnRef("other")
            elif mutation == "value":
                row.values["renamed"]["blob"] = "a" * 64
            elif mutation == "hidden":
                project.db.execute("UPDATE columns SET hidden=1 WHERE name='renamed'")
            elif mutation == "closed":
                await owner.aclose()
            elif mutation == "cancelled":
                cancelled = True
            with pytest.raises((RowError, RuntimeError, asyncio.CancelledError)):
                await reader.extract(row, source, sampling=FrameCount())

    asyncio.run(run())
    assert not calls


@pytest.mark.parametrize("value", [None, ""])
def test_empty_cells_do_no_work_and_return_empty_list(project, monkeypatch, value):
    calls = fake_media(monkeypatch)

    async def run():
        async with bound_media(project) as (owner, original, _, _):
            row = Row({"renamed": value})
            reader = owner.bind_row(
                row,
                sheet_id=original._sheet_id,
                row_id=original._row_id,
                sources={
                    "renamed": {
                        "column_id": original._sources["renamed"]["column_id"],
                        "value": value,
                    }
                },
            )
            assert (
                await reader.extract(row, ColumnRef("renamed"), sampling=FrameCount())
                == []
            )

    asyncio.run(run())
    assert not calls


@pytest.mark.parametrize(
    "params",
    [
        {"sampling": {"kind": "count", "count": 0}},
        {"sampling": {"kind": "count", "count": 201}},
        {"sampling": {"kind": "count", "count": True}},
        {"sampling": {"kind": "interval", "seconds": 0}},
        {"sampling": {"kind": "interval", "seconds": float("inf")}},
        {"sampling": {"kind": "count", "count": 2, "seconds": 3}},
        {"max_dimension": 15},
        {"max_dimension": 4097},
        {"max_dimension": True},
        {"frame_count": 4},
    ],
)
def test_closed_sampling_and_dimensions(params):
    with pytest.raises(ValidationError):
        VideoFramesParams(source="video", **params)


def test_defaults_and_semantic_source_types():
    assert VideoFramesParams(source="video").sampling == FrameCount(count=4)
    assert VideoFramesParams(source="video").max_dimension is None
    assert VideoFramesParams(source="video").source.references()[
        0
    ].accepted_column_types == ("video", "file")
    assert ExtractFacesParams(source="image").source.references()[
        0
    ].accepted_column_types == ("image", "file")
    assert VideoFramesParams.model_json_schema()["properties"]["sampling"][
        "default"
    ] == {"kind": "count", "count": 4}


def test_preview_images_are_inline_bounded_across_outputs_without_blob_writes(
    project, monkeypatch
):
    fake_media(monkeypatch)

    async def run():
        async with bound_media(project, preview=True) as (_, reader, row, stager):
            before = project.db.execute("SELECT count(*) FROM blobs").fetchone()[0]
            monkeypatch.setattr(
                project.blob_store,
                "put_path",
                lambda *args, **kwargs: pytest.fail("preview must not promote bytes"),
            )
            frames = await reader.extract(
                row, ColumnRef("renamed"), sampling=FrameCount(count=8)
            )
            first = reader._stager.dump_field(list[Frame], frames, "first")
            second = reader._stager.dump_field(list[Frame], frames, "second")
            images = [item["image"] for item in first + second]
            assert sum("inline_data_url" in image for image in images) == 12
            assert sum(image.get("preview_omitted", False) for image in images) == 4
            assert stager.preview_omitted == 4
            assert all("blob" not in image for image in images)
            assert all(
                image["inline_data_url"].startswith("data:image/jpeg;base64,")
                for image in images
                if "inline_data_url" in image
            )
            assert [item["t"] for item in first] == [frame.t for frame in frames]
            assert (
                project.db.execute("SELECT count(*) FROM blobs").fetchone()[0] == before
            )
            assert not stager._pending

    asyncio.run(run())


@pytest.mark.parametrize("sampling", [FrameCount(count=2), FrameInterval(seconds=1)])
def test_actual_sampling_revalidates_mutated_typed_values(
    project, monkeypatch, sampling
):
    calls = fake_media(monkeypatch)
    if isinstance(sampling, FrameCount):
        sampling.count = 201
    else:
        sampling.seconds = float("nan")

    async def run():
        async with bound_media(project) as (_, reader, row, _):
            with pytest.raises(ValidationError):
                await reader.extract(row, ColumnRef("renamed"), sampling=sampling)

    asyncio.run(run())
    assert calls == []


def test_close_settles_active_worker_before_releasing_borrowed_input(
    project, monkeypatch
):
    started = asyncio.Event()
    settled = []

    async def sandbox(command, **kwargs):
        source = Path(command[-1])
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert source.is_file()
            settled.append(True)

    monkeypatch.setattr(row_media_read, "run_sandboxed", sandbox)
    monkeypatch.setattr(row_media_read, "_ffmpeg_binary", lambda name: f"/bin/{name}")

    async def run():
        async with bound_media(project) as (owner, reader, row, _):
            pending = asyncio.create_task(
                reader.extract(row, ColumnRef("renamed"), sampling=FrameCount())
            )
            await started.wait()
            await owner.aclose()
            assert settled == [True]
            with pytest.raises(asyncio.CancelledError):
                await pending

    asyncio.run(run())


def test_custom_handler_derives_actual_sampling_and_reuses_images_in_named_outputs(
    project, monkeypatch
):
    from pydantic import BaseModel
    from frisket.actions.core import ActionCategory, RegisteredAction, action, map_rows
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.row_media import VideoColumn
    from frisket.actions.row_media_types import FrameExtractor
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionParams, ActionRequest, RowResult
    from frisket.engine.executor.actions import _default_map_runner_factory
    from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
    from frisket.engine.store.receipts import ReceiptStore

    calls = fake_media(monkeypatch)

    class Params(ActionParams):
        clip: VideoColumn

    class Output(BaseModel):
        timeline: list[Frame]
        cover: list[Frame]

    async def handler(
        params: Params, row: Row, reader: FrameExtractor
    ) -> RowResult[Output]:
        (frame,) = await reader.extract(row, params.clip, sampling=FrameCount(count=1))
        return RowResult(
            output=Output(
                timeline=[frame, frame], cover=[Frame(t=99, image=frame.image)]
            )
        )

    registered = RegisteredAction(
        "example.frame_projection",
        action(
            name="frame_projection",
            title="Frame projection",
            description="Actual read and declared occurrences",
            category=ActionCategory.EXTRACT,
            run=map_rows(handler),
        ),
    )
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, registered.action_id: registered},
    )
    sheet = project.add_sheet("Videos")
    column = project.add_column(sheet, "clip", type="video")
    digest = project.add_blob(b"fixture video", filename="a.mp4", mime="video/mp4")
    rows = project.add_rows(
        sheet,
        [{"clip": media_cell(digest, filename="a.mp4", mime="video/mp4")}],
        {"clip": column},
    )
    bound = BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id=registered.action_id,
            scope={"kind": "sheet_rows", "sheet_id": sheet, "row_ids": rows},
            params={"clip": "clip"},
            output_names={"timeline": "Timeline", "cover": "Cover"},
            idempotency_key="derived",
        ),
    )
    result = run_typed_map_rows_action(
        project, "p", bound, None, _default_map_runner_factory
    )
    assert result.status == "completed", result.model_dump_json()
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    observed = [
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == "row_file_output"
    ]
    assert len(observed) == 3
    assert {item["facts"]["timestamp"] for item in observed} == {6.0}
    assert {item["facts"]["source"]["source_column"] for item in observed} == {"clip"}
    assert len({item["primary"]["blob_hash"] for item in observed}) == 1
    assert {(item["output_key"], tuple(item["item_path"])) for item in observed} == {
        ("timeline", (0, "image")),
        ("timeline", (1, "image")),
        ("cover", (0, "image")),
    }
    named = [item.ref for item in receipt.outputs if item.kind == "named_result"]
    assert {item["route"] for item in named} == {"Timeline", "Cover"}
    cover = next(item["column_id"] for item in named if item["route"] == "Cover")
    assert project.get_values(sheet, cover)[rows[0]][0]["t"] == 99
    assert len(calls) == 2
