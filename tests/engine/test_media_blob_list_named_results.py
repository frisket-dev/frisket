from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.sandbox.shim import SandboxResult
from frisket.engine.store import Project

from test_media_extract_faces_executor import (
    _extract_faces_action,
    _seed as _seed_image_rows,
)
from tests.ops.test_media_video_frames_executor import (
    _seed as _seed_video_rows,
    _video_frames_action,
)


PROJECT_ID = "project-media-blob-list-chain"


def _derive_action(
    *,
    sheet_id: int,
    column_id: int,
    run_id: int,
    route: str,
    schema: str,
    target_sheet_name: str,
    item_schema: dict[str, Any],
    columns: list[dict[str, Any]],
    key: str,
) -> dict[str, Any]:
    return {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": target_sheet_name,
        "params": {
            "source": {
                "kind": "named_result",
                "sheet_id": sheet_id,
                "column_id": column_id,
                "run_id": run_id,
                "route": route,
                "schema": schema,
            },
            "item_schema": item_schema,
            "columns": columns,
        },
        "idempotency_key": key,
    }


def test_media_extract_faces_named_result_feeds_typed_image_derive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = Project.create(tmp_path / "faces.frisket", name="blob list faces")
    seeded = _seed_image_rows(project, tmp_path)
    sheet_id, row_ids = seeded["sheet_id"], seeded["row_ids"]

    async def fake_sandboxed(cmd: list[str], *args: Any, **kwargs: Any):
        del cmd, args
        payload = json.loads(kwargs["stdin_data"].decode("utf-8"))
        crop_path = Path(payload["out_dir"]) / "face0.jpg"
        from tests.engine.test_row_media_reader import jpeg_bytes

        crop_path.write_bytes(jpeg_bytes())
        return SandboxResult(
            0,
            json.dumps(
                {
                    "faces": [
                        {
                            "x": 4,
                            "y": 8,
                            "w": 32,
                            "h": 40,
                            "crop": str(crop_path),
                        }
                    ]
                }
            ),
            "",
        )

    monkeypatch.setattr(
        "frisket.engine.executor.row_media_read.run_sandboxed", fake_sandboxed
    )
    try:
        faces = run_action_spec(
            project,
            _extract_faces_action(sheet_id=sheet_id),
            project_id=PROJECT_ID,
        )
        assert faces.status == "completed", faces.errors
        assert faces.run_id is not None
        named = next(
            output for output in faces.outputs if output.kind == "named_result"
        )
        assert named.ref["source_action_kind"] == "media.extract_faces"
        assert named.ref["row_ids"] == row_ids

        derived = run_action_spec(
            project,
            _derive_action(
                sheet_id=sheet_id,
                column_id=int(named.column_id or named.ref["column_id"]),
                run_id=faces.run_id,
                route="faces",
                schema="faces",
                target_sheet_name="Face Rows",
                item_schema=named.ref["item_schema"],
                columns=[
                    {"name": "x", "path": "$.x", "type": "number"},
                    {"name": "y", "path": "$.y", "type": "number"},
                    {"name": "w", "path": "$.w", "type": "number"},
                    {"name": "h", "path": "$.h", "type": "number"},
                    {"name": "face", "path": "$.face", "type": "image"},
                ],
                key="media_blob_lists@sha256:derive-faces",
            ),
            project_id=PROJECT_ID,
        )
        assert derived.status == "completed", derived.errors
        child_sheet_id = next(
            output.sheet_id for output in derived.outputs if output.kind == "sheet"
        )
        assert child_sheet_id is not None
        child_rows = project.db.execute(
            "SELECT * FROM rows WHERE sheet_id=? ORDER BY position",
            (child_sheet_id,),
        ).fetchall()
        assert [int(row["parent_row_id"]) for row in child_rows] == row_ids
        columns = _columns(project, child_sheet_id)
        assert columns["face"]["type"] == "image"
        face_values = project.get_values(child_sheet_id, int(columns["face"]["id"]))
        assert all(value["blob"] for value in face_values.values())
    finally:
        project.close()


def test_media_video_frames_named_result_feeds_typed_image_derive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = Project.create(tmp_path / "frames.frisket", name="blob list frames")
    seeded = _seed_video_rows(project, tmp_path)
    sheet_id, row_ids = seeded["sheet_id"], seeded["row_ids"]

    async def fake_sandboxed(cmd: list[str], *args: Any, **kwargs: Any):
        del args, kwargs
        if Path(cmd[0]).name == "ffprobe":
            return SandboxResult(0, json.dumps({"format": {"duration": "12.0"}}), "")
        if Path(cmd[0]).name == "ffmpeg":
            from tests.engine.test_row_media_reader import jpeg_bytes

            Path(cmd[-1]).write_bytes(jpeg_bytes())
            return SandboxResult(0, "", "")
        raise AssertionError(f"unexpected sandbox command: {cmd}")

    monkeypatch.setattr(
        "frisket.engine.executor.row_media_read.run_sandboxed", fake_sandboxed
    )
    try:
        frames = run_action_spec(
            project,
            _video_frames_action(sheet_id=sheet_id, frame_count=1),
            project_id=PROJECT_ID,
        )
        assert frames.status == "completed", frames.errors
        assert frames.run_id is not None
        named = next(
            output for output in frames.outputs if output.kind == "named_result"
        )
        assert named.ref["source_action_kind"] == "media.video_frames"
        assert named.ref["row_ids"] == row_ids

        derived = run_action_spec(
            project,
            _derive_action(
                sheet_id=sheet_id,
                column_id=int(named.column_id or named.ref["column_id"]),
                run_id=frames.run_id,
                route="frames",
                schema="frames",
                target_sheet_name="Frame Rows",
                item_schema=named.ref["item_schema"],
                columns=[
                    {"name": "frame", "path": "$.image", "type": "image"},
                    {"name": "timestamp", "path": "$.t", "type": "number"},
                ],
                key="media_blob_lists@sha256:derive-frames",
            ),
            project_id=PROJECT_ID,
        )
        assert derived.status == "completed", derived.errors
        child_sheet_id = next(
            output.sheet_id for output in derived.outputs if output.kind == "sheet"
        )
        assert child_sheet_id is not None
        child_rows = project.db.execute(
            "SELECT * FROM rows WHERE sheet_id=? ORDER BY position",
            (child_sheet_id,),
        ).fetchall()
        assert [int(row["parent_row_id"]) for row in child_rows] == row_ids
        columns = _columns(project, child_sheet_id)
        assert columns["frame"]["type"] == "image"
        frame_values = project.get_values(child_sheet_id, int(columns["frame"]["id"]))
        assert all(value["blob"] for value in frame_values.values())
        timestamps = project.get_values(child_sheet_id, int(columns["timestamp"]["id"]))
        assert list(timestamps.values()) == [6.0, 6.0]
    finally:
        project.close()


def _columns(project, sheet_id: int) -> dict[str, Any]:
    return {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (sheet_id,),
        ).fetchall()
    }
