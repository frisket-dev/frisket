"""Operator GPU preferences are passed to Modal in their declared order."""

from __future__ import annotations

import pytest


def test_gpu_preferences_uses_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket.ai.models.modal_gpu import gpu_preferences

    monkeypatch.delenv("FRISKET_MODAL_DOTS_GPU", raising=False)

    assert gpu_preferences("FRISKET_MODAL_DOTS_GPU", ("L4", "A10")) == ["L4", "A10"]


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("A10", ["A10"]),
        (" A100-80GB , L40S , L4 ", ["A100-80GB", "L40S", "L4"]),
    ],
)
def test_gpu_preferences_honors_operator_order(
    monkeypatch: pytest.MonkeyPatch, configured: str, expected: list[str]
) -> None:
    from frisket.ai.models.modal_gpu import gpu_preferences

    monkeypatch.setenv("FRISKET_MODAL_DOTS_GPU", configured)

    assert gpu_preferences("FRISKET_MODAL_DOTS_GPU", ("L4", "A10")) == expected


@pytest.mark.parametrize("configured", ["", " ", "L4,", ",A10", "L4, ,A10"])
def test_gpu_preferences_rejects_empty_choices(
    monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    from frisket.ai.models.modal_gpu import gpu_preferences

    monkeypatch.setenv("FRISKET_MODAL_DOTS_GPU", configured)

    with pytest.raises(ValueError, match="FRISKET_MODAL_DOTS_GPU"):
        gpu_preferences("FRISKET_MODAL_DOTS_GPU", ("L4", "A10"))
