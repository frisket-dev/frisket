from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from frisket.contracts.action import CostPolicy, IdempotencyPolicy, RetryPolicy
from frisket.sdk.declaration import op
from frisket.sdk.inputs import Columns
from frisket.server import action_catalog_hints
from frisket.server.action_catalog_hints import (
    action_catalog_payload_with_launcher_hints,
)


class _Params(BaseModel):
    input_columns: list[str]


class _Output(BaseModel):
    value: str


def _policies() -> dict[str, Any]:
    return {
        "cost": CostPolicy(kind="none"),
        "idempotency": IdempotencyPolicy(
            supported=True,
            scope="project",
            key_field="idempotency_key",
            behavior="Test declaration.",
        ),
        "retry": RetryPolicy(supported=False, strategy="none"),
    }


def test_typed_inputs_reject_ui_source_metadata_as_a_second_authority() -> None:
    """A typed declaration cannot retain a second UI source authority."""

    with pytest.raises(ValueError, match="inputs|source"):
        op(
            kind="test.source_authority",
            title="Source authority test",
            description="Source authority test",
            params_model=_Params,
            output_model=_Output,
            errors=[],
            side_effects=[],
            examples=[],
            primary_fields=["input_columns"],
            inputs=Columns("input_columns"),
            extra_ui_hints={"source_requirements": []},
            **_policies(),
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
