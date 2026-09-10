"""Sidecar test fixtures. Contract tests run against STUB engines (the heavy
torch-class libraries are optional extras and absent from the default sync);
the `real` suite builds the default registry and importorskips per library."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frisket_models.app import create_app
from stub_helpers import TOKEN, stub_registry


@pytest.fixture
def client() -> TestClient:
    app = create_app(token=TOKEN, registry=stub_registry(), concurrency=2)
    return TestClient(app)
