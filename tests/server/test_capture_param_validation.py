from types import SimpleNamespace

import pytest

from frisket.actions.core import ActionCategory, RegisteredAction, action
from frisket.actions.file_types import UrlColumn
from frisket.actions.page_capture_types import (
    CaptureOptions,
    PageCapturer,
    PreparedPageCapture,
)
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionParams
from frisket.engine.store import Project
from frisket.server.services import action_param_validation


class CustomCaptureParams(ActionParams):
    website: UrlColumn
    collect_links: bool = False


def custom_capture(
    params: CustomCaptureParams, pages: PageCapturer
) -> PreparedPageCapture:
    return pages.prepare(
        params.website,
        options=CaptureOptions(output_mode="links" if params.collect_links else "page"),
    )


@pytest.mark.parametrize("links", [False, True])
def test_capture_schema_projects_actual_prepared_destination_without_writes(
    tmp_path, monkeypatch, links
):
    kind = "custom.capture_mode"
    registered = RegisteredAction(
        kind,
        action(
            name="capture_mode",
            title="Capture",
            description="Resolve actual capture options.",
            category=ActionCategory.SOURCES,
            run=custom_capture,
        ),
    )
    monkeypatch.setattr(
        ACTION_REGISTRY, "_actions", {**ACTION_REGISTRY._actions, kind: registered}
    )
    monkeypatch.setattr(
        action_param_validation,
        "NEW_ACTION_IDS",
        action_param_validation.NEW_ACTION_IDS | {kind},
    )
    project = Project.create(tmp_path / "capture.frisket")
    try:
        sheet = project.add_sheet("Pages")
        column = project.add_column(sheet, "url", type="link")
        project.add_rows(sheet, [{"url": "https://example.test/page"}], {"url": column})
        project.db.commit()
        before = project.db.total_changes
        service = action_param_validation.ActionParamValidationService(
            SimpleNamespace(edition="local", get=lambda _id: project)
        )
        result = service.validate_params(
            "project",
            {
                "action_id": kind,
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"website": "url", "collect_links": links},
            },
        )
        assert result["diagnostics"] == {}
        assert result["creates_sheet"] is links
        assert [field["key"] for field in result["logical_outputs"]] == (
            ["url", "anchor_text", "source_url"] if links else ["page"]
        )
        assert [
            field.get("existing_column_policy", "generated")
            for field in result["logical_outputs"]
        ] == (["generated"] * 3 if links else ["compatible"])
        assert project.db.total_changes == before
    finally:
        project.close()
