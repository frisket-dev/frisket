from __future__ import annotations

import pytest

from frisket.server import action_catalog_hints
from frisket.server.action_catalog_hints import (
    action_catalog_payload_with_launcher_hints,
)


def test_catalog_refuses_static_and_descriptor_source_requirement_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No static source_requirements value may coexist with the descriptor."""

    hints = action_catalog_hints.project_action_catalog_launcher_hints({})
    hints["media.ocr"]["source_requirements"] = [
        {"id": "static_copy", "param": "source"}
    ]
    monkeypatch.setattr(
        action_catalog_hints,
        "project_action_catalog_launcher_hints",
        lambda *args, **kwargs: hints,
    )

    with pytest.raises(ValueError, match="source_requirements"):
        action_catalog_payload_with_launcher_hints({})
