from __future__ import annotations

import struct

import pytest
from pydantic import ValidationError

from frisket.actions.media_metadata import MetadataOutput, MetadataParams
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import (
    root_action_catalog,
    typed_action_for_request,
    validate_root_action,
)
from frisket.engine.executor.action_specs import queued_project_run_kinds
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan


def _request(**params):
    return {
        "action_id": "media.extract_metadata",
        "scope": {"kind": "sheet_rows", "sheet_id": 7, "row_ids": [2, 3]},
        "params": {"source": "asset", "output_mode": "object", **params},
        "output_names": {"details": "Meta"},
        "idempotency_key": "metadata-canonical",
    }


def test_media_extract_metadata_params_are_strict_and_canonical() -> None:
    params = MetadataParams.model_validate({"source": "asset"})
    assert params.model_dump(mode="json") == {
        "source": "asset",
        "output_mode": "columns",
        "refresh": False,
    }
    for payload in (
        {"source": 123},
        {"source": "asset", "include_sensitive": True},
        {"source": "asset", "refresh": "false"},
        {"source": "asset", "output_mode": "unexpected"},
        {"source": "asset", "input_column": "asset"},
        {"source": "asset", "output_prefix": "Meta"},
        {"source": "asset", "sheet_id": 7},
    ):
        with pytest.raises(ValidationError):
            MetadataParams.model_validate(payload)


def test_media_extract_metadata_names_and_scope_use_canonical_request_validation() -> (
    None
):
    for scope in (
        {"kind": "sheet_rows", "sheet_id": True, "row_ids": [1]},
        {"kind": "sheet_rows", "sheet_id": 7, "row_ids": [1, 1]},
        {"kind": "sheet_rows", "sheet_id": 7, "row_ids": [True]},
    ):
        assert not validate_root_action({**_request(), "scope": scope}).ok
    for names in ({"details": ""}, {"details": " Meta "}, {"unknown": "Meta"}):
        assert not validate_root_action({**_request(), "output_names": names}).ok
    # Prefix expansion belongs to the caller; semantic Params do not own a
    # competing prefix/naming authority.
    schema = MetadataParams.model_json_schema()
    assert set(schema["properties"]) == {"source", "output_mode", "refresh"}
    assert schema["additionalProperties"] is False


def test_media_extract_metadata_validation_canonicalizes_hash_fields() -> None:
    validation = validate_root_action(_request())
    assert validation.ok is True
    assert validation.action.action_id == "media.extract_metadata"
    assert validation.params == {"source": "asset", "output_mode": "object"}
    assert validation.action.output_names == {"details": "Meta"}


def test_media_extract_metadata_is_registered_and_queued() -> None:
    kind = "media.extract_metadata"
    assert ACTION_REGISTRY.get(kind).action_id == kind
    assert kind in queued_project_run_kinds()
    catalog = root_action_catalog()
    entry = next(item for item in catalog.actions if item.kind == kind)
    assert entry.cost_policy.kind == "none"
    assert set(entry.required_capabilities) == {"project:read", "project:write"}
    assert entry.ui_hints["semantic_controls"]["source"] == "column"


def test_media_extract_metadata_runner_spec_has_canonical_action_and_inputs(
    tmp_path,
) -> None:
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "plan.frisket")
    try:
        sheet_id = project.add_sheet("Assets")
        source_id = project.add_column(sheet_id, "asset", type="file")
        row_ids = project.add_rows(sheet_id, [{"asset": None}], {"asset": source_id})
        request = _request(output_mode="columns", refresh=True)
        request["scope"] = {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            "row_ids": row_ids,
        }
        request["output_names"] = {"details": "asset_meta_details"}
        plan = build_typed_map_rows_plan(project, typed_action_for_request(request))
        spec = plan.spec_dict()
        assert spec["action_kind"] == "media.extract_metadata"
        assert spec["input_columns"] == ["asset"]
        assert spec["row_ids"] == row_ids
        assert spec["params"] == {
            "source": "asset",
            "output_mode": "columns",
            "refresh": True,
        }
        assert spec["output_names"]["details"] == "asset_meta_details"
        assert "recipe" not in spec and "output_prefix" not in spec
    finally:
        project.close()


def test_media_extract_metadata_declares_stable_typed_output_families() -> None:
    registered = ACTION_REGISTRY.get("media.extract_metadata")
    object_fields = registered.definition.run.resolve_output_fields(
        MetadataParams(source="asset", output_mode="object")
    )
    assert [(field.key, field.column_type) for field in object_fields] == [
        ("details", "json")
    ]
    column_fields = registered.definition.run.resolve_output_fields(
        MetadataParams(source="asset")
    )
    assert [(field.key, field.column_type) for field in column_fields] == [
        ("kind", "category"),
        ("format", "text"),
        ("mime", "text"),
        ("filename", "text"),
        ("size_bytes", "integer"),
        ("blob_hash", "text"),
        ("probe_status", "category"),
        ("title", "text"),
        ("creator", "text"),
        ("created_at", "date"),
        ("width_pixels", "integer"),
        ("height_pixels", "integer"),
        ("capture_device", "text"),
        ("duration_seconds", "number"),
        ("bitrate_bps", "integer"),
        ("audio_codec", "text"),
        ("sample_rate_hz", "integer"),
        ("channel_count", "integer"),
        ("album", "text"),
        ("video_codec", "text"),
        ("frame_rate_fps", "number"),
        ("page_count", "integer"),
        ("pdf_encrypted", "boolean"),
        ("details", "json"),
    ]
    assert column_fields[4].format == "filesize"


@pytest.mark.parametrize(
    "value",
    ["2026-09-06", "2026-09-06T00:00:00", "2026-09-06T00:00:00+02:30"],
)
def test_projected_date_preserves_date_only_midnight_and_timezone(value) -> None:
    output = MetadataOutput(created_at=value)
    assert output.model_dump(mode="json")["created_at"] == value


def test_completed_key_uses_standard_idempotency_replay_without_reprobe(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import frisket.ops.media_metadata as metadata
    import frisket.engine.executor.media_metadata_read as reader_module
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store.media_blobs import media_cell
    from frisket.engine.store import Project

    payload = b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", 5, 4)
    project = Project.create(tmp_path / "metadata-replay.frisket", name="metadata")
    try:
        sheet_id = project.add_sheet("Assets")
        column_id = project.add_column(sheet_id, "asset", type="image")
        digest = project.add_blob(payload, filename="asset.png", mime="image/png")
        row_id = project.add_rows(
            sheet_id,
            [{"asset": media_cell(digest, mime="image/png", filename="asset.png")}],
            {"asset": column_id},
        )[0]
        action = {
            "action_id": "media.extract_metadata",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": [row_id]},
            "params": {"source": "asset", "output_mode": "object"},
            "output_names": {"details": "meta"},
            "idempotency_key": "metadata-standard-replay",
        }

        monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: None)
        original_extract = reader_module.extract_media_metadata
        probe_calls = 0

        def counted_extract(*args, **kwargs):
            nonlocal probe_calls
            probe_calls += 1
            return original_extract(*args, **kwargs)

        monkeypatch.setattr(reader_module, "extract_media_metadata", counted_extract)
        first = run_action_spec(project, action, project_id="metadata-replay")
        assert first.status == "completed", first.errors
        assert first.receipt_id is not None
        assert probe_calls == 1

        replay = run_action_spec(project, action, project_id="metadata-replay")
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert replay.run_id == first.run_id
        assert probe_calls == 1
    finally:
        project.close()
