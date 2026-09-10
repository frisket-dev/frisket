from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("action_id", "column_type", "accepted_types"),
    [
        ("media.ocr", "image", ("image", "file")),
        ("media.transcribe", "audio", ("audio", "video", "file")),
        ("media.extract_pdf_tables", "file", ("file",)),
        ("media.fetch_url", "link", ("link", "text")),
        ("media.ytdlp_download", "link", ("link", "text")),
        ("media.ytdlp_download", "text", ("link", "text")),
        ("web.capture_screenshot", "link", ("text", "link")),
        ("media.video_frames", "video", ("video", "file")),
        ("media.extract_faces", "image", ("image", "file")),
        ("media.to_markdown", "file", ("file", "text")),
    ],
)
def test_typed_media_runtime_consumes_the_declared_semantic_source(
    tmp_path, monkeypatch, action_id, column_type, accepted_types
):
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest, discover_references
    from frisket.engine.executor import map_rows_action
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "media-source.frisket")
    try:
        sheet = project.add_sheet("Media")
        column = project.add_column(sheet, "chosen_source", column_type)
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get(action_id),
            ActionRequest(
                action_id=action_id,
                scope={"kind": "sheet_rows", "sheet_id": sheet},
                params={"source": "chosen_source"},
                idempotency_key="media-source",
            ),
        )
        seen = []
        original = map_rows_action.validate_typed_project_references

        def validate(project, sheet_id, params):
            seen.append(params)
            return original(project, sheet_id, params)

        monkeypatch.setattr(
            map_rows_action, "validate_typed_project_references", validate
        )
        plan = map_rows_action.build_typed_map_rows_plan(project, bound)
        assert seen == [bound.params]
        (source,) = discover_references(seen[0])
        assert source.column == bound.params.source.name == "chosen_source"
        assert (
            source.accepted_column_types
            == bound.params.source.accepted_column_types
            == accepted_types
        )
        assert dict(plan.source_column_ids) == {source.column: column}
        if action_id == "media.ytdlp_download":
            from frisket.actions.file_types import UrlColumn

            assert isinstance(bound.params.source, UrlColumn)
    finally:
        project.close()
