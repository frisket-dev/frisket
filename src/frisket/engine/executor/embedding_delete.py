"""Host-owned index deletion; only project metadata and the receipt commit together."""

from __future__ import annotations

from typing import Any

from frisket.actions.core import _ProjectAction
from frisket.actions.types import DeletedEmbeddingIndex
from frisket.ai.embeddings import EmbeddingStore, VectorBackend
from frisket.contracts.action import (
    ActionError,
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.engine.executor.embedding_export import (
    _unlink_export_artifacts,
)
from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_support import _failed_result
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


class _DeleteRefused(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message


class _IndexDeleter:
    def __init__(self, project: Project):
        self._project = project
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
        space_id = index["space_id"]
        backend = VectorBackend(self._project)
        try:
            backend.ensure_schema()
            deleted_items = backend.delete_index(index_id)
        finally:
            backend.close()
        # The sidecar above and filesystem cleanup below are independent writes.
        # A later project/receipt rollback does not restore either of them.
        store.delete_index(index_id, commit=False)
        space_deleted = store.delete_space_if_orphan(space_id, commit=False)
        deleted_export_artifacts = _unlink_export_artifacts(self._project, index_id)
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
    del cur, resolved
    capability = _IndexDeleter(project)
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
