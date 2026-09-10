"""Embedding space identity.

A vector is only comparable to another vector produced under the *same*
comparability contract. ``space_id`` is that contract, hashed: a generated id
over every parameter that can change vector values or distance semantics
(model, dimension, dtype, metric, normalization, modality, and the normalized
vector options). Presentation-only fields (labels, schedule, output column
name) are deliberately excluded — they are stored separately on the index or
receipts and must never fork a space.

``space_id`` is never typed by a user. Two descriptors with identical strict
fields collide on both ``space_id`` and the full ``descriptor_hash``; when in
doubt, include a parameter in the strict descriptor — it is safer to create two
spaces than to silently compare incompatible vectors.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

SPACE_CONTRACT_VERSION = "frisket.embedding_space.v1"
SOURCE_PAYLOAD_CONTRACT = "frisket.embedding_source_payload.v1"

# Modalities the contract represents from the start. Individual providers may be
# unavailable, but the source/action/provider shape is never text-only.
ALL_MODALITIES: tuple[str, ...] = (
    "text",
    "image",
    "audio",
    "video",
    "file",
    "row",
    "multimodal",
)
VALID_MODALITIES = frozenset(ALL_MODALITIES)
VALID_METRICS = frozenset({"cosine", "dot", "euclidean", "l2"})
VALID_DTYPES = frozenset({"float32", "float16", "int8"})

# The strict descriptor, in a fixed field set. canonical_json sorts keys, so the
# tuple order here is documentation, not the hash input — but the SET is the
# identity boundary: a key outside it (a label, a schedule) cannot affect a
# space_id because it is dropped before hashing.
STRICT_FIELDS: tuple[str, ...] = (
    "contract_version",
    "provider_id",
    "provider_kind",
    "requested_model",
    "actual_model_id",
    "model_revision",
    "modality",
    "dimension",
    "dtype",
    "distance_metric",
    "normalization",
    "vector_options_hash",
    "source_payload_contract",
)


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def vector_options_hash(options: dict[str, Any] | None) -> str:
    """Hash of the normalized vector options (model knobs that change vectors:
    truncate dimension, resize/crop policy, chunk policy, ...). Order-independent
    because canonical_json sorts keys."""
    digest = hashlib.sha256(canonical_json(options or {}).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def make_space_descriptor(
    *,
    provider_id: str,
    provider_kind: str,
    actual_model_id: str,
    modality: str,
    dimension: int,
    requested_model: str | None = None,
    model_revision: str = "unknown",
    dtype: str = "float32",
    distance_metric: str = "cosine",
    normalization: str = "none",
    vector_options: dict[str, Any] | None = None,
    vector_options_hash_value: str | None = None,
    source_payload_contract: str = SOURCE_PAYLOAD_CONTRACT,
    contract_version: str = SPACE_CONTRACT_VERSION,
) -> dict[str, Any]:
    """Build and validate a strict space descriptor.

    ``vector_options`` is hashed into ``vector_options_hash`` unless an explicit
    hash is supplied. Validation is fail-loud: a zero dimension or an unknown
    modality/metric/dtype raises rather than minting a quietly-wrong space.
    """
    voh = vector_options_hash_value or vector_options_hash(vector_options)
    descriptor = {
        "contract_version": contract_version,
        "provider_id": str(provider_id),
        "provider_kind": str(provider_kind),
        "requested_model": requested_model,
        "actual_model_id": str(actual_model_id),
        "model_revision": str(model_revision),
        "modality": str(modality),
        "dimension": int(dimension),
        "dtype": str(dtype),
        "distance_metric": str(distance_metric),
        "normalization": str(normalization),
        "vector_options_hash": voh,
        "source_payload_contract": str(source_payload_contract),
    }
    validate_descriptor(descriptor)
    return descriptor


def validate_descriptor(descriptor: dict[str, Any]) -> None:
    missing = [f for f in STRICT_FIELDS if f not in descriptor]
    if missing:
        raise ValueError(f"space descriptor missing fields: {missing}")
    if not isinstance(descriptor["dimension"], int) or descriptor["dimension"] <= 0:
        raise ValueError("space descriptor dimension must be a positive int")
    if descriptor["modality"] not in VALID_MODALITIES:
        raise ValueError(f"unknown modality: {descriptor['modality']!r}")
    if descriptor["distance_metric"] not in VALID_METRICS:
        raise ValueError(f"unknown distance_metric: {descriptor['distance_metric']!r}")
    if descriptor["dtype"] not in VALID_DTYPES:
        raise ValueError(f"unknown dtype: {descriptor['dtype']!r}")
    for f in ("provider_id", "provider_kind", "actual_model_id"):
        if not descriptor[f]:
            raise ValueError(f"space descriptor {f} must be non-empty")


def _strict_subset(descriptor: dict[str, Any]) -> dict[str, Any]:
    """Exactly the strict fields — drops any presentation key a caller left in
    the dict so it cannot influence identity."""
    return {f: descriptor[f] for f in STRICT_FIELDS}


def space_id_for(descriptor: dict[str, Any]) -> str:
    """``emb_`` + lowercase base32 of sha256(canonical strict descriptor)[:24]."""
    validate_descriptor(descriptor)
    digest = hashlib.sha256(
        canonical_json(_strict_subset(descriptor)).encode("utf-8")
    ).digest()
    body = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return "emb_" + body[:24]


def descriptor_hash(descriptor: dict[str, Any]) -> str:
    """Full sha256 of the canonical strict descriptor — the UNIQUE column in
    ``embedding_spaces`` (space_id is a truncated, friendlier identity)."""
    validate_descriptor(descriptor)
    digest = hashlib.sha256(
        canonical_json(_strict_subset(descriptor)).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"
