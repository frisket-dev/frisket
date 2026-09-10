"""Host-owned embedding reads, freshness, and receipt evidence projection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from frisket.actions.types import EmbeddingVector, RowSource
from frisket.ai.embeddings import EmbeddingStore, VectorBackend, unpack_f32
from frisket.contracts.action import ActionError, Receipt
from frisket.ai.embeddings.freshness import (
    FRESHNESS_FRESH,
    freshness_error_code,
    freshness_reason,
    index_freshness,
)
from frisket.ai.embeddings.scope import IndexScopeError, resolve_index_scope_rows
from frisket.ai.embeddings.spaces import canonical_json
from frisket.engine.executor.action_families._errors import receipt_stale_replay_error
from frisket.engine.executor.action_receipts import _receipt_ref
from frisket.engine.store import Project


class TableReadRefused(Exception):
    def __init__(self, error: ActionError):
        self.error = error


class AdmittedEmbeddingIndexReader:
    def __init__(self, project: Project, action_kind: str):
        self.project = project
        self.action_kind = action_kind
        self.sources: set[RowSource] = set()
        self.facts: list[dict[str, Any]] = []

    def read(
        self, index_id: str, *, minimum_rows: int = 1
    ) -> tuple[EmbeddingVector, ...]:
        if (
            not isinstance(index_id, str)
            or not index_id.strip()
            or type(minimum_rows) is not int
            or minimum_rows < 1
        ):
            raise TableReadRefused(
                ActionError(
                    code="invalid_params",
                    message="read requires a nonempty index ID and positive minimum_rows",
                    action_kind=self.action_kind,
                    field="params",
                )
            )
        result = self._read(index_id, minimum_rows)
        if isinstance(result, ActionError):
            raise TableReadRefused(result.model_copy(update={"field": "params"}))
        return result

    def _read(
        self, index_id: str, minimum_rows: int
    ) -> tuple[EmbeddingVector, ...] | ActionError:
        store = EmbeddingStore(self.project)
        index = store.get_index(index_id)
        if index is None:
            return ActionError(
                code="embedding_index_not_found",
                message=f"no embedding index {index_id!r}",
                action_kind=self.action_kind,
                field="index_id",
            )
        space = store.get_space(index["space_id"])
        if space is None or index["sheet_id"] is None:
            return ActionError(
                code="embedding_space_mismatch",
                message="index has no valid space or source sheet",
                action_kind=self.action_kind,
                field="params",
            )

        backend = VectorBackend(self.project)
        if not backend.db_path.exists():
            return ActionError(
                code="embedding_export_sidecar_missing",
                message=(
                    "the project.embeddings.db sidecar does not exist; refresh the index first"
                ),
                action_kind=self.action_kind,
                field="index_id",
            )
        backend.ensure_schema()
        try:
            # Authoritative freshness preflight (same contract as export/watches): never
            # analyze a stale/incomplete/scope-invalid index.
            freshness = index_read_freshness_error(
                self.project, index, self.action_kind
            )
            if freshness is not None:
                return freshness

            source_columns = json.loads(index["source_columns_json"])
            rows = collect_index_rows(
                self.project, backend, index, source_columns, include_vectors=True
            )
        except IndexScopeError as exc:
            return ActionError(
                code=exc.code,
                message=exc.message,
                action_kind=self.action_kind,
                field="index_id",
            )
        finally:
            backend.close()

        if not rows:
            return ActionError(
                code="embedding_export_no_ready_vectors",
                message=f"index {index_id!r} has no ready vectors to read",
                action_kind=self.action_kind,
                field="index_id",
            )
        dim = int(space["dimension"])
        for row in rows:
            if row["vector"] is None or len(row["vector"]) != dim:
                return ActionError(
                    code="embedding_space_mismatch",
                    message=(
                        f"ready vector {row['source_key']!r} has width "
                        f"{0 if row['vector'] is None else len(row['vector'])} != space "
                        f"dimension {dim}"
                    ),
                    action_kind=self.action_kind,
                    field="index_id",
                    details={
                        "expected_dimension": dim,
                        "source_key": row["source_key"],
                    },
                )

        if len(rows) < minimum_rows:
            return ActionError(
                code="embedding_analysis_insufficient_rows",
                message=f"analysis requires at least {minimum_rows} ready vectors; index has {len(rows)}",
                action_kind=self.action_kind,
                field="params",
                details={"ready": len(rows), "minimum_rows": minimum_rows},
            )
        entries = tuple(
            EmbeddingVector(
                source_key=row["source_key"],
                vector=tuple(row["vector"]),
                source=RowSource(
                    sheet_id=int(index["sheet_id"]), row_id=int(row["row_id"])
                ),
            )
            for row in rows
        )
        self.sources.update(entry.source for entry in entries)
        self.facts.append(
            {
                "kind": "embedding_index_read",
                "index_id": index["id"],
                "space_id": space["id"],
                "provider_id": space["provider_id"],
                "provider_kind": space["provider_kind"],
                "actual_model_id": space["actual_model_id"],
                "model_revision": space["model_revision"],
                "dimension": dim,
                "distance_metric": space["distance_metric"],
                "normalization": space["normalization"],
                "source_policy_hash": index["source_policy_hash"],
                "vector_backend_id": VectorBackend.BACKEND_ID,
                "vector_backend_version": VectorBackend.BACKEND_VERSION,
                "minimum_rows": minimum_rows,
                "row_count": len(entries),
            }
        )
        return entries


def collect_index_rows(
    project: Project,
    backend: VectorBackend,
    index,
    source_columns: list[str],
    *,
    include_vectors: bool,
) -> list[dict[str, Any]]:
    """Ready items joined to their vectors, with each row's CURRENT source values
    (the 'originals'). One query for the items+vectors, one column-fetch per
    source column — no N+1."""
    index_id = index["id"]
    sheet_id = index["sheet_id"]
    current_scope_keys = _current_scope_source_keys(project, index)
    if not current_scope_keys:
        return []
    items = backend.db.execute(
        "SELECT i.source_key AS source_key, i.source_ref_json AS source_ref_json, "
        "i.source_hash AS source_hash, v.vec AS vec "
        "FROM embedding_items i JOIN embedding_vectors_f32 v ON v.id = i.vector_id "
        "WHERE i.index_id=? AND i.status='ready' ORDER BY i.source_key",
        (index_id,),
    ).fetchall()
    row_ids: list[int] = []
    parsed: list[tuple[str, int | None, str, dict, Any]] = []
    for it in items:
        sk = it["source_key"]
        if sk not in current_scope_keys:
            continue
        try:
            rid: int | None = int(sk)
        except (TypeError, ValueError):
            rid = None
        if rid is not None:
            row_ids.append(rid)
        parsed.append(
            (sk, rid, it["source_hash"], _json_obj(it["source_ref_json"]), it["vec"])
        )

    values_by_row: dict[int, dict[str, Any]] = {rid: {} for rid in row_ids}
    if sheet_id is not None and row_ids:
        col_ids = {
            c["name"]: int(c["id"])
            for c in project.columns(sheet_id, include_hidden=True)
        }
        for name in source_columns:
            cid = col_ids.get(name)
            if cid is None:
                continue
            vals = project.get_values(sheet_id, cid, row_ids=row_ids)
            for rid in row_ids:
                if rid in vals:
                    values_by_row[rid][name] = vals[rid]

    rows: list[dict[str, Any]] = []
    for sk, rid, shash, sref, vec in parsed:
        rows.append(
            {
                "source_key": sk,
                "row_id": rid,
                "source_hash": shash,
                "source_ref": sref,
                "values": values_by_row.get(rid, {}) if rid is not None else {},
                "vector": unpack_f32(vec) if include_vectors else None,
            }
        )
    return rows


def _current_scope_source_keys(project: Project, index) -> set[str]:
    return {str(row_id) for row_id in resolve_index_scope_rows(project, index)}


def index_read_freshness_error(
    project: Project, index, action_kind: str
) -> ActionError | None:
    """The freshness gate as an ActionError (shared by the export block and the
    project/cluster resolve seams). Uses the authoritative freshness contract (the same
    index_freshness/freshness_reason the index list and watches use) so it catches BOTH
    changed ready rows and missing/errored current source rows the artifact would silently
    omit. Returns None when fresh."""
    fresh = index_freshness(project, index)
    reason = freshness_reason(index, fresh)
    if reason == FRESHNESS_FRESH:
        return None
    code = freshness_error_code(reason) or "embedding_index_incomplete"
    return ActionError(
        code=code,
        message=f"index is not fresh ({reason}); refresh the index before exporting",
        action_kind=action_kind,
        field="params.index_id",
        details={
            "freshness_reason": reason,
            "stale_source_keys": fresh.stale_keys[:20],
            "missing_source_keys": fresh.missing_keys[:20],
        },
    )


def _embedding_index_ref(receipt: Receipt) -> dict[str, Any] | None:
    return _receipt_ref(receipt, "embedding_index") or _receipt_ref(
        receipt, "embedding_index_ref"
    )


def _embedding_index_definition_replay_error(
    project: Project,
    receipt: Receipt,
    *,
    index_ref: dict[str, Any],
    evidence_ref: dict[str, Any] | None = None,
) -> ActionError | None:
    index_id = index_ref.get("index_id")
    space_id = index_ref.get("space_id")
    if not isinstance(index_id, str) or not isinstance(space_id, str):
        return _embedding_stale_replay_error(
            receipt, "embedding replay receipt has invalid index evidence"
        )
    store = EmbeddingStore(project)
    index = store.get_index(index_id)
    space = store.get_space(space_id)
    if index is None or space is None:
        return _embedding_stale_replay_error(
            receipt,
            "embedding replay index or space is missing",
            details={"receipt_id": receipt.receipt_id, "index_id": index_id},
        )
    if index["space_id"] != space_id:
        return _embedding_stale_replay_error(
            receipt, "embedding replay index points at a different space"
        )
    facts = evidence_ref or {}
    mismatches: list[str] = []
    _compare_field(
        mismatches,
        "provider_id",
        space["provider_id"],
        index_ref.get("provider_id", facts.get("provider_id")),
    )
    _compare_field(
        mismatches,
        "provider_kind",
        space["provider_kind"],
        index_ref.get("provider_kind", facts.get("provider_kind")),
    )
    _compare_field(
        mismatches,
        "actual_model_id",
        space["actual_model_id"],
        index_ref.get("actual_model_id", facts.get("actual_model_id")),
    )
    _compare_field(
        mismatches,
        "dimension",
        int(space["dimension"]),
        index_ref.get("dimension", facts.get("dimension")),
    )
    _compare_field(
        mismatches,
        "distance_metric",
        space["distance_metric"],
        index_ref.get("distance_metric", facts.get("distance_metric")),
    )
    _compare_field(
        mismatches,
        "normalization",
        space["normalization"],
        index_ref.get("normalization", facts.get("normalization")),
    )
    if "dtype" in facts or "dtype" in index_ref:
        _compare_field(
            mismatches,
            "dtype",
            space["dtype"],
            index_ref.get("dtype", facts.get("dtype")),
        )
    if "model_revision" in facts or "model_revision" in index_ref:
        _compare_field(
            mismatches,
            "model_revision",
            space["model_revision"],
            index_ref.get("model_revision", facts.get("model_revision")),
        )
    if "vector_backend_id" in facts:
        _compare_field(
            mismatches,
            "vector_backend_id",
            VectorBackend.BACKEND_ID,
            facts.get("vector_backend_id"),
        )
    if "vector_backend_version" in facts:
        _compare_field(
            mismatches,
            "vector_backend_version",
            VectorBackend.BACKEND_VERSION,
            facts.get("vector_backend_version"),
        )
    _compare_field(
        mismatches,
        "source_policy_hash",
        index["source_policy_hash"],
        index_ref.get("source_policy_hash", facts.get("source_policy_hash")),
    )
    if evidence_ref is not None:
        if "source_query" in evidence_ref:
            _compare_json_field(
                mismatches,
                "source_query",
                index["source_query_json"],
                evidence_ref.get("source_query"),
            )
        if "source_columns" in evidence_ref:
            _compare_json_field(
                mismatches,
                "source_columns",
                index["source_columns_json"],
                evidence_ref.get("source_columns"),
            )
        if "source_policy_hash" in evidence_ref:
            _compare_field(
                mismatches,
                "source_policy_hash",
                index["source_policy_hash"],
                evidence_ref.get("source_policy_hash"),
            )
    if mismatches:
        return _embedding_stale_replay_error(
            receipt,
            "embedding replay index definition drifted",
            details={"receipt_id": receipt.receipt_id, "mismatches": mismatches},
        )
    return None


def _embedding_sidecar_items_replay_error(
    project: Project, receipt: Receipt, items_ref: dict[str, Any]
) -> ActionError | None:
    index_id = items_ref.get("index_id")
    space_id = items_ref.get("space_id")
    if not isinstance(index_id, str) or not isinstance(space_id, str):
        return _embedding_stale_replay_error(
            receipt, "embedding sidecar replay evidence is invalid"
        )
    backend = _open_existing_sidecar(project)
    if backend is None:
        return _embedding_stale_replay_error(
            receipt,
            "embedding sidecar replay cannot open project.embeddings.db",
            details={"receipt_id": receipt.receipt_id, "index_id": index_id},
        )
    try:
        items = items_ref.get("items")
        if isinstance(items, list):
            for expected in items:
                if not isinstance(expected, dict):
                    return _embedding_stale_replay_error(
                        receipt, "embedding sidecar replay item evidence is invalid"
                    )
                actual = _sidecar_item_record(
                    backend, index_id, expected.get("source_key")
                )
                if actual is None or not _sidecar_item_matches(
                    actual, expected, space_id
                ):
                    return _embedding_stale_replay_error(
                        receipt,
                        "embedding sidecar replay item drifted",
                        details={
                            "receipt_id": receipt.receipt_id,
                            "source_key": expected.get("source_key"),
                        },
                    )
            return None
        digest = items_ref.get("items_digest")
        item_count = items_ref.get("item_count")
        if isinstance(digest, str) and isinstance(item_count, int):
            source_keys = items_ref.get("source_keys")
            if not isinstance(source_keys, list):
                return _embedding_stale_replay_error(
                    receipt,
                    "embedding sidecar replay digest lacks source-key evidence",
                    details={"receipt_id": receipt.receipt_id, "index_id": index_id},
                )
            actual_digest, actual_count = _sidecar_items_digest(
                backend, index_id, space_id, source_keys
            )
            if actual_count != item_count or actual_digest != digest:
                return _embedding_stale_replay_error(
                    receipt,
                    "embedding sidecar replay digest drifted",
                    details={"receipt_id": receipt.receipt_id, "index_id": index_id},
                )
            return None
        return _embedding_stale_replay_error(
            receipt,
            "embedding sidecar replay lacks item evidence",
            details={"receipt_id": receipt.receipt_id, "index_id": index_id},
        )
    except (sqlite3.Error, TypeError, ValueError):
        return _embedding_stale_replay_error(
            receipt,
            "embedding sidecar replay schema cannot provide item evidence",
            details={"receipt_id": receipt.receipt_id, "index_id": index_id},
        )
    finally:
        backend.close()


def _compare_field(
    mismatches: list[str], name: str, actual: Any, expected: Any
) -> None:
    if actual != expected:
        mismatches.append(name)


def _compare_json_field(
    mismatches: list[str], name: str, actual_json: str, expected: Any
) -> None:
    try:
        actual = json.loads(actual_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        mismatches.append(name)
        return
    if canonical_json(actual) != canonical_json(expected):
        mismatches.append(name)


def _open_existing_sidecar(project: Project) -> VectorBackend | None:
    backend = VectorBackend(project)
    if not backend.db_path.exists():
        return None
    if not _sidecar_has_replay_schema(backend.db_path):
        return None
    return backend


def _sidecar_has_replay_schema(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('embedding_items', 'embedding_vectors_f32')"
            ).fetchall()
            return {str(row[0]) for row in rows} == {
                "embedding_items",
                "embedding_vectors_f32",
            }
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _vector_sha256(blob: bytes) -> str:
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _sidecar_item_record(
    backend: VectorBackend, index_id: str, source_key: Any
) -> dict[str, Any] | None:
    if not isinstance(source_key, str):
        return None
    row = backend.db.execute(
        "SELECT i.index_id, i.space_id, i.source_key, i.source_ref_json, "
        "i.source_hash, i.status, i.vector_table, i.vector_id, "
        "v.dimension AS dimension, v.vec AS vec "
        "FROM embedding_items i LEFT JOIN embedding_vectors_f32 v ON v.id = i.vector_id "
        "WHERE i.index_id=? AND i.source_key=?",
        (index_id, source_key),
    ).fetchone()
    if row is None:
        return None
    return _sidecar_row_record(row)


def _sidecar_row_record(row) -> dict[str, Any]:
    vec = row["vec"]
    return {
        "index_id": row["index_id"],
        "space_id": row["space_id"],
        "source_key": row["source_key"],
        "source_hash": row["source_hash"],
        "source_ref_json": row["source_ref_json"],
        "source_ref": _json_obj(row["source_ref_json"]),
        "status": row["status"],
        "vector_table": row["vector_table"],
        "vector_id": row["vector_id"],
        "dimension": row["dimension"],
        "vector_sha256": _vector_sha256(vec) if vec is not None else None,
    }


def _sidecar_item_matches(
    actual: dict[str, Any], expected: dict[str, Any], space_id: str
) -> bool:
    # Current VectorBackend only writes f32 vectors. Keep this explicit so a
    # future backend/table change fails closed until replay evidence learns it.
    return (
        actual["space_id"] == space_id
        and actual["vector_table"] == "embedding_vectors_f32"
        and actual["source_key"] == expected.get("source_key")
        and actual["source_hash"] == expected.get("source_hash")
        and canonical_json(actual["source_ref"])
        == canonical_json(expected.get("source_ref"))
        and actual["status"] == expected.get("status")
        and actual["vector_table"] == expected.get("vector_table")
        and actual["vector_id"] == expected.get("vector_id")
        and actual["dimension"] == expected.get("dimension")
        and actual["vector_sha256"] == expected.get("vector_sha256")
    )


def _sidecar_item_evidence(
    backend: VectorBackend,
    *,
    index_id: str,
    space_id: str,
    source_keys: list[str],
) -> dict[str, Any]:
    keys = sorted({str(key) for key in source_keys})
    if not keys:
        items: list[dict[str, Any]] = []
    else:
        placeholders = ",".join("?" for _ in keys)
        rows = backend.db.execute(
            "SELECT i.index_id, i.space_id, i.source_key, i.source_ref_json, "
            "i.source_hash, i.status, i.vector_table, i.vector_id, "
            "v.dimension AS dimension, v.vec AS vec "
            "FROM embedding_items i JOIN embedding_vectors_f32 v ON v.id = i.vector_id "
            f"WHERE i.index_id=? AND i.source_key IN ({placeholders}) "
            "ORDER BY i.source_key",
            (index_id, *keys),
        ).fetchall()
        items = [_sidecar_row_record(row) for row in rows]
    if len(items) != len(keys):
        raise ValueError("embedding sidecar evidence requires ready vector rows")
    if len(items) <= 1000:
        return {"source_keys": keys, "items": [_public_sidecar_item(i) for i in items]}
    digest, count = _sidecar_items_digest(backend, index_id, space_id, keys)
    return {
        "source_keys": keys,
        "item_count": count,
        "items_digest": digest,
    }


def _public_sidecar_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_key": item["source_key"],
        "source_hash": item["source_hash"],
        "source_ref": item["source_ref"],
        "status": item["status"],
        "vector_table": item["vector_table"],
        "vector_id": item["vector_id"],
        "vector_sha256": item["vector_sha256"],
        "dimension": item["dimension"],
    }


def _sidecar_items_digest(
    backend: VectorBackend,
    index_id: str,
    space_id: str,
    source_keys: Any = None,
) -> tuple[str, int]:
    params: list[Any] = [index_id, space_id]
    key_sql = ""
    if isinstance(source_keys, list):
        keys = sorted(str(key) for key in source_keys)
        if not keys:
            return "sha256:" + hashlib.sha256(b"[]").hexdigest(), 0
        key_sql = f"AND i.source_key IN ({','.join('?' for _ in keys)}) "
        params.extend(keys)
    rows = backend.db.execute(
        "SELECT i.source_key, i.source_hash, i.source_ref_json, i.vector_id, "
        "v.dimension, v.vec "
        "FROM embedding_items i JOIN embedding_vectors_f32 v ON v.id = i.vector_id "
        "WHERE i.index_id=? AND i.space_id=? AND i.status='ready' "
        f"{key_sql}"
        "ORDER BY i.source_key",
        params,
    ).fetchall()
    tuples = [
        [
            row["source_key"],
            row["source_hash"],
            canonical_json(_json_obj(row["source_ref_json"])),
            row["vector_id"],
            _vector_sha256(row["vec"]),
            row["dimension"],
        ]
        for row in rows
    ]
    digest = hashlib.sha256(canonical_json(tuples).encode("utf-8")).hexdigest()
    return f"sha256:{digest}", len(tuples)


def _embedding_stale_replay_error(
    receipt: Receipt,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> ActionError:
    return receipt_stale_replay_error(
        receipt,
        message,
        details=details,
        include_receipt_id=not details,
    )


def _json_obj(value: Any) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
