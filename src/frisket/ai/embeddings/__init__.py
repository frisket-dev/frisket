"""Native embeddings.

The embedding-owned building blocks; the v1 ``embedding.index_*`` actions
(`frisket.executor.action_families.embeddings`) and the queued source-append
refresh (`frisket.jobs.embeddings`) are built on top of these.

- ``EmbeddingSpace`` (spaces.py): a strict comparability contract, hashed to a
  generated ``space_id``. Vectors in different spaces are never comparable.
- ``EmbeddingBatchResult`` / ``embedding_capabilities`` (capabilities.py): the
  gateway metadata that stops embeddings from being a bare vector list, plus the
  all-modality picker contract. ``EmbeddingGateway`` (gateway.py) resolves a
  provider's embedder.
- ``EmbeddingStore`` (store.py): canonical space/index definitions in project.db.
- ``VectorBackend`` (vector_backend.py): raw vectors in the rebuildable
  project.embeddings.db sidecar, with exact f32 cosine ranking.
- ``build_source_payloads`` / ``source_hash`` (source_payload.py): the canonical
  source payload + content hash, shared by index refresh and the similarity
  resolver (so staleness is judged the same way everywhere).
- ``resolve_embedding_similarity`` (similarity.py): the ``embedding_similarity``
  retrieval backend ("find similar") over one index/space.
"""

from __future__ import annotations

from .capabilities import (
    ALL_MODALITIES,
    EmbeddingBatchResult,
    EmbeddingCapability,
    build_batch_result,
    embedding_capabilities,
    resolve_embedding_capability,
    space_descriptor_from_result,
)
from .gateway import (
    EmbeddingBackendUnavailable,
    EmbeddingGateway,
    EmbeddingProviderError,
)
from .spaces import (
    SOURCE_PAYLOAD_CONTRACT,
    SPACE_CONTRACT_VERSION,
    canonical_json,
    descriptor_hash,
    make_space_descriptor,
    space_id_for,
    validate_descriptor,
    vector_options_hash,
)
from .similarity import (
    EMBEDDING_SIMILARITY_QUERY_KIND,
    SimilarityError,
    SimilarityHit,
    SimilarityResult,
    resolve_embedding_similarity,
)
from .source_payload import build_source_payloads, source_hash, source_hash_for_row
from .store import REMOTE_PROVIDER_KINDS, EmbeddingStore
from .vector_backend import VectorBackend, pack_f32, unpack_f32

__all__ = [
    "ALL_MODALITIES",
    "EMBEDDING_SIMILARITY_QUERY_KIND",
    "REMOTE_PROVIDER_KINDS",
    "SOURCE_PAYLOAD_CONTRACT",
    "SPACE_CONTRACT_VERSION",
    "EmbeddingBackendUnavailable",
    "EmbeddingBatchResult",
    "EmbeddingCapability",
    "EmbeddingGateway",
    "EmbeddingProviderError",
    "EmbeddingStore",
    "SimilarityError",
    "SimilarityHit",
    "SimilarityResult",
    "VectorBackend",
    "build_batch_result",
    "build_source_payloads",
    "resolve_embedding_similarity",
    "canonical_json",
    "descriptor_hash",
    "embedding_capabilities",
    "make_space_descriptor",
    "pack_f32",
    "resolve_embedding_capability",
    "source_hash",
    "source_hash_for_row",
    "space_descriptor_from_result",
    "space_id_for",
    "unpack_f32",
    "validate_descriptor",
    "vector_options_hash",
]
