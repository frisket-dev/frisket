import pytest

from frisket.actions.registry import ACTION_REGISTRY, NEW_ACTION_IDS
from frisket.actions.system import root_action_catalog, validate_root_action
from frisket.engine.executor.action_dispatch import is_queued


@pytest.mark.parametrize("kind", ("media.ocr", "media.transcribe"))
def test_media_has_one_typed_owner_and_rejects_old_request_shape(kind):
    assert kind in NEW_ACTION_IDS
    assert is_queued(kind)
    assert sum(entry.kind == kind for entry in root_action_catalog().actions) == 1
    assert validate_root_action(
        {
            "action_id": kind,
            "scope": {"kind": "sheet_rows", "sheet_id": 1},
            "params": {"source": "media"},
            "idempotency_key": "media-registration",
        }
    ).ok
    assert not validate_root_action(
        {
            "kind": kind,
            "params": {"sheet_id": 1, "input_columns": ["media"]},
            "idempotency_key": "old-media-registration",
        }
    ).ok


@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        ("media.ocr", {"text": "text", "blocks": "json"}),
        (
            "media.transcribe",
            {
                "text": "timestamped_transcript",
                "segments": "json",
                "detected_language": "category",
            },
        ),
    ),
)
def test_media_catalog_is_projected_from_typed_outputs(kind, expected):
    from frisket.actions.types import ActionRequest, SheetRows

    registered = ACTION_REGISTRY.get(kind)
    _, outputs = registered.bind_request(
        ActionRequest(
            action_id=kind,
            scope=SheetRows(sheet_id=1),
            params={"source": "media"},
            idempotency_key="media-outputs",
        )
    )
    assert {output.key: output.column_type for output in outputs} == expected


def test_media_authoring_types_are_public_sdk_imports():
    from frisket.sdk import (
        AudioColumn,
        OcrColumn,
        OcrOptions,
        OcrReader,
        OcrText,
        Transcriber,
        TranscriptionOptions,
        TranscriptText,
    )

    assert AudioColumn("audio").name == "audio"
    assert OcrColumn("scan").name == "scan"
    assert OcrOptions().normalize("rapidocr")["dpi"] == 200
    assert TranscriptionOptions().normalize("faster_whisper") == {}
    assert OcrText("recognized").model_dump() == "recognized"
    assert TranscriptText("spoken").model_dump() == "spoken"
    assert callable(OcrReader.recognize)
    assert callable(Transcriber.transcribe)


@pytest.mark.parametrize("name", ("ocr", "transcribe"))
def test_custom_media_actions_receive_capability_engine_choices(name, monkeypatch):
    import frisket.actions.registry as registry_module
    from frisket.actions.core import ActionNamespace, ActionRegistry
    from frisket.actions.media import OCR, TRANSCRIBE
    from frisket.server.action_catalog_hints import (
        project_action_catalog_launcher_hints,
    )

    sidecar = {"configured": False, "available": False, "engines": [], "error": None}
    expected = project_action_catalog_launcher_hints(sidecar)[f"media.{name}"][
        "engines"
    ]
    registry = ActionRegistry(
        (ActionNamespace("custom", actions=(OCR if name == "ocr" else TRANSCRIBE,)),)
    )
    monkeypatch.setattr(registry_module, "ACTION_REGISTRY", registry)
    actual = project_action_catalog_launcher_hints(sidecar)[f"custom.{name}"]["engines"]
    assert expected
    assert actual == expected
