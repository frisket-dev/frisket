"""Named embedding indexes over the unified retrieval sidecar.

The project database owns index definitions, policies, and receipts.  This
module maps one current row-composite payload to the immutable retrieval
source/part/representation identities used by both legacy migration and live
refreshes.
"""

from __future__ import annotations

import json
import hashlib
import math
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from frisket.engine.store.retrieval_lifecycle import read_binding_revisions
from frisket.retrieval.contracts import (
    BindingRevision,
    ScopedBinding,
    SourceAnchor,
    SourceLocator,
    SourceRevision,
)
from frisket.retrieval.native import validate_vector_blob
from frisket.retrieval.sources import (
    identity_digest,
    input_hash,
    part_id,
    producer_recipe_id,
    representation_id,
    row_composite_source_revision,
)
from frisket.retrieval.store import (
    BindingDependency,
    NamedDatasetMember,
    NamedDatasetVector,
    PartRepresentation,
    RetrievalStore,
    SourcePublication,
    StoredBinding,
    StoredPart,
)

from .spaces import DescriptorEmbeddingSpace, space_id_for, validate_descriptor


ROW_RECIPE_CONTRACT = "frisket.legacy-row-composite.v1"
ROW_CHANNEL = "row"
UNIFIED_BACKEND_ID = "frisket.retrieval.sqlite-vec1"
UNIFIED_BACKEND_VERSION = "1"
_F32 = struct.Struct("<f")


@dataclass(frozen=True, slots=True)
class NamedIndexRecipe:
    value: Mapping[str, Any]
    recipe_id: str


@dataclass(frozen=True, slots=True)
class NamedIndexItem:
    source_key: str
    row_id: int
    source_hash: str
    source_ref: Mapping[str, Any]
    encoder_input: str | bytes
    input_hash: str
    source: SourceRevision
    locator: SourceLocator
    part_id: str
    representation_id: str
    binding: StoredBinding


@dataclass(frozen=True, slots=True)
class ResolvedNamedIndexAnchor:
    anchor: SourceAnchor
    space_id: str


class NamedIndexAnchorError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def named_index_recipe(
    *,
    descriptor: Mapping[str, Any],
    ordered_columns: Sequence[tuple[str, int]],
    source_policy: Mapping[str, Any],
    source_policy_hash: str,
) -> NamedIndexRecipe:
    """Return the exact row-composite recipe shared with cache migration."""

    value = {
        "contract": ROW_RECIPE_CONTRACT,
        "source_payload_contract": descriptor["source_payload_contract"],
        "modality": descriptor["modality"],
        "ordered_source_columns": [
            {"name": name, "column_id": column_id}
            for name, column_id in ordered_columns
        ],
        "source_policy": dict(source_policy),
        "source_policy_hash": source_policy_hash,
    }
    return NamedIndexRecipe(value=value, recipe_id=producer_recipe_id(value))


