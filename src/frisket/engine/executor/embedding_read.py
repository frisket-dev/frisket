"""Host-owned named-index reads and replay evidence over unified retrieval."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from frisket.actions.types import EmbeddingVector, RowSource
from frisket.ai.embeddings import EmbeddingStore
from frisket.ai.embeddings.freshness import (
    FRESHNESS_FRESH,
    freshness_error_code,
    freshness_reason,
    index_freshness,
)
from frisket.ai.embeddings.scope import IndexScopeError, resolve_index_scope_rows
from frisket.ai.embeddings.source_payload import build_source_payloads
from frisket.ai.embeddings.spaces import canonical_json
from frisket.ai.embeddings.unified_index import (
    UNIFIED_BACKEND_ID,
    UNIFIED_BACKEND_VERSION,
    plan_named_index_items,
    read_named_members,
    vector_values,
)
from frisket.contracts.action import ActionError, Receipt
from frisket.engine.executor.action_receipts import _receipt_ref
from frisket.engine.store import Project
from frisket.retrieval.store import RetrievalStore


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
        metadata = EmbeddingStore(self.project)
        index = metadata.get_index(index_id)
        if index is None:
            return ActionError(
                code="embedding_index_not_found",
                message=f"no embedding index {index_id!r}",
                action_kind=self.action_kind,
                field="index_id",
            )
        space = metadata.get_space(index["space_id"])
        if space is None or index["sheet_id"] is None:
            return ActionError(
                code="embedding_space_mismatch",
                message="index has no valid space or source sheet",
                action_kind=self.action_kind,
                field="params",
            )
        if not RetrievalStore(self.project).path.is_file():
            return ActionError(
                code="embedding_export_sidecar_missing",
                message="the project.retrieval.db sidecar does not exist; refresh the index first",
                action_kind=self.action_kind,
                field="index_id",
            )
        freshness = index_read_freshness_error(self.project, index, self.action_kind)
        if freshness is not None:
            return freshness
        try:
            rows = collect_index_rows(
                self.project,
                index,
                json.loads(index["source_columns_json"]),
                include_vectors=True,
            )
        except IndexScopeError as exc:
            return ActionError(
                code=exc.code,
                message=exc.message,
                action_kind=self.action_kind,
                field="index_id",
            )
        except (TypeError, ValueError):
            return ActionError(
                code="embedding_space_mismatch",
                message="a ready vector does not match the index space dimension",
                action_kind=self.action_kind,
                field="index_id",
                details={"expected_dimension": int(space["dimension"])},
            )
        if not rows:
            return ActionError(
                code="embedding_export_no_ready_vectors",
                message=f"index {index_id!r} has no ready vectors to read",
                action_kind=self.action_kind,
                field="index_id",
            )
        dimension = int(space["dimension"])
        if any(
            row["vector"] is None or len(row["vector"]) != dimension for row in rows
        ):
            return ActionError(
                code="embedding_space_mismatch",
                message="a ready vector does not match the index space dimension",
                action_kind=self.action_kind,
                field="index_id",
                details={"expected_dimension": dimension},
            )
        if len(rows) < minimum_rows:
            return ActionError(
                code="embedding_analysis_insufficient_rows",
                message=(
                    f"analysis requires at least {minimum_rows} ready vectors; "
                    f"index has {len(rows)}"
                ),
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
                "dimension": dimension,
                "distance_metric": space["distance_metric"],
                "normalization": space["normalization"],
                "source_policy_hash": index["source_policy_hash"],
                "vector_backend_id": UNIFIED_BACKEND_ID,
                "vector_backend_version": UNIFIED_BACKEND_VERSION,
                "minimum_rows": minimum_rows,
                "row_count": len(entries),
            }
        )
        return entries


def collect_index_rows(
    project: Project,
    index: Any,
    source_columns: list[str],
    *,
    include_vectors: bool,
) -> list[dict[str, Any]]:
    """Read exact current named members and join current source values in batches."""

    scope_rows = resolve_index_scope_rows(project, index)
    payloads = build_source_payloads(project, index, source_columns, scope_rows)
    space = EmbeddingStore(project).get_space(index["space_id"])
    if space is None:
        return []
    _recipe, planned = plan_named_index_items(
        project, index=index, space=space, payloads=payloads
    )
    with RetrievalStore(project) as retrieval:
        stored = {
            (row.part_id, row.representation.representation_id): row
            for row in read_named_members(
                retrieval,
                dataset_id=str(index["id"]),
                space_id=str(space["id"]),
            )
        }
    row_ids = [item.row_id for item in planned]
    values_by_row: dict[int, dict[str, Any]] = {row_id: {} for row_id in row_ids}
    column_ids = {
        str(column["name"]): int(column["id"])
        for column in project.columns(int(index["sheet_id"]), include_hidden=True)
    }
    for name in source_columns:
        column_id = column_ids.get(name)
        if column_id is None:
            continue
        values = project.get_values(int(index["sheet_id"]), column_id, row_ids=row_ids)
        for row_id in row_ids:
            if row_id in values:
                values_by_row[row_id][name] = values[row_id]

    rows: list[dict[str, Any]] = []
    for item in planned:
        named = stored.get((item.part_id, item.representation_id))
        if named is None:
            continue
        representation = named.representation
        rows.append(
            {
                "source_key": item.source_key,
                "row_id": item.row_id,
                "source_hash": item.source_hash,
                "source_ref": dict(item.source_ref),
                "values": values_by_row.get(item.row_id, {}),
                "vector": (
                    list(
                        vector_values(
                            representation.vector,
                            dimension=representation.dimension,
                        )
                    )
                    if include_vectors
                    else None
                ),
                "_vector_blob": representation.vector,
                "representation_id": representation.representation_id,
                "part_id": item.part_id,
                "dimension": representation.dimension,
            }
        )
    return rows


def index_read_freshness_error(
    project: Project, index: Any, action_kind: str
) -> ActionError | None:
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
    metadata = EmbeddingStore(project)
    index = metadata.get_index(index_id)
    space = metadata.get_space(space_id)
    if index is None or space is None or index["space_id"] != space_id:
        return _embedding_stale_replay_error(
            receipt, "embedding replay index or space is missing or changed"
        )
    facts = evidence_ref or {}
    mismatches: list[str] = []
    for name in (
        "provider_id",
        "provider_kind",
        "actual_model_id",
        "distance_metric",
        "normalization",
    ):
        expected = index_ref.get(name, facts.get(name))
        if expected is not None and space[name] != expected:
            mismatches.append(name)
    expected_dimension = index_ref.get("dimension", facts.get("dimension"))
    if expected_dimension is not None and int(space["dimension"]) != expected_dimension:
        mismatches.append("dimension")
    expected_policy = index_ref.get(
        "source_policy_hash", facts.get("source_policy_hash")
    )
    if expected_policy is not None and index["source_policy_hash"] != expected_policy:
        mismatches.append("source_policy_hash")
    if "source_query" in facts and canonical_json(
        _json_obj(index["source_query_json"])
    ) != canonical_json(facts["source_query"]):
        mismatches.append("source_query")
    if "source_columns" in facts and canonical_json(
        json.loads(index["source_columns_json"])
    ) != canonical_json(facts["source_columns"]):
        mismatches.append("source_columns")
    for name, current in (
        ("vector_backend_id", UNIFIED_BACKEND_ID),
        ("vector_backend_version", UNIFIED_BACKEND_VERSION),
    ):
        expected = facts.get(name)
        if expected is not None and expected != current:
            mismatches.append(name)
    if mismatches:
        return _embedding_stale_replay_error(
            receipt,
            "embedding replay index definition drifted",
            details={"receipt_id": receipt.receipt_id, "mismatches": mismatches},
        )
    return None


def _named_item_evidence(
    project: Project,
    *,
    index: Any,
    space: Any,
    source_keys: list[str] | None = None,
) -> dict[str, Any]:
    rows = collect_index_rows(
        project,
        index,
        json.loads(index["source_columns_json"]),
        include_vectors=False,
    )
    if source_keys is not None:
        selected = set(source_keys)
        rows = [row for row in rows if row["source_key"] in selected]
    records = [
        {
            "source_key": row["source_key"],
            "source_hash": row["source_hash"],
            "source_ref": row["source_ref"],
            "status": "ready",
            "vector_table": "representations",
            "vector_id": row["representation_id"],
            "vector_sha256": _vector_sha256(row["_vector_blob"]),
            "dimension": row["dimension"],
            "part_id": row["part_id"],
        }
        for row in rows
    ]
    records.sort(key=lambda item: item["source_key"])
    keys = [record["source_key"] for record in records]
    if len(records) <= 1000:
        return {"evidence_version": 2, "source_keys": keys, "items": records}
    digest = hashlib.sha256(canonical_json(records).encode("utf-8")).hexdigest()
    return {
        "evidence_version": 2,
        "source_keys": keys,
        "item_count": len(records),
        "items_digest": f"sha256:{digest}",
    }


def _embedding_sidecar_items_replay_error(
    project: Project, receipt: Receipt, items_ref: dict[str, Any]
) -> ActionError | None:
    index_id = items_ref.get("index_id")
    space_id = items_ref.get("space_id")
    if not isinstance(index_id, str) or not isinstance(space_id, str):
        return _embedding_stale_replay_error(
            receipt, "embedding replay item evidence is invalid"
        )
    if not RetrievalStore(project).path.is_file():
        return _embedding_stale_replay_error(
            receipt, "embedding replay unified sidecar is missing"
        )
    metadata = EmbeddingStore(project)
    index = metadata.get_index(index_id)
    space = metadata.get_space(space_id)
    if index is None or space is None:
        return _embedding_stale_replay_error(
            receipt, "embedding replay index or space is missing"
        )
    keys = items_ref.get("source_keys")
    if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
        return _embedding_stale_replay_error(
            receipt, "embedding replay lacks source-key evidence"
        )
    try:
        actual = _named_item_evidence(
            project, index=index, space=space, source_keys=keys
        )
    except (KeyError, TypeError, ValueError, IndexScopeError):
        return _embedding_stale_replay_error(
            receipt, "embedding replay cannot resolve unified item evidence"
        )
    expected_items = items_ref.get("items")
    if isinstance(expected_items, list):
        by_key = {item["source_key"]: item for item in actual.get("items", [])}
        for expected in expected_items:
            if not isinstance(expected, dict):
                return _embedding_stale_replay_error(
                    receipt, "embedding replay item evidence is invalid"
                )
            current = by_key.get(expected.get("source_key"))
            if current is None or any(
                canonical_json(current.get(field))
                != canonical_json(expected.get(field))
                for field in (
                    "source_hash",
                    "source_ref",
                    "status",
                    "vector_sha256",
                    "dimension",
                )
            ):
                return _embedding_stale_replay_error(
                    receipt,
                    "embedding replay item drifted",
                    details={"source_key": expected.get("source_key")},
                )
        return None
    if items_ref.get("evidence_version") != 2:
        return _embedding_stale_replay_error(
            receipt,
            "legacy aggregate vector evidence cannot be revalidated after migration",
        )
    if actual.get("item_count") != items_ref.get("item_count") or actual.get(
        "items_digest"
    ) != items_ref.get("items_digest"):
        return _embedding_stale_replay_error(
            receipt, "embedding replay item digest drifted"
        )
    return None


def _vector_sha256(blob: bytes) -> str:
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _embedding_stale_replay_error(
    receipt: Receipt, message: str, *, details: dict[str, Any] | None = None
) -> ActionError:
    return ActionError(
        code="stale_replay",
        message=message,
        action_kind=receipt.action_kind,
        details=details or {},
    )


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


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
