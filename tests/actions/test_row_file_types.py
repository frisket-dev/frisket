import hashlib

import pytest
from pydantic import BaseModel, Field, field_serializer

from frisket.actions.core import map_rows
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.file_fetch import FETCH_URL
from frisket.actions.screenshot import CAPTURE_SCREENSHOT
from frisket.actions.row_media import VIDEO_FRAMES, EXTRACT_FACES
from frisket.actions.types import ActionParams, Row, RowResult, StagedFile, StagedImage
from frisket.engine.executor.blob_outputs import RowBlobOutput, RowBlobPlan
from frisket.engine.executor.row_file_stage import RowFileStager


def test_file_actions_infer_semantic_top_level_and_nested_columns():
    assert FETCH_URL.run.output_fields[0].column_type == "file"
    assert CAPTURE_SCREENSHOT.run.output_fields[0].column_type == "image"
    assert VIDEO_FRAMES.run.output_fields[0].column_type == "json"
    assert EXTRACT_FACES.run.output_fields[0].column_type == "json"
    fetch = ACTION_REGISTRY.get("media.fetch_url").catalog_entry()
    assert "external_cost_requires_confirmation" in {
        item["code"] for item in fetch["errors"]
    }
    assert fetch["cost_policy"] == {"kind": "unknown", "requires_confirmation": True}


def test_file_output_serializers_cannot_move_declared_handle_positions():
    class Params(ActionParams):
        pass

    class Output(BaseModel):
        image: StagedImage

        @field_serializer("image")
        def move(self, value):
            return {"forged": "image"}

    def handler(params: Params, row: Row) -> RowResult[Output]:
        raise AssertionError("registration only")

    with pytest.raises(TypeError, match="serializers"):
        map_rows(handler)


def test_stager_copies_host_plan_and_preserves_distinct_occurrences(tmp_path):
    payload = b"file fixture"
    path = tmp_path / "input"
    path.write_bytes(payload)
    stager = RowFileStager(None)
    bound = stager.bind_row(1)
    try:
        handle = bound.stage_output(
            RowBlobOutput(
                primary=RowBlobPlan(
                    role="download",
                    content_digest=hashlib.sha256(payload).hexdigest(),
                    staged_path=path,
                    filename="a.txt",
                    mime="text/plain",
                    source_url="https://example.test/a",
                ),
                facts={"kind": "fetch"},
            )
        )
        first = bound.dump_field(StagedFile, handle, "first")
        second = bound.dump_field(StagedFile, handle, "second")
        assert first == second
        assert bound.descriptors("first")[0]["output_key"] == "first"
        assert bound.descriptors("second")[0]["output_key"] == "second"
        with pytest.raises(Exception, match="admitted row"):
            stager.bind_row(2).dump_field(StagedFile, handle, "foreign")
        with pytest.raises(Exception, match="admitted row"):
            bound.dump_field(StagedFile, StagedFile(len(payload)), "forged")
    finally:
        stager.close()


def test_staged_nested_aliases_must_be_unique():
    class Params(ActionParams):
        pass

    class Item(BaseModel):
        first: StagedFile = Field(alias="same")
        second: StagedFile = Field(alias="same")

    class Output(BaseModel):
        files: list[Item]

    def handler(params: Params, row: Row) -> RowResult[Output]:
        raise AssertionError("registration only")

    with pytest.raises(TypeError, match="unique"):
        map_rows(handler)
