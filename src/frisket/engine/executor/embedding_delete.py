"""Host-owned index deletion; only project metadata and the receipt commit together."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from frisket.actions.core import _ProjectAction
from frisket.actions.types import DeletedEmbeddingIndex
from frisket.ai.embeddings import EmbeddingStore
from frisket.contracts.action import (
    ActionError,
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.engine.executor.embedding_export import (
    embedding_export_dir,
    _unlink_export_artifacts,
)
from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_support import _failed_result
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.retrieval.store import RetrievalStore


class _DeleteRefused(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message


@dataclass(slots=True)
class _DeleteEffects:
    project: Project
    index_id: str | None = None


def _referenced_index_id(raw: Any, *, lens: bool = False) -> str | None:
    try:
        value = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    query = value.get("query") if lens else value
    if not isinstance(query, dict):
        return None
    index_id = query.get("embedding_index_id")
    return index_id.strip() if isinstance(index_id, str) else None


def _require_unreferenced(project: Project, index_id: str) -> None:
    watches = [
        row
        for row in project.watches()
        if bool(row["enabled"]) and _referenced_index_id(row["query"]) == index_id
    ]
    lenses = [
        row
        for row in project.lenses()
        if _referenced_index_id(row["spec"], lens=True) == index_id
    ]
    if not watches and not lenses:
        return
    references = [f"watch {row['id']} ({row['name']})" for row in watches]
    references.extend(f"lens {row['id']} ({row['name']})" for row in lenses)
    preview = ", ".join(references[:5])
    if len(references) > 5:
        preview = f"{preview}, and {len(references) - 5} more"
    raise _DeleteRefused(
        "embedding_index_in_use",
        f"Index {index_id!r} is still used by {preview}. "
        "Remove those saved searches before deleting it.",
    )


def _resolve_delete_effects(project: Project, _params: Any) -> _DeleteEffects:
    return _DeleteEffects(project)


def _finalize_delete_effects(effects: _DeleteEffects) -> None:
    """Remove derived records only after metadata and receipt have committed."""

    if effects.index_id is None:
        return
    with RetrievalStore(effects.project) as retrieval:
        retrieval.replace_named_dataset_members(effects.index_id, ())
    _unlink_export_artifacts(effects.project, effects.index_id)


class _IndexDeleter:
    def __init__(self, project: Project, effects: _DeleteEffects):
        self._project = project
        self._effects = effects
        self._called = False
        self.result: DeletedEmbeddingIndex | None = None

    def delete(self, index_id: str) -> DeletedEmbeddingIndex:
        if self._called:
            raise RuntimeError("index deletion capability may be called only once")
        self._called = True
        if not isinstance(index_id, str) or not index_id.strip():
            raise _DeleteRefused(
                "invalid_params",
                "Index deletion requires a nonempty index ID.",
            )
        store = EmbeddingStore(self._project)
        index = store.get_index(index_id)
        if index is None:
            raise _DeleteRefused(
                "embedding_index_not_found", f"no embedding index {index_id!r}"
            )
        _require_unreferenced(self._project, index_id)
        space_id = index["space_id"]
        with RetrievalStore(self._project) as retrieval:
            deleted_items = int(
                retrieval.db.execute(
                    "SELECT count(*) FROM named_dataset_membership WHERE dataset_id=?",
                    (index_id,),
                ).fetchone()[0]
            )
        try:
            deleted_export_artifacts = sum(
                1
                for path in embedding_export_dir(self._project).glob(
                    f"{index_id}.embeddings.*"
                )
                if path.is_file()
            )
        except OSError:
            deleted_export_artifacts = 0
        store.delete_index(index_id, commit=False)
        space_deleted = store.delete_space_if_orphan(space_id, commit=False)
        self._effects.index_id = index_id
        self.result = DeletedEmbeddingIndex(
            index_id=index_id,
            space_id=space_id,
            deleted_items=deleted_items,
            space_deleted=space_deleted,
            deleted_export_artifacts=deleted_export_artifacts,
        )
        return self.result.model_copy(deep=True)


def _insert_receipt(project: Project, receipt: Receipt) -> None:
    ReceiptStore(project).insert_finished(receipt, commit=False)


def _perform_delete_in_txn(
    project: Project,
    cur: Any,
    action: _TypedProjectEnvelope,
    params: Any,
    *,
    terminal: _ProjectAction,
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
    resolved: Any,
) -> ActionResult:
    del cur
    if not isinstance(resolved, _DeleteEffects):
        raise TypeError("index deletion requires resolved delete effects")
    capability = _IndexDeleter(project, resolved)
    try:
        returned = terminal.handler(params, capability)
    except _DeleteRefused as error:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code=error.code,
                message=error.message,
                action_kind=action.kind,
                field="params",
            ),
        )
    result = capability.result
    if result is None or not isinstance(returned, DeletedEmbeddingIndex):
        raise TypeError(
            "index deletion handler must call its capability and return a deletion"
        )
    # Only admitted calls supply receipt facts, never reconstructed handler claims.
    output_ref = {"kind": "embedding_index_delete", **result.model_dump(mode="json")}
    receipt = Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="completed",
        inputs=[
            ReceiptIO(
                name="embedding_index",
                ref={
                    "kind": "embedding_index_ref",
                    "index_id": result.index_id,
                    "space_id": result.space_id,
                },
            )
        ],
        outputs=[ReceiptIO(name=result.index_id, ref=output_ref)],
        evidence=[ReceiptEvidence(ref=output_ref, retention="pinned")],
    )
    _insert_receipt(project, receipt)
    return _result_from_receipt(receipt)