def descriptor_space(space: Any) -> tuple[dict[str, Any], DescriptorEmbeddingSpace]:
    try:
        descriptor = json.loads(space["descriptor_json"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("embedding space descriptor is invalid") from exc
    if not isinstance(descriptor, dict):
        raise ValueError("embedding space descriptor must be an object")
    validate_descriptor(descriptor)
    expected_id = space_id_for(descriptor)
    if expected_id != str(space["id"]):
        raise ValueError("embedding space identity does not match its descriptor")
    return descriptor, DescriptorEmbeddingSpace.from_descriptor(descriptor)


def ordered_index_columns(project: Any, index: Any) -> tuple[tuple[str, int], ...]:
    try:
        names = json.loads(index["source_columns_json"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("embedding index source columns are invalid") from exc
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(name, str) or not name for name in names)
        or len(set(names)) != len(names)
    ):
        raise ValueError("embedding index source columns are invalid")
    by_name = {
        str(column["name"]): int(column["id"])
        for column in project.columns(int(index["sheet_id"]), include_hidden=True)
    }
    try:
        return tuple((name, by_name[name]) for name in names)
    except KeyError as exc:
        raise ValueError(
            f"embedding index source column is missing: {exc.args[0]}"
        ) from exc


def recipe_for_index(
    project: Any, index: Any, descriptor: Mapping[str, Any]
) -> NamedIndexRecipe:
    try:
        policy = json.loads(index["source_policy_json"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("embedding index source policy is invalid") from exc
    if not isinstance(policy, dict):
        raise ValueError("embedding index source policy must be an object")
    return named_index_recipe(
        descriptor=descriptor,
        ordered_columns=ordered_index_columns(project, index),
        source_policy=policy,
        source_policy_hash=str(index["source_policy_hash"]),
    )


def plan_named_index_items(
    project: Any,
    *,
    index: Any,
    space: Any,
    payloads: Sequence[Mapping[str, Any]],
) -> tuple[NamedIndexRecipe, tuple[NamedIndexItem, ...]]:
    """Bind current payloads to exact current-cell revisions and identities."""

    descriptor, _space = descriptor_space(space)
    columns = ordered_index_columns(project, index)
    recipe = recipe_for_index(project, index, descriptor)
    pairs = [
        (int(payload["row_id"]), column_id)
        for payload in payloads
        for _name, column_id in columns
    ]
    revisions = read_binding_revisions(project.db, pairs)
    items: list[NamedIndexItem] = []
    for payload in payloads:
        row_id = int(payload["row_id"])
        dependencies: list[BindingDependency] = []
        member_revisions: list[dict[str, int]] = []
        for _name, column_id in columns:
            # A missing sparse cell is part of the input too. Filling it later
            # must invalidate the composite, just like changing a present cell.
            revision = revisions.get((row_id, column_id), 0)
            dependencies.append(BindingDependency(row_id, column_id, revision))
            member_revisions.append(
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "retrieval_revision": revision,
                }
            )
        encoder_input = named_encoder_input(project, payload, descriptor)
        exact_input_hash = input_hash(encoder_input)
        source_key = str(payload["source_key"])
        source = row_composite_source_revision(
            index_id=str(index["id"]),
            source_key=source_key,
            ordered_member_revisions=member_revisions,
            recipe=recipe.value,
            content_hash=exact_input_hash,
        )
        locator = SourceLocator(kind="row_composite")
        stored_part_id = part_id(source, recipe.value, locator, kind="row_composite")
        stored_representation_id = representation_id(
            space=str(space["id"]), input_hash=exact_input_hash
        )
        binding_id = identity_digest(
            "binding",
            {
                "source_id": source.source_id,
                "sheet_id": int(index["sheet_id"]),
                "row_id": row_id,
                "column_id": None,
            },
        )
        items.append(
            NamedIndexItem(
                source_key=source_key,
                row_id=row_id,
                source_hash=str(payload["source_hash"]),
                source_ref=dict(payload["source_ref"]),
                encoder_input=encoder_input,
                input_hash=exact_input_hash,
                source=source,
                locator=locator,
                part_id=stored_part_id,
                representation_id=stored_representation_id,
                binding=StoredBinding(
                    binding_id=binding_id,
                    sheet_id=int(index["sheet_id"]),
                    row_id=row_id,
                    column_id=None,
                    dependencies=tuple(dependencies),
                ),
            )
        )
    return recipe, tuple(items)


def resolve_named_cell_anchor(
    project: Any,
    *,
    index_id: str,
    sheet_id: int,
    row_id: int,
    column_id: int,
) -> ResolvedNamedIndexAnchor:
    """Resolve a browser cell selection to the index's exact current row composite."""

    from frisket.features.watchlists.specs import (
        canonical_json as query_canonical_json,
        normalize_query_spec,
    )
    from frisket.querysets import SheetRowSetError, resolve_sheet_filter_rows

    from .scope import IndexScopeError
    from .source_payload import build_source_payloads
    from .store import EmbeddingStore

    metadata = EmbeddingStore(project)
    index = metadata.get_index(index_id)
    if index is None:
        raise NamedIndexAnchorError(
            "embedding_index_not_found", f"no embedding index {index_id!r}"
        )
    if int(index["sheet_id"]) != sheet_id:
        raise NamedIndexAnchorError(
            "embedding_anchor_scope_mismatch",
            "the selected cell is outside the embedding index sheet",
        )
    columns = ordered_index_columns(project, index)
    if column_id not in {identifier for _name, identifier in columns}:
        raise NamedIndexAnchorError(
            "embedding_anchor_scope_mismatch",
            "the selected cell is not one of the embedding index source columns",
        )
    try:
        raw_scope = json.loads(index["source_query_json"] or "{}")
        if not raw_scope:
            in_scope = row_id in project.visible_row_ids(sheet_id, [row_id])
        else:
            normalized = normalize_query_spec(raw_scope)
            if normalized.get("kind") != "sheet.filter":
                raise NamedIndexAnchorError(
                    "embedding_source_unsupported",
                    "the embedding index source scope is not a sheet filter",
                )
            scope = normalized.get("scope") or {}
            if scope.get("sheet_id") != sheet_id:
                raise NamedIndexAnchorError(
                    "embedding_anchor_scope_mismatch",
                    "the embedding index source scope names a different sheet",
                )
            page = resolve_sheet_filter_rows(
                project,
                sheet_id,
                filter_=query_canonical_json(normalized.get("filter", {})),
                sort=(
                    query_canonical_json(normalized["sort"])
                    if "sort" in normalized
                    else None
                ),
                limit=1,
                row_ids=[row_id],
            )
            in_scope = row_id in page.row_ids
        if not in_scope:
            raise NamedIndexAnchorError(
                "embedding_anchor_scope_mismatch",
                "the selected row is outside the embedding index source scope",
            )
    except (IndexScopeError, SheetRowSetError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, NamedIndexAnchorError):
            raise
        if isinstance(exc, IndexScopeError):
            raise NamedIndexAnchorError(exc.code, exc.message) from exc
        raise NamedIndexAnchorError("invalid_query_spec", str(exc)) from exc
    space = metadata.get_space(index["space_id"])
    if space is None:
        raise NamedIndexAnchorError(
            "embedding_space_mismatch", "the embedding index space is missing"
        )
    payloads = build_source_payloads(
        project, index, [name for name, _identifier in columns], [row_id]
    )
    if len(payloads) != 1:
        raise NamedIndexAnchorError(
            "embedding_anchor_not_found", "the selected row has no embeddable source"
        )
    _recipe, items = plan_named_index_items(
        project, index=index, space=space, payloads=payloads
    )
    item = items[0]
    with RetrievalStore(project) as retrieval:
        stored = dataset_rows(retrieval, index_id)
    if stored.get(row_id) != (item.part_id, item.representation_id):
        raise NamedIndexAnchorError(
            "embedding_source_stale",
            "the selected row is not current in this embedding index",
        )
    return ResolvedNamedIndexAnchor(
        anchor=SourceAnchor(
            source=item.source,
            locator=item.locator,
            binding=ScopedBinding(
                binding_id=item.binding.binding_id,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=None,
                dependencies=tuple(
                    BindingRevision(
                        row_id=dependency.row_id,
                        column_id=dependency.column_id,
                        retrieval_revision=dependency.retrieval_revision,
                    )
                    for dependency in item.binding.dependencies
                ),
            ),
            exclude_exact=True,
        ),
        space_id=str(space["id"]),
    )


def named_encoder_input(
    project: Any, payload: Mapping[str, Any], descriptor: Mapping[str, Any]
) -> str | bytes:
    modality = descriptor["modality"]
    if modality in {"text", "row"}:
        value = payload["payload"]
        if not isinstance(value, str):
            raise TypeError("text payload is not a string")
        return value
    if modality == "image":
        media = payload["source_ref"].get("media") or []
        if len(media) != 1:
            raise ValueError("image input requires one immutable blob")
        blob_hash = media[0].get("blob_hash")
        if not isinstance(blob_hash, str) or not media[0].get("materializable"):
            raise ValueError("image blob is not materializable")
        data = project.read_blob(blob_hash)
        import hashlib

        if hashlib.sha256(data).hexdigest() != blob_hash:
            raise ValueError("image blob digest mismatch")
        if data.startswith(b"%PDF-"):
            raise ValueError("PDF is not an image embedding input")
        return data
    raise ValueError(
        f"embedding modality {modality!r} is not supported by named indexes"
    )


def read_named_members(
    store: RetrievalStore, *, dataset_id: str, space_id: str
) -> tuple[NamedDatasetVector, ...]:
    rows: list[NamedDatasetVector] = []
    after: tuple[str, str] | None = None
    while True:
        page = store.read_named_dataset_vectors(
            dataset_id, space_id=space_id, limit=1000, after=after
        )
        rows.extend(page)
        if len(page) < 1000:
            break
        tail = page[-1]
        after = (tail.part_id, tail.representation.representation_id)
    return tuple(rows)


def publish_named_index(
    store: RetrievalStore,
    *,
    index_id: str,
    space_id: str,
    recipe: NamedIndexRecipe,
    items: Sequence[NamedIndexItem],
    preserve_members: Iterable[NamedDatasetMember] = (),
) -> None:
    """Publish all row composites, then atomically expose the complete membership."""

    members = list(preserve_members)
    for item in items:
        publication = SourcePublication(
            source_revision=item.source,
            recipe_id=recipe.recipe_id,
            channel=ROW_CHANNEL,
            space_id=space_id,
            parts=(
                StoredPart(
                    item.part_id,
                    "row_composite",
                    item.locator,
                    group_key=item.source.source_id,
                ),
            ),
            bindings=(item.binding,),
            attachments=(
                PartRepresentation(item.part_id, item.representation_id, ROW_CHANNEL),
            ),
        )
        store.begin_preparation(
            item.source,
            recipe_id=recipe.recipe_id,
            channel=ROW_CHANNEL,
            space_id=space_id,
        )
        if not store.publish_source(publication):
            raise RuntimeError("named-index source publication was superseded")
        members.append(
            NamedDatasetMember(index_id, item.part_id, item.representation_id)
        )
    store.replace_named_dataset_members(index_id, members)


def vector_values(blob: bytes, *, dimension: int) -> tuple[float, ...]:
    validated = validate_vector_blob(
        blob, dimensions=dimension, require_unit_norm=False
    )
    return tuple(value[0] for value in _F32.iter_unpack(validated))


def cached_representation(
    item: NamedIndexItem,
    vector: Sequence[float],
    *,
    space_id: str,
    dimension: int,
):
    """Pack a provider vector under the deterministic named-index cache key."""

    from frisket.retrieval.store import CachedRepresentation

    if len(vector) != dimension:
        raise ValueError(f"expected {dimension} vector dimensions")
    chunks: list[bytes] = []
    for value in vector:
        if isinstance(value, bool):
            raise ValueError("vector values must be numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("vector values must be finite")
        chunks.append(_F32.pack(number))
    blob = b"".join(chunks)
    validate_vector_blob(blob, dimensions=dimension, require_unit_norm=False)
    return CachedRepresentation(
        representation_id=item.representation_id,
        space_id=space_id,
        input_hash=item.input_hash,
        vector=blob,
        dimension=dimension,
    )


def member_pairs(rows: Iterable[NamedDatasetVector]) -> set[tuple[str, str]]:
    return {(row.part_id, row.representation.representation_id) for row in rows}


def dataset_rows(store: RetrievalStore, dataset_id: str) -> dict[int, tuple[str, str]]:
    """Return row bindings and member identities without loading vector blobs."""

    rows = store.db.execute(
        "SELECT binding.row_id,member.part_id,member.representation_id "
        "FROM named_dataset_membership AS member "
        "JOIN parts AS part ON part.part_id=member.part_id "
        "JOIN bindings AS binding ON binding.source_id=part.source_id "
        "AND binding.version_kind=part.version_kind "
        "AND binding.version_value=part.version_value "
        "AND binding.version_type=part.version_type "
        "WHERE member.dataset_id=? ORDER BY binding.row_id,member.part_id",
        (dataset_id,),
    ).fetchall()
    return {
        int(row["row_id"]): (str(row["part_id"]), str(row["representation_id"]))
        for row in rows
    }


def dataset_binding_hash(store: RetrievalStore, dataset_id: str) -> str:
    """Hash named membership and binding revisions without reading vector blobs."""

    digest = hashlib.sha256()
    for row in store.db.execute(
        "SELECT member.part_id,member.representation_id,dependency.row_id,"
        "dependency.column_id,dependency.retrieval_revision "
        "FROM named_dataset_membership AS member "
        "JOIN parts AS part ON part.part_id=member.part_id "
        "JOIN bindings AS binding ON binding.source_id=part.source_id "
        "AND binding.version_kind=part.version_kind "
        "AND binding.version_value=part.version_value "
        "AND binding.version_type=part.version_type "
        "JOIN binding_dependencies AS dependency "
        "ON dependency.binding_id=binding.binding_id "
        "WHERE member.dataset_id=? "
        "ORDER BY member.part_id,member.representation_id,dependency.column_id,"
        "dependency.row_id",
        (dataset_id,),
    ):
        digest.update(
            json.dumps(tuple(row), separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        )
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


__all__ = [
    "NamedIndexItem",
    "NamedIndexAnchorError",
    "NamedIndexRecipe",
    "ResolvedNamedIndexAnchor",
    "ROW_CHANNEL",
    "ROW_RECIPE_CONTRACT",
    "UNIFIED_BACKEND_ID",
    "UNIFIED_BACKEND_VERSION",
    "descriptor_space",
    "cached_representation",
    "dataset_rows",
    "dataset_binding_hash",
    "member_pairs",
    "named_encoder_input",
    "named_index_recipe",
    "ordered_index_columns",
    "plan_named_index_items",
    "publish_named_index",
    "read_named_members",
    "recipe_for_index",
    "resolve_named_cell_anchor",
    "vector_values",
]
