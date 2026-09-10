"""Host-owned embedding export staging, delivery, receipts, and replay."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from frisket.actions.core import _ProjectAction
from frisket.actions.embeddings import IndexExportParams
from frisket.actions.types import IndexExport, IndexExportFormat
from frisket.ai.embeddings import EmbeddingStore, VectorBackend
from frisket.ai.embeddings import export as _export
from frisket.ai.embeddings.freshness import (
    FRESHNESS_FRESH,
    freshness_reason,
    index_freshness,
)
from frisket.ai.embeddings.scope import IndexScopeError
from frisket.contracts.action import (
    ActionError,
    ActionResult,
    ActionSpec,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
from frisket.engine.executor.action_receipts import _receipt_ref, _result_from_receipt
from frisket.engine.executor.action_families.exports import (
    _ExportDelivery,
    _deliver_export_artifact,
    _finalize_export_delivery,
    _precheck_export_destination,
    _rollback_export_delivery,
)
from frisket.engine.executor.embedding_read import (
    collect_index_rows,
    index_read_freshness_error,
    _embedding_index_ref,
    _embedding_index_definition_replay_error,
    _embedding_sidecar_items_replay_error,
    _embedding_stale_replay_error,
    _json_obj,
    _sidecar_item_evidence,
)
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.sdk.plain import build_plain_action_run_fn

logger = logging.getLogger("frisket.executor")


# The server export route writes artifacts to this per-project dir as
# {index_id}.embeddings.{fmt} (latest-by-index/format). Kept in one place so the
# delete-time orphan cleanup and the manifest route agree on the layout.
EMBEDDING_EXPORT_SUBDIR = ("exports", "embeddings")


def embedding_export_dir(project: Project) -> Path:
    return project.path.joinpath(*EMBEDDING_EXPORT_SUBDIR)


def _unlink_export_artifacts(project: Project, index_id: str) -> int:
    """Best-effort removal of an index's export artifact files. Never raises (a
    missing dir / unlink error must not fail the delete)."""
    removed = 0
    try:
        for path in embedding_export_dir(project).glob(f"{index_id}.embeddings.*"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    except OSError:
        return removed
    return removed


# --------------------------------------------------------------------------
# embedding.index_export
# --------------------------------------------------------------------------


def _export_provenance(index, space, ready_items: int, total_items: int) -> dict:
    return {
        "kind": "embedding_index_export_provenance",
        "index_id": index["id"],
        "space_id": space["id"],
        "provider_id": space["provider_id"],
        "provider_kind": space["provider_kind"],
        "actual_model_id": space["actual_model_id"],
        "model_revision": space["model_revision"],
        "dimension": int(space["dimension"]),
        "dtype": space["dtype"],
        "distance_metric": space["distance_metric"],
        "normalization": space["normalization"],
        "source_query": _json_obj(index["source_query_json"]),
        "source_columns": json.loads(index["source_columns_json"]),
        "source_policy_hash": index["source_policy_hash"],
        "vector_backend_id": VectorBackend.BACKEND_ID,
        "vector_backend_version": VectorBackend.BACKEND_VERSION,
        "total_items": total_items,
        "ready_items": ready_items,
    }


def _export_items_evidence(
    *,
    backend: VectorBackend,
    index,
    space,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    evidence = {
        "kind": "embedding_index_export_items",
        "index_id": index["id"],
        "space_id": space["id"],
        "backend_id": VectorBackend.BACKEND_ID,
        "backend_version": VectorBackend.BACKEND_VERSION,
        "source_query": _json_obj(index["source_query_json"]),
        "source_columns": json.loads(index["source_columns_json"]),
        "source_policy_hash": index["source_policy_hash"],
    }
    evidence.update(
        _sidecar_item_evidence(
            backend,
            index_id=index["id"],
            space_id=space["id"],
            source_keys=[str(row["source_key"]) for row in rows],
        )
    )
    return evidence


def _embedding_export_replay_error(
    project: Project, receipt: Receipt
) -> ActionError | None:
    index_ref = _embedding_index_ref(receipt)
    provenance = _receipt_ref(receipt, "embedding_index_export_provenance")
    items_ref = _receipt_ref(receipt, "embedding_index_export_items")
    if index_ref is None or provenance is None or items_ref is None:
        return _embedding_stale_replay_error(
            receipt,
            "embedding export replay receipt lacks provenance or item evidence",
        )
    stale = _embedding_index_definition_replay_error(
        project, receipt, index_ref=index_ref, evidence_ref=provenance
    )
    if stale is not None:
        return stale
    store = EmbeddingStore(project)
    index = store.get_index(index_ref["index_id"])
    if index is None:
        return _embedding_stale_replay_error(
            receipt, "embedding export replay index is missing"
        )
    fresh = index_freshness(project, index)
    reason = freshness_reason(index, fresh)
    if reason != FRESHNESS_FRESH:
        return _embedding_stale_replay_error(
            receipt,
            "embedding export replay index is no longer fresh",
            details={"receipt_id": receipt.receipt_id, "freshness_reason": reason},
        )
    output_row_counts = [
        io.ref.get("row_count")
        for io in receipt.outputs
        if io.ref.get("kind") == "export_artifact"
    ]
    if not output_row_counts or any(
        isinstance(count, bool) or not isinstance(count, int)
        for count in output_row_counts
    ):
        return _embedding_stale_replay_error(
            receipt,
            "embedding export replay receipt lacks artifact row-count evidence",
        )
    if any(count != provenance.get("ready_items") for count in output_row_counts):
        return _embedding_stale_replay_error(
            receipt,
            "embedding export replay artifact row counts do not match provenance",
        )
    return _embedding_sidecar_items_replay_error(project, receipt, items_ref)


@dataclass
class _IndexExportResolved:
    """Staged exports and host-read facts, with reversible per-file deliveries."""

    artifacts: list[Any]
    artifact_refs: list[dict[str, Any]]
    provenance: dict[str, Any]
    export_item_evidence: dict[str, Any]
    index: Any
    space: Any
    include_vectors: bool
    deliveries: list[_ExportDelivery]


def index_export_replay_error(
    project: Project, receipt: Receipt, *, artifact_path: Path | None = None
) -> ActionError | None:
    """Validate artifact bytes before their recorded index snapshot.

    HTTP downloads select one receipt-member artifact; action replay checks all.
    A newer export may legitimately replace a sibling format at the same path.
    """
    refs = [
        io.ref
        for io in receipt.outputs
        if io.ref.get("kind") == "export_artifact"
        and (artifact_path is None or io.ref.get("path") == str(artifact_path))
    ]
    if not refs:
        return ActionError(
            code="embedding_export_artifact_missing",
            message="export receipt does not contain the requested artifact",
            action_kind=receipt.action_kind,
            field="params",
        )
    for ref in refs:
        if _export.sha256_file(str(ref.get("path"))) != ref.get("sha256"):
            return ActionError(
                code="embedding_export_artifact_missing",
                message=f"prior export artifact {ref.get('path')!r} is missing or changed",
                action_kind=receipt.action_kind,
                field="params",
            )
    return _embedding_export_replay_error(project, receipt)


def _resolve_index_export(
    project: Project, params: IndexExportParams
) -> _IndexExportResolved | ActionError:
    """The pre-txn resolve (`plain_resolve_fn`): read the index/space, validate the sidecar +
    destination + ready vectors + source freshness, collect the export rows, and stage the
    export artifacts as tmp files in the destination dir. Returns an `_IndexExportResolved`
    holder (artifacts + precomputed receipt material) or an `ActionError` for any failure
    before the txn opens. Mirrors the pre-txn section of the original handler."""
    store = EmbeddingStore(project)
    index = store.get_index(params.index_id)
    if index is None:
        return ActionError(
            code="embedding_index_not_found",
            message=f"no embedding index {params.index_id!r}",
            action_kind="embedding.index_export",
            field="params.index_id",
        )
    space = store.get_space(index["space_id"])
    if space is None:
        return ActionError(
            code="embedding_index_not_found",
            message=f"embedding index {params.index_id!r} references a missing space",
            action_kind="embedding.index_export",
            field="params.index_id",
        )

    if not (project.path / "project.embeddings.db").exists():
        return ActionError(
            code="embedding_export_sidecar_missing",
            message="project.embeddings.db sidecar does not exist; refresh the index first",
            action_kind="embedding.index_export",
            field="params.index_id",
        )

    dest_dir = Path(params.destination.path)
    if not dest_dir.is_absolute():
        dest_dir = project.path / dest_dir
    if not dest_dir.is_dir():
        return ActionError(
            code="invalid_export_destination",
            message=f"destination {params.destination.path!r} is not an existing directory",
            action_kind="embedding.index_export",
            field="params.destination.path",
        )
    dest_dir = dest_dir.resolve()

    backend = VectorBackend(project)
    backend.ensure_schema()
    try:
        counts = backend.item_counts(params.index_id)
        total_items = sum(counts.values())
        ready_items = counts.get("ready", 0)
        if ready_items == 0:
            return ActionError(
                code="embedding_export_no_ready_vectors",
                message=f"index {params.index_id!r} has no ready vectors to export",
                action_kind="embedding.index_export",
                field="params.index_id",
            )
        # Authoritative preflight: the export must reflect the CURRENT source scope,
        # not just the ready rows. Use the same freshness contract as the index list
        # + watches (index_freshness/freshness_reason): block on changed ready rows
        # (embedding_source_stale), on missing/errored rows the export would silently
        # omit (embedding_index_incomplete), or an unresolvable scope
        # (embedding_index_scope_invalid). Never write a partial/stale artifact.
        block = index_read_freshness_error(project, index, "embedding.index_export")
        if block is not None:
            return block
        source_columns = json.loads(index["source_columns_json"])
        rows = collect_index_rows(
            project,
            backend,
            index,
            source_columns,
            include_vectors=params.include_vectors,
        )
        export_item_evidence = _export_items_evidence(
            backend=backend,
            index=index,
            space=space,
            rows=rows,
        )
        base_name = f"{params.index_id}.embeddings"
        for fmt in params.formats:
            destination_error = _precheck_export_destination(
                dest_dir / f"{base_name}.{fmt}", action_kind="embedding.index_export"
            )
            if destination_error is not None:
                return destination_error
        artifacts = _export.write_export_artifacts(
            rows,
            dest_dir=dest_dir,
            base_name=base_name,
            formats=params.formats,
            include_vectors=params.include_vectors,
        )
    except IndexScopeError as exc:
        return ActionError(
            code=exc.code,
            message=exc.message,
            action_kind="embedding.index_export",
            field="params.index_id",
        )
    except Exception:
        logger.debug(
            "action_failed",
            exc_info=True,
            extra={"event": "action_failed", "action_kind": "embedding.index_export"},
        )
        return ActionError(
            code="project_write_failed",
            message="embedding export artifacts could not be written",
            action_kind="embedding.index_export",
        )
    finally:
        backend.close()

    artifact_refs = [
        {
            "kind": "export_artifact",
            "export_kind": "embedding_index_export",
            "format": art.format,
            "path": art.path,
            "byte_count": art.byte_count,
            "sha256": art.sha256,
            "row_count": art.row_count,
        }
        for art in artifacts
    ]
    provenance = _export_provenance(index, space, len(rows), total_items)
    return _IndexExportResolved(
        artifacts=artifacts,
        artifact_refs=artifact_refs,
        provenance=provenance,
        export_item_evidence=export_item_evidence,
        index=index,
        space=space,
        include_vectors=params.include_vectors,
        deliveries=[],
    )


def _perform_index_export_in_txn(
    project: Project,
    cur: Any,
    action: ActionSpec,
    params: Any,
    *,
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
    resolved: _IndexExportResolved,
) -> ActionResult:
    """Commit host-read evidence and reversible deliveries under the plain lifecycle."""
    index = resolved.index
    space = resolved.space
    artifact_refs = resolved.artifact_refs
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
                    "index_id": index["id"],
                    "space_id": space["id"],
                    "source_policy_hash": index["source_policy_hash"],
                },
            )
        ],
        outputs=[
            ReceiptIO(name=f"export_{art['format']}", ref=art) for art in artifact_refs
        ],
        provider_use=[
            {
                "provider": space["provider_id"],
                "service": "embedding.index_export",
                "external_api": False,
                "cost_actual": 0.0,
            }
        ],
        evidence=[
            ReceiptEvidence(ref=resolved.provenance, retention="pinned"),
            ReceiptEvidence(ref=resolved.export_item_evidence, retention="pinned"),
        ],
        exports=artifact_refs,
    )
    ReceiptStore(project).insert_finished(receipt, commit=False)
    for art in resolved.artifacts:
        resolved.deliveries.append(
            _deliver_export_artifact(Path(art.tmp_path), Path(art.path))
        )
    return _result_from_receipt(receipt)


def _cleanup_index_export(resolved: _IndexExportResolved) -> None:
    for i in reversed(range(len(resolved.artifacts))):
        artifact = resolved.artifacts[i]
        _rollback_export_delivery(
            delivery=resolved.deliveries[i] if i < len(resolved.deliveries) else None,
            tmp_path=Path(artifact.tmp_path),
            destination=Path(artifact.path),
        )


def _finalize_index_export(resolved: _IndexExportResolved) -> None:
    for delivery in resolved.deliveries:
        _finalize_export_delivery(delivery)


def _index_export_exception_error(action: ActionSpec) -> ActionError:
    """The in-txn exception seam (`exception_error_fn`): the rollback error the original
    handler surfaced when the receipt insert / artifact move could not be committed."""
    return ActionError(
        code="project_write_failed",
        message="embedding export receipt/artifacts could not be committed",
        action_kind=action.kind,
    )


class _IndexExportRefused(Exception):
    def __init__(self, error: ActionError):
        self.error = error


class _IndexExporter:
    def __init__(self, project: Project):
        self.project = project
        self.resolved: _IndexExportResolved | None = None

    def export(
        self,
        index_id: str,
        *,
        path: str,
        formats: Iterable[IndexExportFormat] = ("jsonl",),
        include_vectors: bool = True,
    ) -> IndexExport:
        if self.resolved is not None:
            raise RuntimeError("index exporter may be called only once")
        try:
            arguments = IndexExportParams.model_validate(
                {
                    "index_id": index_id,
                    "destination": {"kind": "local_dir", "path": path},
                    "formats": formats,
                    "include_vectors": include_vectors,
                }
            )
        except ValidationError:
            raise _IndexExportRefused(
                ActionError(
                    code="invalid_params", message="index export arguments are invalid"
                )
            ) from None
        resolved = _resolve_index_export(self.project, arguments)
        if isinstance(resolved, ActionError):
            raise _IndexExportRefused(resolved)
        self.resolved = resolved
        return IndexExport(
            index_id=resolved.index["id"],
            space_id=resolved.space["id"],
            total_items=resolved.provenance["total_items"],
            ready_items=resolved.provenance["ready_items"],
            exported_rows=resolved.provenance["ready_items"],
            include_vectors=resolved.include_vectors,
            artifacts=tuple(
                {
                    key: value
                    for key, value in ref.items()
                    if key not in {"kind", "export_kind"}
                }
                for ref in resolved.artifact_refs
            ),
        )


def _resolve_typed_index_export(
    project: Project, params: Any, terminal: _ProjectAction, action_kind: str
) -> _IndexExportResolved | ActionError:
    capability = _IndexExporter(project)
    try:
        returned = terminal.handler(params, capability)
        if capability.resolved is None or not isinstance(returned, IndexExport):
            raise TypeError(
                "index export handler must call its capability and return an export"
            )
        return capability.resolved
    except _IndexExportRefused as exc:
        if capability.resolved is not None:
            _cleanup_index_export(capability.resolved)
        return exc.error.model_copy(
            update={"action_kind": action_kind, "field": "params"}
        )
    except BaseException:
        if capability.resolved is not None:
            _cleanup_index_export(capability.resolved)
        raise


def run_index_export(
    project: Project,
    action: _TypedProjectEnvelope,
    params: Any,
    *,
    terminal: _ProjectAction,
    params_hash: str,
    project_id: str,
) -> ActionResult:
    run = build_plain_action_run_fn(
        kind=action.kind,
        params_model=terminal.params_model,
        perform_in_txn_fn=_perform_index_export_in_txn,
        params_hash_fn=lambda _action: params_hash,
        replay_validate_fn=index_export_replay_error,
        resolve_fn=lambda project_, params_: _resolve_typed_index_export(
            project_, params_, terminal, action.kind
        ),
        exception_error_fn=_index_export_exception_error,
        cleanup_resolved_fn=_cleanup_index_export,
        post_commit_fn=_finalize_index_export,
    )
    return run(project, action, params, project_id=project_id)
