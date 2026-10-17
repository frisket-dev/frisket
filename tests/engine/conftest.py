"""Engine tests own their catalogs; tests/ai covers pricing refresh lifecycle."""

import pytest


@pytest.fixture(autouse=True)
def no_background_pricing_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep engine tests free of process-lifetime catalog maintenance."""
    monkeypatch.setattr(
        "frisket.ai.llm.pricing_refresh.start_pricing_refresh", lambda: None
    )
