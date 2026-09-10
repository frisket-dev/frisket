from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.store.media_blobs import media_cell
from frisket.engine.sandbox.shim import SandboxResult
from frisket.engine.store import Project
from tests.engine.test_row_media_reader import jpeg_bytes

# Reset by _patch_face_sandbox at the start of every harness test for this
# case; asserts on it are only meaningful behind that patch.
_SANDBOX_CALLS: list[str] = []


def _patch_face_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    _SANDBOX_CALLS.clear()

    async def fake_sandboxed(cmd: list[str], *args: Any, **kwargs: Any):
        _SANDBOX_CALLS.append("run_sandboxed")
        payload = json.loads(kwargs["stdin_data"].decode("utf-8"))
        out_dir = Path(payload["out_dir"])
        crop_path = out_dir / "face0.jpg"
        crop_path.write_bytes(jpeg_bytes())
        return SandboxResult(
            0,
            json.dumps(
                {"faces": [{"x": 4, "y": 8, "w": 32, "h": 40, "crop": str(crop_path)}]}
            ),
            "",
        )

    monkeypatch.setattr(
        "frisket.engine.executor.row_media_read.run_sandboxed", fake_sandboxed
    )


def _extract_faces_action(
    *,
    sheet_id: int,
    row_ids: list[int] | None = None,
    input_columns: list[str] | None = None,
    output_name: str = "faces",
    idempotency_key: str = "media_extract_faces@sha256:first",
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    assert input_columns is None or len(input_columns) == 1
    assert capabilities is None
    scope = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    return {
        "action_id": "media.extract_faces",
        "scope": scope,
        "params": {"source": input_columns[0] if input_columns else "image"},
        "output_names": {"faces": output_name},
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("Photos")
    columns = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "image": project.add_column(sheet_id, "image", type="image"),
    }
    blobs = [
        project.add_blob(
            f"fake image bytes {label}".encode("utf-8"),
            filename=f"{label}.jpg",
            mime="image/jpeg",
            source_url=f"https://media.example/{label}.jpg",
        )
        for label in ("photo1", "photo2")
    ]
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Photo 1",
                "image": media_cell(
                    blobs[0],
                    mime="image/jpeg",
                    filename="photo1.jpg",
                ),
            },
            {
                "title": "Photo 2",
                "image": media_cell(
                    blobs[1],
                    mime="image/jpeg",
                    filename="photo2.jpg",
                ),
            },
        ],
        columns,
    )
    return {"sheet_id": sheet_id, "row_ids": row_ids, "blobs": blobs}


@pytest.fixture(params=["frames", "faces"])
def media_case(request, monkeypatch, tmp_path):
    from tests.ops.test_media_video_frames_executor import (
        _patch_ffmpeg_sandbox,
        _seed as seed_video,
        _video_frames_action,
    )

    project = Project.create(tmp_path / "typed.frisket")
    if request.param == "frames":
        _patch_ffmpeg_sandbox(monkeypatch)
        seeded = seed_video(project, tmp_path)
        make = _video_frames_action
        image_key = "image"
    else:
        _patch_face_sandbox(monkeypatch)
        seeded = _seed(project, tmp_path)
        make = _extract_faces_action
        image_key = "face"
    try:
        yield project, seeded, make, request.param, image_key
    finally:
        project.close()


