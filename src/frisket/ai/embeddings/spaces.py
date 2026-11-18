"""Embedding space identity.

A vector is only comparable to another vector produced under the *same*
comparability contract. ``space_id`` is that contract, hashed: a generated id
over every parameter that can change vector values or distance semantics
(model, dimension, dtype, metric, normalization, modality, and the normalized
vector options). Presentation-only fields (labels, schedule, generated column
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
from dataclasses import dataclass
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

# The vectors an in-process FastEmbed model produces for a given model id depend
# on the library's tokenizer limits and model sources, so a local space records
# the encoding generation as its ``model_revision``. Change it only when a
# FastEmbed upgrade changes those vectors; a patch release that does not must
# keep it, or every local index needlessly re-embeds.
LOCAL_FASTEMBED_ENCODER_REVISION = "fastembed-0.9"

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


@dataclass(frozen=True, slots=True)
class DescriptorEmbeddingSpace:
    """The cache-relevant shape of an existing strict space descriptor."""

    dimensions: int
    identity: str
    normalized: bool

    @classmethod
    def from_descriptor(cls, descriptor: dict[str, Any]) -> DescriptorEmbeddingSpace:
        validate_descriptor(descriptor)
        return cls(
            dimensions=descriptor["dimension"],
            identity=space_id_for(descriptor),
            normalized=descriptor["normalization"] == "l2",
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
    model_revision: str | None = None,
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
    hash is supplied. ``model_revision`` defaults to the provider's current
    encoding generation (see ``current_space_descriptor``). Validation is
    fail-loud: a zero dimension or an unknown modality/metric/dtype raises rather
    than minting a quietly-wrong space.
    """
    voh = vector_options_hash_value or vector_options_hash(vector_options)
    if model_revision is None:
        model_revision = _current_model_revision(str(provider_id)) or "unknown"
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


def _current_model_revision(provider_id: str) -> str | None:
    """The encoding generation this build produces for a provider, or None when
    the provider's vectors do not depend on a library in this process."""
    return LOCAL_FASTEMBED_ENCODER_REVISION if provider_id == "fastembed" else None


def current_space_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    """The descriptor that the space's provider produces vectors under today.

    A local space minted by an older encoding generation maps to the current
    one, so its stored vectors read as belonging to a different space and are
    re-embedded; every other descriptor is returned unchanged."""
    revision = _current_model_revision(str(descriptor["provider_id"]))
    if revision is None or descriptor["model_revision"] == revision:
        return descriptor
    return {**descriptor, "model_revision": revision}


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
