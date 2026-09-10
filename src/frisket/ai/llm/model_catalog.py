"""Curated, runnable model catalog shared by the server and maintenance tools.

Provider ``/models`` endpoints include embeddings, audio, fine-tunes, snapshots,
and models that do not support Frisket's chat/structured-output wire contract.
The picker therefore uses a small reviewed catalog instead of blindly exposing
everything an account can see.  Edit ``model_catalog.json`` and run
``scripts/dev/sync_model_catalog.py`` to refresh the web fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).with_name("model_catalog.json")


def load_model_catalog() -> dict[str, Any]:
    return json.loads(CATALOG_PATH.read_text())


MODEL_ENTRIES: dict[str, list[dict[str, Any]]] = load_model_catalog()["providers"]
MODEL_CATALOG: dict[str, list[str]] = {
    provider: [entry["id"] for entry in entries]
    for provider, entries in MODEL_ENTRIES.items()
}