def test_typed_media_results_named_routes_durable_occurrences_and_replay(
    media_case, monkeypatch
):
    from frisket.engine.executor.actions import run_action_spec
    from frisket.engine.store.receipts import ReceiptStore

    project, seeded, make, key, image_key = media_case
    request = make(sheet_id=seeded["sheet_id"], output_name="Renamed")
    result = run_action_spec(project, request, project_id="p")
    assert result.status == "completed", result.model_dump_json()
    assert [(output.kind, output.name) for output in result.outputs] == [
        ("column", "Renamed"),
        ("named_result", "Renamed"),
    ]
    column = next(
        output.column_id for output in result.outputs if output.kind == "column"
    )
    values = project.get_values(seeded["sheet_id"], column)
    assert set(values) == set(seeded["row_ids"])
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    named = next(
        output.ref for output in receipt.outputs if output.kind == "named_result"
    )
    assert named["schema"] == key
    assert named["route"] == "Renamed"
    assert named["row_ids"] == seeded["row_ids"]
    assert named["may_feed"] == ["derive.table_from_list"]
    observations = [
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == "row_file_output"
    ]
    assert len(observations) == sum(len(items) for items in values.values())
    for row_id, items in values.items():
        for index, item in enumerate(items):
            image = item[image_key]
            assert set(image) == {"blob", "mime", "filename"}
            assert image["mime"] == "image/jpeg"
            observation = next(
                obs
                for obs in observations
                if obs["row_id"] == row_id and obs["item_path"] == [index, image_key]
            )
            assert observation["column_id"] == column
            assert observation["primary"]["blob_hash"] == image["blob"]
            assert observation["facts"]["source"]["blob_hash"] in seeded["blobs"]
            if key == "frames":
                assert item["t"] == [3.0, 9.0][index]
                assert observation["facts"]["timestamp"] == item["t"]
            else:
                assert observation["facts"]["bbox"] == {
                    name: item[name] for name in ("x", "y", "w", "h")
                }
                assert observation["facts"]["model_sha256"]

    async def unexpected(*args, **kwargs):
        pytest.fail("completed replay must not extract again")

    monkeypatch.setattr(
        "frisket.engine.executor.row_media_read.run_sandboxed", unexpected
    )
    replay = run_action_spec(project, request, project_id="p")
    assert replay.status == "completed", replay.model_dump_json()
    assert replay.receipt_id == result.receipt_id
    assert replay.run_id == result.run_id
    assert (
        ReceiptStore(project).parsed_by_id(result.receipt_id).evidence
        == receipt.evidence
    )


def test_typed_media_selected_rows_collision_and_conflicting_identity(media_case):
    from frisket.engine.executor.actions import run_action_spec

    project, seeded, make, key, _ = media_case
    sheet, rows = seeded["sheet_id"], seeded["row_ids"]
    request = make(sheet_id=sheet, row_ids=[rows[0]])
    completed = run_action_spec(project, request, project_id="p")
    assert completed.status == "completed", completed.model_dump_json()
    output = next(item for item in completed.outputs if item.kind == "column")
    values = project.get_values(sheet, output.column_id)
    assert values.get(rows[0])
    assert values.get(rows[1]) is None
    count = project.db.execute("SELECT count(*) FROM runs").fetchone()[0]
    conflict = run_action_spec(
        project,
        {
            **request,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": rows},
        },
        project_id="p",
    )
    assert conflict.status == "failed"
    assert any(error.code == "idempotency_conflict" for error in conflict.errors)
    collision = run_action_spec(
        project,
        make(sheet_id=sheet, output_name="title", idempotency_key="collision"),
        project_id="p",
    )
    assert collision.status == "failed"
    assert project.db.execute("SELECT count(*) FROM runs").fetchone()[0] == count


def test_typed_media_replay_refuses_changed_values_or_missing_outputs(media_case):
    from frisket.engine.executor.actions import run_action_spec

    project, seeded, make, _, _ = media_case
    request = make(sheet_id=seeded["sheet_id"])
    completed = run_action_spec(project, request, project_id="p")
    assert completed.status == "completed", completed.model_dump_json()
    output = next(item for item in completed.outputs if item.kind == "column")
    project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (output.column_id,))
    project.db.commit()
    replay = run_action_spec(project, request, project_id="p")
    assert replay.status == "failed"
    assert [error.code for error in replay.errors] == ["stale_replay"]


def test_typed_media_repeated_backfill_keeps_output_identity_and_named_route(
    media_case,
    monkeypatch,
):
    from frisket.engine.executor import row_media_read
    from frisket.engine.executor.actions import run_action_spec
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.store.result_generations import ResultGenerationStore

    project, seeded, make, key, image_key = media_case
    calls = []
    sandbox = row_media_read.run_sandboxed

    async def counted(*args, **kwargs):
        calls.append(args[0])
        return await sandbox(*args, **kwargs)

    monkeypatch.setattr(row_media_read, "run_sandboxed", counted)
    sheet, rows = seeded["sheet_id"], seeded["row_ids"]
    original = run_action_spec(project, make(sheet_id=sheet), project_id="p")
    assert original.status == "completed", original.model_dump_json()
    output = next(item for item in original.outputs if item.kind == "column")
    project.db.execute(
        "UPDATE columns SET name='Renamed' WHERE id=?", (output.column_id,)
    )
    project.db.commit()
    untouched = ResultGenerationStore(project).read_cell_heads(output.column_id)[
        rows[1]
    ]
    for index in range(2):
        before_calls = len(calls)
        body = {
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [rows[0]]},
            "params": {"column": "Renamed"},
            "idempotency_key": f"media-backfill-{index}",
        }
        result = run_action_spec(project, body, project_id="p")
        assert result.status == "completed", result.model_dump_json()
        assert len(calls) == before_calls + (3 if key == "frames" else 1)
        heads = ResultGenerationStore(project).read_cell_heads(output.column_id)
        assert heads[rows[0]].run_id == result.run_id
        assert heads[rows[1]] == untouched
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        named = next(
            item.ref for item in receipt.outputs if item.kind == "named_result"
        )
        assert named["route"] == "Renamed"
        assert named["schema"] == key
        assert named["column_id"] == output.column_id
        assert named["row_ids"] == [rows[0]]
        occurrences = [
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "row_file_output"
        ]
        assert len(occurrences) == (2 if key == "frames" else 1)
        assert all(
            item["row_id"] == rows[0] and item["column_id"] == output.column_id
            for item in occurrences
        )
        replay = run_action_spec(project, body, project_id="p")
        assert replay.status == "completed", replay.model_dump_json()
        assert replay.receipt_id == result.receipt_id
        assert len(calls) == before_calls + (3 if key == "frames" else 1)


