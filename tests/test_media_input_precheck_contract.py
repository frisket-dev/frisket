from __future__ import annotations

import pytest


def test_video_interval_overflow_is_a_structural_refusal() -> None:
    from pydantic import ValidationError
    from frisket.actions.row_media import VideoFramesParams

    with pytest.raises(ValidationError):
        VideoFramesParams.model_validate(
            {"source": "video", "sampling": {"kind": "interval", "seconds": 10**10_000}}
        )


@pytest.mark.parametrize("confirmation", [True, 1, ""])
@pytest.mark.parametrize("action_id", ["media.fetch_url", "media.ytdlp_download"])
def test_typed_acquisition_confirmation_is_a_request_token(action_id, confirmation):
    from pydantic import ValidationError
    from frisket.actions.types import ActionRequest

    with pytest.raises(ValidationError):
        ActionRequest(
            action_id=action_id,
            scope={"kind": "sheet_rows", "sheet_id": 1},
            params={"source": "url"},
            confirmation=confirmation,
            idempotency_key="fetch-confirmation",
        )


@pytest.mark.parametrize(
    ("scope_override", "params_override", "field"),
    [
        ({"sheet_id": True}, {}, "sheet_id"),
        ({}, {"source": " "}, "source"),
        ({"row_ids": []}, {}, "row_ids"),
        ({"row_ids": [1, False]}, {}, "row_ids"),
        ({"row_ids": [1, 1]}, {}, "row_ids"),
        ({}, {"unexpected": True}, "unexpected"),
    ],
    ids=("sheet", "source", "row-list", "row-item", "duplicate-rows", "unknown-param"),
)
@pytest.mark.parametrize(
    "action_id",
    [
        "media.ocr",
        "media.transcribe",
        "media.extract_pdf_tables",
        "media.fetch_url",
        "media.ytdlp_download",
        "web.capture_screenshot",
        "media.video_frames",
        "media.extract_faces",
        "media.to_markdown",
    ],
)
def test_typed_media_models_own_malformed_scope_and_source(
    scope_override, params_override, field, action_id
):
    from pydantic import ValidationError
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest

    with pytest.raises(ValidationError) as raised:
        request = ActionRequest.model_validate(
            {
                "action_id": action_id,
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": 1,
                    "row_ids": [1, 2],
                    **scope_override,
                },
                "params": {"source": "source", **params_override},
                "idempotency_key": "media-precheck",
            }
        )
        BoundTypedActionRequest.bind(ACTION_REGISTRY.get(request.action_id), request)
    assert any(field in error["loc"] for error in raised.value.errors())
    if field == "unexpected":
        assert any(
            error["type"] == "extra_forbidden" and error["loc"] == ("unexpected",)
            for error in raised.value.errors()
        )


@pytest.mark.parametrize(
    "legacy_param",
    ["sheet_id", "input_columns", "row_ids", "output_name", "confirmed"],
)
def test_ytdlp_params_do_not_reintroduce_envelope_or_legacy_source_fields(legacy_param):
    from pydantic import ValidationError

    from frisket.actions.media_download import MediaDownloadParams

    with pytest.raises(ValidationError) as raised:
        MediaDownloadParams.model_validate({"source": "url", legacy_param: True})
    assert any(
        error["type"] == "extra_forbidden" and error["loc"] == (legacy_param,)
        for error in raised.value.errors()
    )
