"""Host-owned package evidence and receipts for typed plugin manifest loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from frisket.actions.core import _ProjectAction
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import PluginManifestLoader, PluginManifestRecord
from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.engine.executor.action_inventory import (
    ExecutorContext,
    ExecutorDeps,
    _ActionCoreSpec,
    _TypedProjectEnvelope,
)
from frisket.engine.executor.action_lifecycle import (
    _child_sheet_deterministic_result_from_existing,
    _run_action_core_spec,
)
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.store.receipts import ReceiptStore
from frisket.plugins.load_evidence import (
    PluginLoadEvidence,
    PluginLoadEvidenceError,
    build_plugin_load_evidence,
)


def supports_typed_plugin_load_action(terminal: Any) -> bool:
    return isinstance(terminal, _ProjectAction) and terminal.capabilities == (
        PluginManifestLoader,
    )


class _ManifestLoader:
    def __init__(self, action: _TypedProjectEnvelope, request_hash: str) -> None:
        self.action = action
        self.request_hash = request_hash
        self.calls = 0
        self.evidence: PluginLoadEvidence | None = None
        self.result: PluginManifestRecord | None = None

    def load(self, path: str) -> PluginManifestRecord:
        self.calls += 1
        if self.calls != 1:
            raise ValueError("a plugin action must load exactly one manifest")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("manifest path must be non-empty")
        self.evidence = build_plugin_load_evidence(
            self.action, Path(path), request_hash=self.request_hash
        )
        loaded = self.evidence.loaded
        self.result = PluginManifestRecord(
            plugin_id=loaded.manifest.id,
            version=loaded.manifest.version,
            manifest_sha256=loaded.sha256,
            byte_count=loaded.byte_count,
            contributes=loaded.manifest.contributes.model_dump(mode="json"),
        )
        return self.result


def run_typed_plugin_load_action(
    project: Any, project_id: str, bound: BoundTypedActionRequest
) -> ActionResult:
    terminal = bound.action.definition.run
    if not supports_typed_plugin_load_action(terminal):
        raise TypeError("typed plugin executor requires a manifest loader action")
    envelope = _TypedProjectEnvelope(
        kind=bound.action.action_id,
        idempotency_key=bound.request.idempotency_key,
        params=bound.params.model_dump(mode="json"),
    )
    request_hash = typed_request_hash(bound)

    def evidence_error(error: PluginLoadEvidenceError) -> ActionError:
        return ActionError(
            code=error.code,
            message=error.message,
            action_kind=envelope.kind,
            field=error.field,
            details=error.details or {},
        )

    def write_error(_action: Any = None) -> ActionError:
        return ActionError(
            code="project_write_failed",
            message="The plugin manifest receipt could not be written.",
            action_kind=envelope.kind,
        )

    def resolve(_project: Any, params: Any) -> PluginLoadEvidence | ActionError:
        loader = _ManifestLoader(envelope, request_hash)
        try:
            returned = terminal.handler(params, loader)
            if (
                loader.calls != 1
                or loader.evidence is None
                or loader.result is None
                or not isinstance(returned, PluginManifestRecord)
            ):
                raise ValueError(
                    "plugin action must load one manifest and return its domain result"
                )
            return loader.evidence
        except PluginLoadEvidenceError as error:
            return evidence_error(error)
        except Exception:
            return write_error()

    def validate_replay(_project: Any, receipt: Receipt) -> ActionError | None:
        # The shared lifecycle checks canonical request identity first. The path
        # here is the actual capability argument, not an assumed Params field.
        ref = next(
            (
                item.ref
                for item in receipt.outputs
                if item.ref.get("kind") == "plugin_manifest"
            ),
            None,
        )
        if ref is None or not isinstance(ref.get("path"), str):
            return write_error()
        path = Path(ref["path"])
        if not path.exists() and not path.is_symlink():
            return None
        try:
            current = build_plugin_load_evidence(
                envelope, path, request_hash=request_hash
            )
        except PluginLoadEvidenceError as error:
            return evidence_error(error)
        if current.manifest_ref != ref:
            return ActionError(
                code="idempotency_conflict",
                message="The plugin package changed since this idempotency key was used.",
                action_kind=envelope.kind,
                field="idempotency_key",
            )
        return None

    def perform(
        project_: Any,
        _cur: Any,
        action: Any,
        _params: Any,
        *,
        project_id: str,
        action_id: str,
        receipt_id: str,
        params_hash: str,
        resolved: PluginLoadEvidence,
    ) -> ActionResult:
        ref = resolved.manifest_ref
        receipt = Receipt(
            receipt_id=receipt_id,
            project_id=project_id,
            action_id=action_id,
            action_kind=action.kind,
            idempotency_key=action.idempotency_key,
            params_hash=params_hash,
            status="completed",
            inputs=[ReceiptIO(name="manifest", ref=ref)],
            outputs=[ReceiptIO(name="plugin_manifest", ref=ref)],
            evidence=[ReceiptEvidence(ref=ref, retention="compactable")],
        )
        ReceiptStore(project_).insert_completed(receipt, commit=False)
        return ActionResult(
            action=ActionIdentity(kind=action.kind, action_id=action_id),
            status="completed",
            project_id=project_id,
            outputs=[
                ActionOutput(kind="plugin_manifest", name=ref["plugin_id"], ref=ref)
            ],
            receipt_id=receipt_id,
        )

    spec = _ActionCoreSpec(
        kind=envelope.kind,
        params_model=terminal.params_model,
        body_kind="plain",
        params_hash_fn=lambda _action: request_hash,
        result_from_existing_fn=_child_sheet_deterministic_result_from_existing(
            validate_replay
        ),
        plain_resolve_fn=resolve,
        plain_perform_in_txn_fn=perform,
        plain_exception_error_fn=write_error,
    )
    return _run_action_core_spec(
        project,
        envelope,
        bound.params,
        spec=spec,
        ctx=ExecutorContext(project_id=project_id, deps=ExecutorDeps()),
    )
