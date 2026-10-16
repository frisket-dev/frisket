"""Data-preserving migration from the two legacy embedding caches.

The project database remains authoritative for embedding spaces, named-index
definitions, receipts, watches, and source values.  This module only copies a
legacy vector when its complete space and exact current encoder input can be
proved.  It never calls an embedding provider and never removes a legacy file.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
import tempfile
from array import array
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.ai.embeddings.source_payload import build_source_payloads
from frisket.ai.embeddings.unified_index import (
    ROW_CHANNEL,
    ROW_RECIPE_CONTRACT,
    named_encoder_input,
    named_index_recipe,
)
from frisket.ai.embeddings.spaces import (
    STRICT_FIELDS,
    canonical_json as embedding_canonical_json,
    descriptor_hash,
    space_id_for,
    validate_descriptor,
)
from frisket.engine.store.retrieval_lifecycle import read_binding_revisions
from frisket.engine.store.blob_backend import BlobStoreError
from frisket.features.watchlists.specs import (
    canonical_json as query_canonical_json,
    normalize_query_spec,
)
from frisket.querysets import SheetRowSetError, resolve_sheet_filter_rows

from .contracts import SourceLocator
from .sources import (
    identity_digest,
    input_hash,
    part_id,
    representation_id,
    row_composite_source_revision,
)
from .store import (
    BindingDependency,
    CachedRepresentation,
    NamedDatasetMember,
    PartRepresentation,
    RetrievalStore,
    SourcePublication,
    StoredBinding,
    StoredPart,
)


LEGACY_ROW_RECIPE = ROW_RECIPE_CONTRACT
_F32 = struct.Struct("<f")
_NAMED_TABLES = {
    "embedding_items": {
        "index_id",
        "space_id",
        "source_key",
        "source_ref_json",
        "source_hash",
        "status",
        "vector_table",
        "vector_id",
    },
    "embedding_vectors_f32": {"id", "dimension", "vec"},
}


@dataclass(frozen=True, slots=True)
class LegacyCellVectorProof:
    """Exact legacy cache key input and its compatible complete target space.

    ``legacy_model_id`` is the literal model string used by the old
    ``sha1(model_id + "\\0" + text)`` key.  The target space must record that
    exact string as ``actual_model_id``; aliases are deliberately not inferred.
    """

    space_id: str
    legacy_model_id: str
    text: str

    def __post_init__(self) -> None:
        if not self.space_id.strip():
            raise ValueError("space_id must not be empty")
        if not self.legacy_model_id.strip():
            raise ValueError("legacy_model_id must not be empty")


@dataclass(frozen=True, slots=True)
class MigrationMiss:
    reason: str
    count: int


@dataclass(frozen=True, slots=True)
class LegacyCacheMigrationReport:
    named_sidecar_found: bool
    analytical_sidecar_found: bool
    named_scan_complete: bool
    analytical_scan_complete: bool
    named_indexes: int
    named_items: int
    named_imported: int
    analytical_items: int
    analytical_imported: int
    misses: tuple[MigrationMiss, ...]

    @property
    def complete(self) -> bool:
        """Whether every found legacy cache was structurally scanned."""

        return self.named_scan_complete and self.analytical_scan_complete

    @property
    def derived_rows_accounted(self) -> bool:
        """Whether a cleanup controller may advance to its external checks.

        This does not authorize deletion by itself.  The caller must also prove
        that no legacy worker is live and that the unified runtime is ready.
        """

        return self.complete


@dataclass(frozen=True, slots=True)
class _VerifiedSpace:
    space_id: str
    descriptor: Mapping[str, Any]
    dimension: int
    modality: str


@dataclass(frozen=True, slots=True)
class _IndexPlan:
    index: sqlite3.Row
    space: _VerifiedSpace
    sheet_id: int
    columns: tuple[tuple[str, int], ...]
    recipe: Mapping[str, Any]
    recipe_id: str
    query_spec: Mapping[str, Any]


def legacy_cell_vector_key(model_id: str, text: str) -> str:
    """Return the exact content key used by the retired analytical cache."""

    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("legacy model id must not be empty")
    if not isinstance(text, str):
        raise TypeError("legacy analytical input must be text")
    return hashlib.sha1(
        (model_id + "\0" + text).encode("utf-8", errors="replace")
    ).hexdigest()


def migrate_legacy_embedding_caches(
    project: Any,
    retrieval_store: RetrievalStore,
    *,
    cell_vector_proofs: Iterable[LegacyCellVectorProof] = (),
    batch_size: int = 250,
) -> LegacyCacheMigrationReport:
    """Import all provable legacy vectors without spending or changing truth.

    Work is bounded by ``batch_size``.  Named membership is spooled to a
    temporary file and then replaced atomically per index, so a large index is
    never materialized as one Python list.  Deterministic identities and store
    upserts make a completed run idempotent.
    """

    if isinstance(batch_size, bool) or not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be in [1, 1000]")
    # Fail early when the caller forgot to open the target sidecar.
    _ = retrieval_store.db

    misses: Counter[str] = Counter()
    named_path = Path(project.path) / "project.embeddings.db"
    analytical_path = Path(project.path) / "project.search.db"
    named_found = named_path.is_file()
    analytical_found = analytical_path.is_file()
    named_complete = not named_found
    analytical_complete = not analytical_found
    named_indexes = 0
    named_items = 0
    named_imported = 0
    analytical_items = 0
    analytical_imported = 0

    with project.read_snapshot() as snapshot:
        spaces = _verified_spaces(snapshot.db, misses)
        indexes = snapshot.db.execute(
            "SELECT * FROM embedding_indexes ORDER BY id"
        ).fetchall()
        named_indexes = len(indexes)

        if named_found:
            legacy: sqlite3.Connection | None = None
            try:
                legacy = _open_readonly(named_path)
                if not _has_schema(legacy, _NAMED_TABLES):
                    misses["named_sidecar_schema_invalid"] += 1
                else:
                    legacy.execute("BEGIN")
                    legacy.execute(
                        "CREATE TEMP TABLE known_indexes(id TEXT PRIMARY KEY) WITHOUT ROWID"
                    )
                    legacy.executemany(
                        "INSERT INTO known_indexes(id) VALUES (?)",
                        ((str(row["id"]),) for row in indexes),
                    )
                    orphaned = int(
                        legacy.execute(
                            "SELECT count(*) FROM embedding_items item "
                            "LEFT JOIN known_indexes known ON known.id=item.index_id "
                            "WHERE known.id IS NULL"
                        ).fetchone()[0]
                    )
                    misses["named_definition_missing"] += orphaned
                    named_items += orphaned
                    unreferenced_vectors = int(
                        legacy.execute(
                            "SELECT count(*) FROM embedding_vectors_f32 vector "
                            "LEFT JOIN embedding_items item "
                            "ON item.vector_id=vector.id "
                            "WHERE item.vector_id IS NULL"
                        ).fetchone()[0]
                    )
                    misses["named_vector_unreferenced"] += unreferenced_vectors
                    for index in indexes:
                        imported, seen = _migrate_named_index(
                            project,
                            snapshot,
                            retrieval_store,
                            legacy,
                            index,
                            spaces,
                            batch_size,
                            misses,
                        )
                        named_imported += imported
                        named_items += seen
                    named_complete = True
            except (sqlite3.Error, OSError):
                misses["named_sidecar_unreadable"] += 1
            finally:
                if legacy is not None:
                    legacy.close()

        if analytical_found:
            legacy = None
            try:
                legacy = _open_readonly(analytical_path)
                if not _has_schema(legacy, {"cell_vec": {"key", "vec"}}):
                    misses["analytical_sidecar_schema_invalid"] += 1
                else:
                    legacy.execute("BEGIN")
                    analytical_items = int(
                        legacy.execute("SELECT count(*) FROM cell_vec").fetchone()[0]
                    )
                    analytical_imported = _migrate_cell_vectors(
                        legacy,
                        retrieval_store,
                        spaces,
                        cell_vector_proofs,
                        batch_size,
                        misses,
                    )
                    accounted = int(
                        legacy.execute(
                            "SELECT count(*) FROM temp.accounted_cell_vec"
                        ).fetchone()[0]
                    )
                    misses["analytical_input_unproven"] += max(
                        0, analytical_items - accounted
                    )
                    analytical_complete = True
            except (sqlite3.Error, OSError):
                misses["analytical_sidecar_unreadable"] += 1
            finally:
                if legacy is not None:
                    legacy.close()

    return LegacyCacheMigrationReport(
        named_sidecar_found=named_found,
        analytical_sidecar_found=analytical_found,
        named_scan_complete=named_complete,
        analytical_scan_complete=analytical_complete,
        named_indexes=named_indexes,
        named_items=named_items,
        named_imported=named_imported,
        analytical_items=analytical_items,
        analytical_imported=analytical_imported,
        misses=tuple(
            MigrationMiss(reason, count)
            for reason, count in sorted(misses.items())
            if count
        ),
    )


def _migrate_named_index(
    project: Any,
    snapshot: Any,
    store: RetrievalStore,
    legacy: sqlite3.Connection,
    index: sqlite3.Row,
    spaces: Mapping[str, _VerifiedSpace],
    batch_size: int,
    misses: Counter[str],
) -> tuple[int, int]:
    index_id = str(index["id"])
    seen = int(
        legacy.execute(
            "SELECT count(*) FROM embedding_items WHERE index_id=?", (index_id,)
        ).fetchone()[0]
    )
    try:
        plan = _index_plan(snapshot, index, spaces)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        misses["named_definition_invalid"] += seen
        store.replace_named_dataset_members(index_id, ())
        return 0, seen

    imported = 0
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as membership:
        cursor = legacy.execute(
            "SELECT item.*, vector.dimension AS stored_dimension, "
            "vector.vec AS stored_vector FROM embedding_items item "
            "LEFT JOIN embedding_vectors_f32 vector ON vector.id=item.vector_id "
            "WHERE item.index_id=? ORDER BY item.source_key",
            (index_id,),
        )
        while rows := cursor.fetchmany(batch_size):
            imported += _migrate_named_batch(
                project,
                snapshot,
                store,
                plan,
                rows,
                membership,
                misses,
            )
        membership.seek(0)
        store.replace_named_dataset_members(
            index_id, _spooled_members(index_id, membership)
        )
    return imported, seen


def _migrate_named_batch(
    project: Any,
    snapshot: Any,
    store: RetrievalStore,
    plan: _IndexPlan,
    rows: Sequence[sqlite3.Row],
    membership: Any,
    misses: Counter[str],
) -> int:
    parsed: list[tuple[sqlite3.Row, int]] = []
    for row in rows:
        try:
            row_id = int(row["source_key"])
            if row_id <= 0 or str(row_id) != str(row["source_key"]):
                raise ValueError
        except (TypeError, ValueError):
            misses["named_source_key_invalid"] += 1
            continue
        parsed.append((row, row_id))
    if not parsed:
        return 0

    row_ids = [row_id for _row, row_id in parsed]
    try:
        in_scope = _rows_in_scope(snapshot, plan, row_ids)
    except (TypeError, ValueError, SheetRowSetError):
        misses["named_scope_unprovable"] += len(parsed)
        return 0
    payloads = {
        str(payload["source_key"]): payload
        for payload in build_source_payloads(
            snapshot,
            plan.index,
            [name for name, _column_id in plan.columns],
            row_ids,
        )
    }
    requested = [
        (row_id, column_id)
        for _row, row_id in parsed
        for _name, column_id in plan.columns
    ]
    revisions = read_binding_revisions(snapshot.db, requested)

    imported = 0
    for row, row_id in parsed:
        source_key = str(row["source_key"])
        if row_id not in in_scope:
            misses["named_out_of_scope"] += 1
            continue
        payload = payloads.get(source_key)
        if payload is None:
            misses["named_source_empty"] += 1
            continue
        reason = _legacy_named_row_error(row, payload, plan.space)
        if reason is not None:
            misses[reason] += 1
            continue
        dependencies: list[BindingDependency] = []
        member_revisions: list[dict[str, int]] = []
        for _name, column_id in plan.columns:
            revision = revisions.get((row_id, column_id), 0)
            dependencies.append(BindingDependency(row_id, column_id, revision))
            member_revisions.append(
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "retrieval_revision": revision,
                }
            )
        if not dependencies:
            misses["named_binding_revision_missing"] += 1
            continue
        try:
            encoder_input = named_encoder_input(project, payload, plan.space.descriptor)
        except (BlobStoreError, LookupError, OSError, TypeError, ValueError):
            misses["named_input_unprovable"] += 1
            continue
        exact_input_hash = input_hash(encoder_input)
        source = row_composite_source_revision(
            index_id=str(plan.index["id"]),
            source_key=source_key,
            ordered_member_revisions=member_revisions,
            recipe=plan.recipe,
            content_hash=exact_input_hash,
        )
        locator = SourceLocator(kind="row_composite")
        stored_part_id = part_id(source, plan.recipe, locator, kind="row_composite")
        stored_representation_id = representation_id(
            space=plan.space.space_id, input_hash=exact_input_hash
        )
        binding_id = identity_digest(
            "binding",
            {
                "source_id": source.source_id,
                "sheet_id": plan.sheet_id,
                "row_id": row_id,
                "column_id": None,
            },
        )
        publication = SourcePublication(
            source_revision=source,
            recipe_id=plan.recipe_id,
            channel=ROW_CHANNEL,
            parts=(
                StoredPart(
                    stored_part_id,
                    "row_composite",
                    locator,
                    group_key=source.source_id,
                ),
            ),
            bindings=(
                StoredBinding(
                    binding_id,
                    sheet_id=plan.sheet_id,
                    row_id=row_id,
                    column_id=None,
                    dependencies=tuple(dependencies),
                ),
            ),
            representations=(
                CachedRepresentation(
                    stored_representation_id,
                    plan.space.space_id,
                    exact_input_hash,
                    bytes(row["stored_vector"]),
                    plan.space.dimension,
                ),
            ),
            attachments=(
                PartRepresentation(
                    stored_part_id, stored_representation_id, ROW_CHANNEL
                ),
            ),
        )
        store.begin_preparation(source, recipe_id=plan.recipe_id, channel=ROW_CHANNEL)
        if not store.publish_source(publication):
            misses["named_publication_superseded"] += 1
            continue
        membership.write(
            json.dumps(
                [stored_part_id, stored_representation_id], separators=(",", ":")
            )
            + "\n"
        )
        imported += 1
    return imported


def _migrate_cell_vectors(
    legacy: sqlite3.Connection,
    store: RetrievalStore,
    spaces: Mapping[str, _VerifiedSpace],
    proofs: Iterable[LegacyCellVectorProof],
    batch_size: int,
    misses: Counter[str],
) -> int:
    legacy.execute(
        "CREATE TEMP TABLE accounted_cell_vec(key TEXT PRIMARY KEY) WITHOUT ROWID"
    )
    pending: list[CachedRepresentation] = []
    imported = 0
    for proof in proofs:
        key = legacy_cell_vector_key(proof.legacy_model_id, proof.text)
        row = legacy.execute("SELECT vec FROM cell_vec WHERE key=?", (key,)).fetchone()
        if row is None:
            misses["analytical_proof_not_cached"] += 1
            continue
        first = legacy.execute(
            "INSERT OR IGNORE INTO accounted_cell_vec(key) VALUES (?)", (key,)
        ).rowcount
        if not first:
            misses["analytical_duplicate_proof"] += 1
            continue
        space = spaces.get(proof.space_id)
        if space is None:
            misses["analytical_space_unproven"] += 1
            continue
        if space.modality not in {"text", "row"}:
            misses["analytical_space_unproven"] += 1
            continue
        if str(space.descriptor["actual_model_id"]) != proof.legacy_model_id:
            misses["analytical_model_mismatch"] += 1
            continue
        try:
            vector = _native_f32_to_little_endian(bytes(row["vec"]), space.dimension)
            exact_input_hash = input_hash(proof.text)
        except (OverflowError, UnicodeEncodeError, ValueError):
            misses["analytical_vector_invalid"] += 1
            continue
        pending.append(
            CachedRepresentation(
                representation_id(space=space.space_id, input_hash=exact_input_hash),
                space.space_id,
                exact_input_hash,
                vector,
                space.dimension,
            )
        )
        imported += 1
        if len(pending) >= batch_size:
            store.put_representations(pending)
            pending.clear()
    if pending:
        store.put_representations(pending)
    return imported


def _index_plan(
    snapshot: Any,
    index: sqlite3.Row,
    spaces: Mapping[str, _VerifiedSpace],
) -> _IndexPlan:
    space = spaces[str(index["space_id"])]
    sheet_id = int(index["sheet_id"])
    source_columns = _json_list(index["source_columns_json"])
    if not source_columns or any(
        not isinstance(name, str) or not name for name in source_columns
    ):
        raise ValueError("invalid source columns")
    if len(set(source_columns)) != len(source_columns):
        raise ValueError("duplicate source columns")
    by_name = {
        str(column["name"]): int(column["id"])
        for column in snapshot.columns(sheet_id, include_hidden=True)
    }
    columns = tuple((name, by_name[name]) for name in source_columns)
    source_policy = _json_object(index["source_policy_json"])
    expected_policy_hash = (
        "sha256:"
        + hashlib.sha256(
            embedding_canonical_json(source_policy).encode("utf-8")
        ).hexdigest()
    )
    if expected_policy_hash != str(index["source_policy_hash"]):
        raise ValueError("source policy hash mismatch")
    query_spec = _json_object(index["source_query_json"])
    named_recipe = named_index_recipe(
        descriptor=space.descriptor,
        ordered_columns=columns,
        source_policy=source_policy,
        source_policy_hash=expected_policy_hash,
    )
    recipe = named_recipe.value
    return _IndexPlan(
        index=index,
        space=space,
        sheet_id=sheet_id,
        columns=columns,
        recipe=recipe,
        recipe_id=named_recipe.recipe_id,
        query_spec=query_spec,
    )


def _rows_in_scope(snapshot: Any, plan: _IndexPlan, row_ids: Sequence[int]) -> set[int]:
    if not plan.query_spec:
        return set(snapshot.visible_row_ids(plan.sheet_id, list(row_ids)))
    normalized = normalize_query_spec(dict(plan.query_spec))
    if normalized.get("kind") != "sheet.filter":
        raise ValueError("unsupported source query")
    scope = normalized.get("scope") or {}
    if scope.get("sheet_id") != plan.sheet_id:
        raise ValueError("source query sheet mismatch")
    filter_json = query_canonical_json(normalized.get("filter", {}))
    sort_json = (
        query_canonical_json(normalized["sort"]) if "sort" in normalized else None
    )
    page = resolve_sheet_filter_rows(
        snapshot,
        plan.sheet_id,
        filter_=filter_json,
        sort=sort_json,
        limit=len(row_ids),
        row_ids=row_ids,
    )
    return set(page.row_ids)


def _legacy_named_row_error(
    row: sqlite3.Row, payload: Mapping[str, Any], space: _VerifiedSpace
) -> str | None:
    if row["status"] != "ready":
        return "named_not_ready"
    if str(row["space_id"]) != space.space_id:
        return "named_space_mismatch"
    if row["vector_table"] != "embedding_vectors_f32" or row["vector_id"] is None:
        return "named_vector_missing"
    if row["stored_vector"] is None or row["stored_dimension"] is None:
        return "named_vector_missing"
    if int(row["stored_dimension"]) != space.dimension:
        return "named_vector_dimension_mismatch"
    vector = bytes(row["stored_vector"])
    if len(vector) != space.dimension * _F32.size:
        return "named_vector_dimension_mismatch"
    try:
        values = (value[0] for value in _F32.iter_unpack(vector))
        if not all(math.isfinite(value) for value in values):
            return "named_vector_invalid"
    except struct.error:
        return "named_vector_invalid"
    if str(row["source_hash"]) != str(payload["source_hash"]):
        return "named_source_stale"
    try:
        source_ref = json.loads(row["source_ref_json"])
    except (TypeError, json.JSONDecodeError):
        return "named_source_ref_invalid"
    if source_ref != payload["source_ref"]:
        return "named_source_ref_stale"
    return None


def _verified_spaces(
    db: sqlite3.Connection, misses: Counter[str]
) -> dict[str, _VerifiedSpace]:
    out: dict[str, _VerifiedSpace] = {}
    for row in db.execute("SELECT * FROM embedding_spaces ORDER BY id"):
        try:
            descriptor = _json_object(row["descriptor_json"])
            validate_descriptor(descriptor)
            if space_id_for(descriptor) != str(row["id"]):
                raise ValueError
            if descriptor_hash(descriptor) != str(row["descriptor_hash"]):
                raise ValueError
            for field in STRICT_FIELDS:
                if descriptor[field] != row[field]:
                    raise ValueError
            out[str(row["id"])] = _VerifiedSpace(
                str(row["id"]),
                descriptor,
                int(descriptor["dimension"]),
                str(descriptor["modality"]),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            misses["space_definition_invalid"] += 1
    return out


def _native_f32_to_little_endian(blob: bytes, dimension: int) -> bytes:
    if len(blob) != dimension * array("f").itemsize:
        raise ValueError("legacy vector dimension mismatch")
    values = array("f")
    values.frombytes(blob)
    if len(values) != dimension or not all(math.isfinite(value) for value in values):
        raise ValueError("legacy vector is invalid")
    return b"".join(_F32.pack(float(value)) for value in values)


def _spooled_members(dataset_id: str, file: Any) -> Iterator[NamedDatasetMember]:
    for line in file:
        part, representation = json.loads(line)
        yield NamedDatasetMember(dataset_id, part, representation)


def _open_readonly(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    return db


def _has_schema(db: sqlite3.Connection, required: Mapping[str, set[str]]) -> bool:
    for table, columns in required.items():
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            return False
        actual = {
            str(info["name"]) for info in db.execute(f"PRAGMA table_info({table})")
        }
        if not columns <= actual:
            return False
    return True


def _json_object(raw: Any) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def _json_list(raw: Any) -> list[Any]:
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("expected JSON list")
    return value


__all__ = [
    "LegacyCacheMigrationReport",
    "LegacyCellVectorProof",
    "MigrationMiss",
    "migrate_legacy_embedding_caches",
]
