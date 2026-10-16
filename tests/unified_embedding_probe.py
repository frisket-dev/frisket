"""Read-only test probe for named datasets in the unified retrieval store."""

from __future__ import annotations

from frisket.ai.embeddings.unified_index import UNIFIED_BACKEND_ID, dataset_rows
from frisket.retrieval.store import RetrievalStore


class UnifiedEmbeddingProbe:
    BACKEND_ID = UNIFIED_BACKEND_ID

    def __init__(self, project):
        self._store = RetrievalStore(project).open()

    def ensure_schema(self) -> None:
        return None

    def item_counts(self, index_id: str) -> dict[str, int]:
        amount = len(dataset_rows(self._store, index_id))
        return {"ready": amount} if amount else {}

    def get_item(self, index_id: str, source_key: str):
        pair = dataset_rows(self._store, index_id).get(int(source_key))
        if pair is None:
            return None
        return {"part_id": pair[0], "representation_id": pair[1], "status": "ready"}

    def index_binding(self, index_id: str):
        return tuple(sorted(dataset_rows(self._store, index_id).items()))

    def close(self) -> None:
        self._store.close()