def test_zero_images_preserve_actual_read_facts_and_feedable_empty_list(
    media_case, monkeypatch
):
    from frisket.engine.executor.actions import run_action_spec
    from frisket.engine.store.receipts import ReceiptStore

    project, seeded, make, key, _ = media_case

    async def empty(command, **kwargs):
        if Path(command[0]).name == "ffprobe":
            return SandboxResult(0, json.dumps({"format": {"duration": "12"}}), "")
        if Path(command[0]).name == "ffmpeg":
            return SandboxResult(1, "", "No frame")
        return SandboxResult(0, json.dumps({"faces": []}), "")

    monkeypatch.setattr("frisket.engine.executor.row_media_read.run_sandboxed", empty)
    result = run_action_spec(project, make(sheet_id=seeded["sheet_id"]), project_id="p")
    assert result.status == "completed", result.model_dump_json()
    output = next(item for item in result.outputs if item.kind == "column")
    assert list(project.get_values(seeded["sheet_id"], output.column_id).values()) == [
        [],
        [],
    ]
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    observations = [
        item.ref for item in receipt.evidence if item.ref.get("kind") == "row_file_call"
    ]
    assert len(observations) == 2
    assert {item["source"]["blob_hash"] for item in observations} == set(
        seeded["blobs"]
    )
    assert all(item["observation_kind"] == "media_read" for item in observations)
    assert all(
        item["engine"] == ("ffmpeg" if key == "frames" else "opencv_yunet")
        for item in observations
    )
    assert not any(
        item.ref.get("kind") == "row_file_output" for item in receipt.evidence
    )
    named = next(item.ref for item in receipt.outputs if item.kind == "named_result")
    assert named["row_ids"] == seeded["row_ids"]


def test_typed_media_preview_returns_inline_images_without_publication(media_case):
    import asyncio
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest
    from frisket.ai.llm import ModelRouter
    from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
    from frisket.engine.runner.map_runner import MapRunner
    from frisket.execution.attempt_authority import UnroutedOnlyAuthority

    project, seeded, make, key, image_key = media_case
    body = make(sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"])
    if key == "frames":
        body["params"]["sampling"] = {"kind": "count", "count": 8}
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(body["action_id"]), ActionRequest.model_validate(body)
    )
    plan = build_typed_map_rows_plan(project, bound)
    runner = MapRunner(
        project,
        ModelRouter(cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(project),
    )
    tables = ("runs", "receipts", "blobs", "results", "columns")
    before = {
        table: [tuple(row) for row in project.db.execute(f"SELECT * FROM {table}")]
        for table in tables
    }
    result = asyncio.run(runner.preview(plan.spec_dict(), program=plan.program))
    assert result.row_ids == seeded["row_ids"]
    images = [
        item[image_key]
        for cells in result.values.values()
        for item in cells[key]["value"]
    ]
    inline = [image for image in images if "inline_data_url" in image]
    omitted = [image for image in images if image.get("preview_omitted")]
    assert len(inline) == (12 if key == "frames" else 2)
    assert len(omitted) == (4 if key == "frames" else 0)
    assert all(
        image["inline_data_url"].startswith("data:image/jpeg;base64,")
        for image in inline
    )
    assert all("blob" not in image for image in images)
    assert {
        table: [tuple(row) for row in project.db.execute(f"SELECT * FROM {table}")]
        for table in tables
    } == before
