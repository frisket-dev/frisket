import pytest

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry, action
from frisket.actions.page_capture import CapturePageParams, capture_page
from frisket.actions.types import ActionRequest


def registered_capture():
    return ActionRegistry(
        [
            ActionNamespace(
                "custom",
                actions=[
                    action(
                        name="capture",
                        title="Capture",
                        description="Capture page files or a links table.",
                        run=capture_page,
                        category=ActionCategory.SOURCES,
                        examples=(CapturePageParams(source="url"),),
                    )
                ],
            )
        ]
    ).get("custom.capture")


@pytest.mark.parametrize("sheet_name", [None, "Links"])
def test_capture_defers_destination_validation_to_its_actual_preparation(sheet_name):
    registered = registered_capture()
    params, fields = registered.bind_request(
        ActionRequest(
            action_id="custom.capture",
            scope={"kind": "sheet_rows", "sheet_id": 1, "row_ids": [3]},
            params={"source": "url"},
            output_names={"page": "Archived page"},
            sheet_name=sheet_name,
            idempotency_key="capture-declaration",
        )
    )
    assert params.source.name == "url"
    assert fields is None


def test_capture_catalog_requires_network_and_dynamic_output_resolution():
    entry = registered_capture().catalog_entry()
    assert entry["required_capabilities"] == ["project:write", "external:url_capture"]
    assert entry["ui_hints"]["dynamic_outputs"] is True
    assert entry["examples"][0]["scope"] == {"kind": "sheet_rows", "sheet_id": 1}
    assert entry["examples"][0]["params"] == {"source": "url"}


def test_capture_refuses_project_scope():
    with pytest.raises(ValueError, match="scope"):
        registered_capture().bind_request(
            ActionRequest(
                action_id="custom.capture",
                scope={"kind": "project"},
                params={"source": "url"},
                idempotency_key="capture-declaration",
            )
        )
