"""Native embedding index lifecycle action family.

Legacy create/refresh actions over the embeddings foundation (frisket.embeddings):

- ``embedding.index_create`` — resolves a provider/model into a strict space and
  creates the index in project.db. Curated models are metadata-only; an uncurated
  remote model makes one consented, capped, metered dimension-discovery probe.
  It never writes vectors (those are the refresh's job).
- ``embedding.index_refresh`` — embeds missing/stale (incremental) or all (full)
  source rows into the project.embeddings.db sidecar under an index-level claim,
  gating remote egress on the index provider policy.

Direct vs queued: this family is the synchronous direct path (a dev/executor
path with full idempotency + receipt + claim discipline). The MapRunner/output-
column queued-action machinery does not fit a sidecar-writing embedder, so the
recurring/source-triggered PRODUCT path instead reuses the generic JobQueue/Worker
in ``frisket.jobs.embeddings`` (on_source_append enqueues a job that calls this
same refresh action over the appended rows). That module owns the recurring path.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import hashlib
from contextlib import ExitStack
from typing import Any


from frisket.actions.core import _ProjectAction
from frisket.actions.embeddings import (
    IndexCreateParams,
    IndexRefreshParams,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    EmbeddingIndexPolicy,
    EmbeddingIndexPolicyUpdater,
    IndexDeleter,
    IndexExporter,
    IndexCreator,
    IndexRefresher,
    CreatedEmbeddingIndex,
    RefreshedEmbeddingIndex,
)
from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    ActionSpec,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.ai.embeddings import (
    REMOTE_PROVIDER_KINDS,
    EmbeddingBackendUnavailable,
    EmbeddingGateway,
    EmbeddingProviderError,
    EmbeddingStore,
    VectorBackend,
    make_space_descriptor,
    resolve_embedding_capability,
)
from frisket.ai.embeddings.capabilities import embedding_model_size_budget_gb
from frisket.ai.embeddings.gateway import provider_normalization
from frisket.ai.embeddings.scope import IndexScopeError, resolve_scope_rows
from frisket.ai.embeddings.source_payload import build_source_payloads
from frisket.ai.embeddings.spaces import canonical_json
from frisket.sdk.plain import build_plain_action_run_fn
from frisket.engine.executor.action_inventory import (
    _TypedProjectEnvelope,
)
from frisket.engine.executor.action_receipts import (
    _receipt_ref,
    _result_from_receipt,
)
from frisket.engine.executor.action_reservations import (
    _receipt_for_idempotency,
    _reserve_running_action_receipt,
    _running_receipt_stale_result,
)
from frisket.engine.executor.action_support import (
    _failed_result,
    _new_id,
)
from frisket.engine.executor.project_run_terminalization import (
    CurrentWriterTerminalAuthority,
    terminalize_project_run,
)
from frisket.execution.attempt import (
    AttemptClaimRefused,
    StaleAttemptWriter,
    set_attempt_state,
)
from frisket.execution.attempt_authority import mint_direct_effect_attempt
from frisket.engine.store import Project
from frisket.engine.store.blob_backend import BlobNotFoundError
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.ai.models.metadata import ModelCallMeta

from frisket.engine.executor.embedding_read import (
    _embedding_index_ref,
    _embedding_index_definition_replay_error,
    _embedding_sidecar_items_replay_error,
    _compare_field,
    _compare_json_field,
    _open_existing_sidecar,
    _sidecar_item_evidence,
    _embedding_stale_replay_error,
    _json_obj,
)
from frisket.engine.executor.embedding_export import (
    embedding_export_dir,
    run_index_export,
)

logger = logging.getLogger("frisket.executor")

# Modalities this slice can actually embed. ``image`` joins text/row via the
# in-process fastembed CLIP image engine; audio/video/file are
# still contract-valid (a space can be created only when their engine is built —
# it is not) but refresh reports embedding_source_unsupported.
_EMBEDDABLE_MODALITIES = frozenset({"text", "row", "image"})

# Media modalities have no local fallback engine; an unavailable one blocks at create.
_MEDIA_MODALITIES = frozenset({"image", "audio", "video", "file"})


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------


def _error(action: ActionSpec, project_id: str, code, message, **kw):
    return _failed_result(
        project_id=project_id,
        action_kind=action.kind,
        error=ActionError(code=code, message=message, action_kind=action.kind, **kw),
    )


def _replay(
    project: Project,
    action: ActionSpec,
    *,
    params_hash: str,
    project_id: str,
) -> ActionResult | None:
    existing = _receipt_for_idempotency(project, action.idempotency_key)
    if existing is None:
        return None
    if existing["params_hash"] != params_hash:
        return _error(
            action,
            project_id,
            "idempotency_conflict",
            "idempotency_key was already used with different normalized params",
            field="idempotency_key",
        )
    receipt = Receipt.model_validate(json.loads(existing["body"]))
    stale = _embedding_lifecycle_replay_error(project, receipt)
    if stale is not None:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=stale,
        )
    return _result_from_receipt(receipt)


def _embedding_lifecycle_replay_error(
    project: Project, receipt: Receipt
) -> ActionError | None:
    # embedding.index_export intentionally uses index_export_replay_error(): artifact
    # bytes have a stronger, format-specific error code before lifecycle stale checks.
    if _receipt_ref(receipt, "embedding_index") is not None:
        return _embedding_create_replay_error(project, receipt)
    if _receipt_ref(receipt, "embedding_index_refresh") is not None:
        return _embedding_refresh_replay_error(project, receipt)
    return None


def _embedding_create_replay_error(
    project: Project, receipt: Receipt
) -> ActionError | None:
    index_ref = _receipt_ref(receipt, "embedding_index")
    source_ref = _receipt_ref(receipt, "embedding_source")
    if index_ref is None or source_ref is None:
        return _embedding_stale_replay_error(
            receipt,
            "embedding create replay receipt lacks index/source evidence",
        )
    index_id = index_ref.get("index_id")
    space_id = index_ref.get("space_id")
    if not isinstance(index_id, str) or not isinstance(space_id, str):
        return _embedding_stale_replay_error(
            receipt, "embedding create replay receipt has invalid index evidence"
        )
    store = EmbeddingStore(project)
    index = store.get_index(index_id)
    space = store.get_space(space_id)
    if index is None or space is None:
        return _embedding_stale_replay_error(
            receipt,
            "embedding create replay index or space is missing",
            details={"receipt_id": receipt.receipt_id, "index_id": index_id},
        )
    if index["space_id"] != space_id:
        return _embedding_stale_replay_error(
            receipt, "embedding create replay index points at a different space"
        )

    mismatches: list[str] = []
    _compare_field(mismatches, "sheet_id", index["sheet_id"], index_ref.get("sheet_id"))
    _compare_json_field(
        mismatches,
        "source_columns",
        index["source_columns_json"],
        source_ref.get("source_columns"),
    )
    if "source_query" not in index_ref:
        return _embedding_stale_replay_error(
            receipt,
            "embedding create replay receipt lacks source-query evidence",
        )
    expected_query = index_ref.get("source_query")
    _compare_json_field(
        mismatches, "source_query", index["source_query_json"], expected_query
    )
    if "source_policy_hash" not in index_ref:
        return _embedding_stale_replay_error(
            receipt,
            "embedding create replay receipt lacks source-policy evidence",
        )
    _compare_field(
        mismatches,
        "source_policy_hash",
        index["source_policy_hash"],
        index_ref.get("source_policy_hash"),
    )
    _compare_field(mismatches, "modality", space["modality"], index_ref.get("modality"))
    _compare_field(
        mismatches,
        "provider_id",
        space["provider_id"],
        index_ref.get("provider_id"),
    )
    _compare_field(
        mismatches,
        "provider_kind",
        space["provider_kind"],
        index_ref.get("provider_kind"),
    )
    expected_model = index_ref.get("space_model_id", index_ref.get("actual_model_id"))
    _compare_field(
        mismatches, "actual_model_id", space["actual_model_id"], expected_model
    )
    _compare_field(
        mismatches, "dimension", int(space["dimension"]), index_ref.get("dimension")
    )
    if mismatches:
        return _embedding_stale_replay_error(
            receipt,
            "embedding create replay metadata drifted",
            details={"receipt_id": receipt.receipt_id, "mismatches": mismatches},
        )
    return None


def _embedding_update_policy_replay_error(
    project: Project, receipt: Receipt
) -> ActionError | None:
    output_ref = _receipt_ref(receipt, "embedding_index_update_policy")
    if output_ref is None:
        return _embedding_stale_replay_error(
            receipt, "embedding policy replay receipt lacks policy output"
        )
    index_id = output_ref.get("index_id")
    space_id = output_ref.get("space_id")
    if not isinstance(index_id, str) or not isinstance(space_id, str):
        return _embedding_stale_replay_error(
            receipt, "embedding policy replay receipt has invalid policy output"
        )
    policy = {
        key: output_ref.get(key) for key in ("maintenance_policy", "provider_policy")
    }
    if (
        any(not isinstance(value, dict) for value in policy.values())
        or len(receipt.inputs) != 1
        or receipt.inputs[0].ref
        != {"kind": "embedding_index_ref", "index_id": index_id, "space_id": space_id}
        or len(receipt.outputs) != 1
        or receipt.outputs[0].name != index_id
        or len(receipt.evidence) != 1
        or receipt.evidence[0].retention != "pinned"
        or receipt.evidence[0].ref != {"kind": "embedding_index_policy", **policy}
    ):
        return _embedding_stale_replay_error(
            receipt, "embedding policy replay receipt has inconsistent policy evidence"
        )
    index = EmbeddingStore(project).get_index(index_id)
    if index is None:
        return _embedding_stale_replay_error(
            receipt,
            "embedding policy replay index is missing",
            details={"receipt_id": receipt.receipt_id, "index_id": index_id},
        )
    mismatches: list[str] = []
    _compare_field(mismatches, "space_id", index["space_id"], space_id)
    _compare_json_field(
        mismatches,
        "maintenance_policy",
        index["maintenance_policy_json"],
        output_ref.get("maintenance_policy"),
    )
    _compare_json_field(
        mismatches,
        "provider_policy",
        index["provider_policy_json"],
        output_ref.get("provider_policy"),
    )
    if mismatches:
        return _embedding_stale_replay_error(
            receipt,
            "embedding policy replay metadata drifted",
            details={"receipt_id": receipt.receipt_id, "mismatches": mismatches},
        )
    return None


def _embedding_delete_replay_error(
    project: Project, receipt: Receipt
) -> ActionError | None:
    output_ref = _receipt_ref(receipt, "embedding_index_delete")
    if output_ref is None:
        return _embedding_stale_replay_error(
            receipt, "embedding delete replay receipt lacks delete output"
        )
    index_id = output_ref.get("index_id")
    space_id = output_ref.get("space_id")
    if not isinstance(index_id, str) or not isinstance(space_id, str):
        return _embedding_stale_replay_error(
            receipt, "embedding delete replay receipt has invalid delete output"
        )
    store = EmbeddingStore(project)
    if store.get_index(index_id) is not None:
        return _embedding_stale_replay_error(
            receipt,
            "embedding delete replay index exists again",
            details={"receipt_id": receipt.receipt_id, "index_id": index_id},
        )
    if (
        output_ref.get("space_deleted") is True
        and store.get_space(space_id) is not None
    ):
        return _embedding_stale_replay_error(
            receipt,
            "embedding delete replay space exists again",
            details={"receipt_id": receipt.receipt_id, "space_id": space_id},
        )
    if _sidecar_has_index_rows(project, index_id):
        return _embedding_stale_replay_error(
            receipt,
            "embedding delete replay sidecar rows exist again",
            details={"receipt_id": receipt.receipt_id, "index_id": index_id},
        )
    try:
        artifacts = list(embedding_export_dir(project).glob(f"{index_id}.embeddings.*"))
    except OSError:
        artifacts = []
    if artifacts:
        return _embedding_stale_replay_error(
            receipt,
            "embedding delete replay export artifacts exist again",
            details={
                "receipt_id": receipt.receipt_id,
                "paths": [str(path) for path in artifacts[:20]],
            },
        )
    return None


def _embedding_refresh_replay_error(
    project: Project, receipt: Receipt
) -> ActionError | None:
    index_ref = _embedding_index_ref(receipt)
    items_ref = _receipt_ref(receipt, "embedding_index_refresh_items")
    output_ref = _receipt_ref(receipt, "embedding_index_refresh")
    if index_ref is None or items_ref is None or output_ref is None:
        return _embedding_stale_replay_error(
            receipt,
            "embedding refresh replay receipt lacks sidecar evidence",
        )
    stale = _embedding_index_definition_replay_error(
        project, receipt, index_ref=index_ref, evidence_ref=items_ref
    )
    if stale is not None:
        return stale
    stale = _embedding_refresh_counts_replay_error(project, receipt, items_ref)
    if stale is not None:
        return stale
    return _embedding_sidecar_items_replay_error(project, receipt, items_ref)


def _embedding_refresh_counts_replay_error(
    project: Project, receipt: Receipt, items_ref: dict[str, Any]
) -> ActionError | None:
    index_id = items_ref.get("index_id")
    if not isinstance(index_id, str):
        return _embedding_stale_replay_error(
            receipt, "embedding refresh replay count evidence is invalid"
        )
    counts = items_ref.get("counts")
    if not isinstance(counts, dict):
        return _embedding_stale_replay_error(
            receipt, "embedding refresh replay lacks count evidence"
        )
    expected_ready = counts.get("ready_items")
    expected_total = counts.get("total_items")
    if (
        isinstance(expected_ready, bool)
        or not isinstance(expected_ready, int)
        or isinstance(expected_total, bool)
        or not isinstance(expected_total, int)
    ):
        return _embedding_stale_replay_error(
            receipt, "embedding refresh replay count evidence is invalid"
        )

    # Whole-index refresh receipts claim aggregate index counts. Row-scoped
    # refreshes validate only their recorded item evidence because later disjoint
    # scoped refreshes may legitimately change whole-index counts.
    if items_ref.get("row_scope") is None:
        backend = _open_existing_sidecar(project)
        if backend is None:
            return _embedding_stale_replay_error(
                receipt,
                "embedding refresh replay cannot open sidecar for count validation",
            )
        try:
            live_counts = backend.item_counts(index_id)
        except sqlite3.Error:
            return _embedding_stale_replay_error(
                receipt,
                "embedding refresh replay sidecar count schema is invalid",
                details={"receipt_id": receipt.receipt_id, "index_id": index_id},
            )
        finally:
            backend.close()
        live_total = sum(int(v) for v in live_counts.values())
        if (
            live_counts.get("ready", 0) != expected_ready
            or live_total != expected_total
        ):
            return _embedding_stale_replay_error(
                receipt,
                "embedding refresh replay sidecar counts drifted",
                details={
                    "receipt_id": receipt.receipt_id,
                    "index_id": index_id,
                    "expected_ready": expected_ready,
                    "actual_ready": live_counts.get("ready", 0),
                },
            )
        index = EmbeddingStore(project).get_index(index_id)
        if (
            index is None
            or index["last_refresh_receipt_id"] is not None
            and index["last_refresh_receipt_id"] != receipt.receipt_id
        ):
            return _embedding_stale_replay_error(
                receipt,
                "embedding refresh replay is no longer the latest whole-index refresh",
                details={"receipt_id": receipt.receipt_id, "index_id": index_id},
            )
    return None


def _source_policy_hash(policy: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(policy).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _sidecar_has_index_rows(project: Project, index_id: str) -> bool:
    path = project.path / "project.embeddings.db"
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT 1 FROM embedding_items WHERE index_id=? LIMIT 1", (index_id,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return True


def _insert_receipt(project: Project, receipt: Receipt) -> None:
    ReceiptStore(project).insert_finished(receipt, commit=False)


def _resolve_modality(modality: str) -> str:
    return "text" if modality == "auto" else modality


# Conservative chars-per-token heuristic for the token-limit guard. Real BPE
# tokenizers average ~4 chars/token for English; using 4 UNDER-estimates token
# counts for dense/CJK text, so this only flags inputs that are very likely
# truncated (no false alarms), without pulling in a tokenizer dependency.
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    """Cheap, dependency-free upper-ish bound on a payload's token count."""
    import math

    return math.ceil(len(text) / _CHARS_PER_TOKEN)


def _count_likely_truncated(payloads: list[dict[str, Any]], max_input_tokens) -> int:
    """How many payloads likely exceed the model's max_input_tokens (and are thus
    silently truncated by the provider). None limit -> guard skipped (0)."""
    if not max_input_tokens:
        return 0
    return sum(
        1
        for item in payloads
        if _estimate_tokens(str(item.get("payload", ""))) > int(max_input_tokens)
    )


class _ImageSourceError(Exception):
    """An image row could not be materialized to real image bytes.

    We never fall back to embedding its text representation, so refresh
    surfaces ``embedding_source_unsupported`` instead of faking a vector.
    """

    def __init__(self, message: str, *, source_key: str):
        super().__init__(message)
        self.source_key = source_key


def _image_input_for(item: dict[str, Any], project: Project, leases: ExitStack) -> str:
    """Lease one image blob for fastembed.ImageEmbedding.

    Source payloads hold only immutable content identity. ``leases`` keeps the
    first media blob alive through the gateway call; missing or malformed bytes
    are an honest error, never a silent text fallback.
    """
    media = (item.get("source_ref") or {}).get("media") or []
    blob_hash = media[0].get("blob_hash") if media else None
    if not isinstance(blob_hash, str) or not media[0].get("materializable"):
        raise _ImageSourceError(
            "image row has no resolvable on-disk blob to embed "
            f"(source_key={item['source_key']!r}); re-import the image",
            source_key=str(item["source_key"]),
        )
    try:
        path = leases.enter_context(project.materialize_blob(blob_hash))
    except (BlobNotFoundError, ValueError) as exc:
        raise _ImageSourceError(
            "image row has no resolvable blob to embed "
            f"(source_key={item['source_key']!r}); re-import the image",
            source_key=str(item["source_key"]),
        ) from exc
    return str(path)


def _gateway_inputs(
    modality: str,
    to_embed: list[dict[str, Any]],
    *,
    project: Project,
    leases: ExitStack,
) -> list[Any]:
    """The per-modality input list handed to ``gateway.embed``. Text/row send the
    joined text payload; image sends resolved on-disk blob paths (the blob store
    resolution the gateway itself cannot do — it has no project)."""
    if modality == "image":
        return [_image_input_for(item, project, leases) for item in to_embed]
    return [item["payload"] for item in to_embed]


# Triggers that run a refresh unattended (no human at the keyboard). Remote
# egress on these needs a second opt-in beyond allow_remote, plus a cost ceiling.
AUTOMATIC_REFRESH_TRIGGER_KINDS = frozenset({"schedule", "source_run_completed"})


def _is_automatic_refresh(params: IndexRefreshParams) -> bool:
    trigger = params.trigger_ref or {}
    return trigger.get("trigger_kind") in AUTOMATIC_REFRESH_TRIGGER_KINDS


def _is_positive_finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _automatic_cost_gate(action, params, index, space, project_id, *, units: int):
    """Cost/privacy gate for automatic remote refresh, BEFORE any provider call.

    Embedding unit pricing is not modeled yet, so we never fabricate a cost. The
    honest gate: an unattended remote refresh that would embed >0 rows must carry
    an explicit ``max_cost_usd_per_refresh`` ceiling on the provider policy — that
    ceiling IS the operator's confirmation that they accept the (unknown) cost.
    Missing ceiling blocks with embedding_cost_requires_confirmation."""
    if units <= 0 or not _is_automatic_refresh(params):
        return None
    if space["provider_kind"] not in REMOTE_PROVIDER_KINDS:
        return None
    provider_policy = _json_obj(index["provider_policy_json"])
    cost_cap = provider_policy.get("max_cost_usd_per_refresh")
    if not _is_positive_finite_number(cost_cap):
        return _error(
            action,
            project_id,
            "embedding_cost_requires_confirmation",
            (
                "automatic remote refresh requires provider policy "
                "max_cost_usd_per_refresh; embedding cost is unknown, so set a "
                "ceiling to confirm the spend before egress"
            ),
            field="params.index_id",
            details={"units": units, "cost_source": "unknown"},
        )
    return None


class _RefreshScopeError(Exception):
    """A refresh row-scope (stored source_query or params.row_scope) could not be
    resolved. Carries the typed action error code to surface before claim/provider."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _scope_rows(project: Project, sheet_id: int, spec: dict[str, Any]) -> list[int]:
    """Ordered visible row ids for a scope spec, mapping the shared resolver's typed
    IndexScopeError onto the refresh action's _RefreshScopeError (surfaced before
    any claim or provider call)."""
    try:
        return resolve_scope_rows(project, sheet_id, spec)
    except IndexScopeError as exc:
        raise _RefreshScopeError(exc.code, exc.message) from exc


def _resolve_refresh_rows(
    project: Project, index, params: IndexRefreshParams
) -> list[int]:
    """Rows to consider for this refresh: the stored index source_query, narrowed
    by params.row_scope (intersection preserving the stored scope's order). Both
    must target the index's sheet."""
    sheet_id = index["sheet_id"]
    source_query = _json_obj(index["source_query_json"])
    row_scope = params.row_scope
    if sheet_id is None:
        if source_query or row_scope:
            raise _RefreshScopeError(
                "invalid_query_spec",
                "index has no sheet scope to resolve a query against",
            )
        return []
    base = _scope_rows(project, sheet_id, source_query)
    if row_scope:
        base = [
            row_id
            for row_id in base
            if row_id in _narrow_set(project, sheet_id, row_scope)
        ]
    return base


def _narrow_set(project: Project, sheet_id: int, row_scope: dict[str, Any]) -> set[int]:
    """The narrowing set for params.row_scope. Two shapes:

    - ``{"kind": "row_ids", "row_ids": [...]}`` — an explicit id list (how the
      on_source_append trigger scopes a refresh to just the appended rows);
    - otherwise a sheet.filter QuerySpec resolved against the index sheet.

    The caller intersects this with the stored scope, so ids outside the index
    scope are dropped."""
    if row_scope.get("kind") == "row_ids":
        try:
            return {int(row_id) for row_id in row_scope.get("row_ids") or []}
        except (TypeError, ValueError) as exc:
            raise _RefreshScopeError(
                "invalid_query_spec", "row_scope row_ids must be integers"
            ) from exc
    return set(_scope_rows(project, sheet_id, row_scope))


def _validate_create_scope(
    project: Project,
    action: ActionSpec,
    params: IndexCreateParams,
    project_id: str,
) -> ActionResult | None:
    """Reject an unknown sheet or missing source columns BEFORE any metadata or
    receipt is written. Only checkable when a concrete sheet is targeted."""
    sheet_id = params.sheet_id
    if sheet_id is None:
        return None
    if not any(int(sheet["id"]) == sheet_id for sheet in project.sheets()):
        return _error(
            action,
            project_id,
            "invalid_input_ref",
            f"sheet_id {sheet_id} is not a visible sheet",
            field="params.sheet_id",
        )
    source_names = params.source_columns
    names = {c["name"] for c in project.columns(sheet_id, include_hidden=True)}
    missing = [name for name in source_names if name not in names]
    if missing:
        return _error(
            action,
            project_id,
            "invalid_input_ref",
            f"source columns not found on sheet {sheet_id}: {missing}",
            field="params.source_columns",
            details={"missing": missing},
        )
    return None


# --------------------------------------------------------------------------
# embedding.index_create
# --------------------------------------------------------------------------


def _embedding_create_existing_state(
    project: Project,
    action: ActionSpec,
    existing: Any,
    *,
    params_hash: str,
    project_id: str,
) -> ActionResult | dict[str, Any]:
    """Resolve a terminal create receipt or a paid-probe reconciliation point."""
    if existing["params_hash"] != params_hash:
        return _error(
            action,
            project_id,
            "idempotency_conflict",
            "idempotency_key was already used with different normalized params",
            field="idempotency_key",
        )
    receipt = Receipt.model_validate(json.loads(existing["body"]))
    if receipt.status != "running":
        if receipt.status != "completed":
            return _result_from_receipt(receipt)
        replay = _replay(
            project,
            action,
            params_hash=params_hash,
            project_id=project_id,
        )
        if replay is None:  # pragma: no cover - existing was just read
            raise RuntimeError("embedding create receipt disappeared during replay")
        return replay

    egress_reservation = next(
        (
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "embedding_dimension_probe_egress_reservation"
        ),
        None,
    )
    checkpoint = next(
        (
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "embedding_dimension_probe_checkpoint"
        ),
        None,
    )
    if checkpoint is None:
        if egress_reservation is not None:
            return _error(
                action,
                project_id,
                "external_effect_reconciliation_required",
                (
                    "a prior embedding dimension probe may have reached the "
                    "provider; refusing another paid probe without operator "
                    "reconciliation"
                ),
                field="idempotency_key",
                details={
                    "receipt_id": receipt.receipt_id,
                    "run_id": receipt.run_id,
                    "attempt_id": egress_reservation.get("attempt_id"),
                    "provider": egress_reservation.get("provider_id"),
                    "model": egress_reservation.get("requested_model"),
                    "reconciliation_required": True,
                    "retryable": False,
                },
            )
        stale = _running_receipt_stale_result(
            project,
            existing,
            params_hash=params_hash,
            project_id=project_id,
            action=action,
        )
        if stale is not None:
            return stale
        return _error(
            action,
            project_id,
            "idempotency_in_progress",
            (
                "idempotency_key is already reserved by a running "
                "embedding.index_create action"
            ),
            field="idempotency_key",
            details={"receipt_id": receipt.receipt_id},
        )
    fact = next(
        (
            item.ref
            for item in receipt.evidence
            if item.ref.get("fact_version") == "frisket.model-call-fact.v1"
        ),
        None,
    )
    if (
        receipt.run_id is None
        or not receipt.op_ids
        or not isinstance(fact, dict)
        or fact.get("fact_version") != "frisket.model-call-fact.v1"
    ):
        return _error(
            action,
            project_id,
            "idempotency_checkpoint_invalid",
            "paid embedding probe checkpoint is incomplete; refusing another call",
            field="idempotency_key",
            details={"receipt_id": receipt.receipt_id},
        )
    return {"receipt": receipt, "checkpoint": checkpoint, "provider_fact": fact}


def _reserve_embedding_create_probe(
    project: Project,
    action: ActionSpec,
    *,
    params_hash: str,
    project_id: str,
) -> ActionResult | dict[str, Any]:
    return _reserve_running_action_receipt(
        project,
        action,
        params_hash=params_hash,
        project_id=project_id,
        reservation_kind="embedding_dimension_probe_reservation",
        result_from_existing_fn=_embedding_create_existing_state,
    )


def _delete_embedding_probe_reservation(project: Project, receipt_id: str) -> None:
    ReceiptStore(project).delete_running(receipt_id)


def _mark_embedding_probe_egress_reserved(
    project: Project,
    *,
    receipt_id: str,
    run_id: int,
    op_id: int,
    attempt_id: str,
    provider_id: str,
    requested_model: str | None,
) -> None:
    """Bind the running receipt to the direct-effect writer before egress.

    A bare running receipt is eligible for generic stale cleanup.  Once a paid
    provider call can begin, that would be unsafe: a process death can leave
    the provider outcome ambiguous while the stale-receipt path later clears
    the only idempotency fence.  Persist this marker first so every replay
    refuses another probe until an operator reconciles the original attempt.
    A successful provider return replaces it with the stronger returned
    checkpoint below.
    """

    stored = ReceiptStore(project).find_by_id(receipt_id)
    if stored is None:
        raise RuntimeError("embedding probe reservation disappeared before egress")
    receipt = stored.parsed()
    evidence = [
        item
        for item in receipt.evidence
        if item.ref.get("kind") != "embedding_dimension_probe_egress_reservation"
    ]
    evidence.append(
        ReceiptEvidence(
            ref={
                "kind": "embedding_dimension_probe_egress_reservation",
                "run_id": run_id,
                "op_id": op_id,
                "attempt_id": attempt_id,
                "provider_id": provider_id,
                "requested_model": requested_model,
            },
            retention="pinned",
        )
    )
    receipt = receipt.model_copy(
        update={"run_id": run_id, "op_ids": [op_id], "evidence": evidence}
    )
    try:
        project.db.execute("BEGIN IMMEDIATE")
        landed = ReceiptStore(project).update_body_status(
            receipt,
            require_status="running",
            commit=False,
        )
        if not landed:
            raise RuntimeError("embedding probe reservation was lost before egress")
        project.db.commit()
    except BaseException:
        project.db.rollback()
        raise


def _run_paid_embedding_dimension_probe(
    project: Project,
    action: ActionSpec,
    params: IndexCreateParams,
    *,
    cap: dict[str, Any],
    modality: str,
    project_id: str,
    params_hash: str,
    action_id: str,
    receipt_id: str,
    router: Any | None,
    embedding_gateway: Any | None,
) -> ActionResult | dict[str, Any]:
    from frisket.engine.runner.validation import (
        ProviderKeyRefusal,
        assert_provider_spend_cap,
    )

    try:
        assert_provider_spend_cap(project, str(cap["provider_id"]))
    except ProviderKeyRefusal as exc:
        _delete_embedding_probe_reservation(project, receipt_id)
        return _error(
            action,
            project_id,
            exc.error_code,
            exc.action_message(),
            field="params.provider",
            details=exc.details,
        )

    try:
        op_id = project.append_op(
            "embedding.index_create",
            {"action_id": action.kind, "params": dict(action.params)},
            label="Probe embedding model dimension",
        )
        run_store = RunResultStore(project)
        run_id = run_store.start_run(
            op_id,
            int(params.sheet_id),
            action.kind,
            model=str(params.model or cap["model_id"]),
            params=action.params,
            total_rows=1,
        )
    except Exception:
        _delete_embedding_probe_reservation(project, receipt_id)
        return _error(
            action,
            project_id,
            "project_write_failed",
            "embedding probe accounting envelope could not be written",
        )

    probe_row_ids = project.visible_row_ids(int(params.sheet_id))[:1]
    try:
        commitment = mint_direct_effect_attempt(
            project,
            action_kind=action.kind,
            run_id=run_id,
            scope=tuple(probe_row_ids),
        )
    except AttemptClaimRefused as exc:
        run_store.finish_run(run_id, "failed")
        _delete_embedding_probe_reservation(project, receipt_id)
        return _error(
            action,
            project_id,
            "project_write_failed",
            str(exc),
            field="params.model",
            details={"retryable": True},
        )
    attempt_id = commitment.attempt_id

    try:
        _mark_embedding_probe_egress_reserved(
            project,
            receipt_id=receipt_id,
            run_id=run_id,
            op_id=op_id,
            attempt_id=attempt_id,
            provider_id=str(cap["provider_id"]),
            requested_model=params.model,
        )
    except Exception:
        project.db.execute("BEGIN IMMEDIATE")
        run_store.finish_run(run_id, "failed", commit=False)
        set_attempt_state(project, attempt_id, "halted", commit=False)
        ReceiptStore(project).delete_running(receipt_id, commit=False)
        project.db.commit()
        return _error(
            action,
            project_id,
            "project_write_failed",
            "embedding probe egress reservation could not be written",
        )

    gateway = embedding_gateway or EmbeddingGateway(router=router)
    try:
        probe = gateway.embed(
            ["dimension probe"],
            provider=cap["provider_id"],
            model=params.model,
            modality=modality,
        )
    except EmbeddingBackendUnavailable as exc:
        project.db.execute("BEGIN IMMEDIATE")
        run_store.finish_run(run_id, "failed", commit=False)
        set_attempt_state(project, attempt_id, "halted", commit=False)
        ReceiptStore(project).delete_running(receipt_id, commit=False)
        project.db.commit()
        return _error(
            action,
            project_id,
            "embedding_probe_failed",
            (
                f"could not discover the dimension for {cap['provider_id']}/"
                f"{params.model!r}: {exc}"
            ),
            field="params.model",
            details={"provider": cap["provider_id"], "model": params.model},
        )
    except EmbeddingProviderError as exc:
        # The remote gateway wraps every provider/transport/response failure in
        # this type.  The provider may have accepted and charged the request,
        # so preserve the running receipt plus dispatching attempt.  The
        # pre-egress marker makes exact retries refuse forever instead of
        # becoming eligible for generic stale-receipt cleanup and re-buying.
        return _error(
            action,
            project_id,
            "embedding_probe_failed",
            (
                f"could not discover the dimension for {cap['provider_id']}/"
                f"{params.model!r}: {exc}"
            ),
            field="params.model",
            details={
                "provider": cap["provider_id"],
                "model": params.model,
                "reconciliation_required": True,
                "retryable": False,
            },
        )
    except Exception:
        # An unclassified failure may have happened after remote acceptance.
        # Keep both the reservation and the dispatching attempt visible for
        # reconciliation; clearing either would authorize a blind re-buy.
        raise

    probe_for_fact = dict(probe)
    probe_for_fact.setdefault("provider_id", cap["provider_id"])
    probe_for_fact.setdefault("provider_kind", cap["provider_kind"])
    probe_for_fact.setdefault("actual_model_id", params.model or str(cap["model_id"]))
    provider_fact = _embedding_provider_fact(probe_for_fact)
    if provider_fact is None:  # pragma: no cover - non-None result above
        run_store.finish_run(run_id, "failed")
        return _error(
            action,
            project_id,
            "embedding_probe_failed",
            "dimension probe returned no durable provider accounting fact",
            field="params.model",
        )
    try:
        dimension = int(probe["dimension"])
    except (KeyError, TypeError, ValueError):
        dimension = 0
    if dimension <= 0:
        # The provider returned, so preserve the paid fact even though its payload
        # cannot mint an index. Keep the reservation: an exact retry must refuse a
        # second unknown/invalid probe rather than silently buy it again.
        project.db.execute("BEGIN IMMEDIATE")
        _persist_embedding_provider_fact(
            project,
            run_store=run_store,
            run_id=run_id,
            fact=provider_fact,
            sheet_id=int(params.sheet_id),
            row_ids=probe_row_ids,
            commit=False,
            writer_attempt_id=attempt_id,
            claimless_direct_effect=True,
            authorized_attempt_id=attempt_id,
        )
        run_store.finish_run(run_id, "failed", commit=False)
        set_attempt_state(project, attempt_id, "effected", commit=False)
        project.db.commit()
        return _error(
            action,
            project_id,
            "embedding_probe_failed",
            (
                f"probe for {cap['provider_id']}/{params.model!r} returned no "
                "usable dimension"
            ),
            field="params.model",
            details={"provider": cap["provider_id"], "model": params.model},
        )

    actual_model_id = probe.get("actual_model_id") or (params.model or "")
    checkpoint_ref = {
        "kind": "embedding_dimension_probe_checkpoint",
        "operation_args_sha256": _source_policy_hash(dict(action.params)),
        "provider_id": cap["provider_id"],
        "provider_kind": cap["provider_kind"],
        "requested_model": params.model,
        "actual_model_id": actual_model_id,
        "space_model_id": params.model or actual_model_id,
        "dimension": dimension,
        "probe_input_count": 1,
    }
    checkpoint_receipt = Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="running",
        run_id=run_id,
        op_ids=[op_id],
        inputs=[
            ReceiptIO(
                name="source",
                ref={
                    "kind": "embedding_source",
                    "sheet_id": params.sheet_id,
                    "source_columns": params.source_columns,
                    "modality": modality,
                },
            )
        ],
        provider_use=[
            {
                "provider": cap["provider_id"],
                "service": "embedding.index_create.dimension_probe",
                "external_api": True,
                "cost_actual": _provider_fact_cost_actual(provider_fact),
            }
        ],
        evidence=[
            ReceiptEvidence(ref=checkpoint_ref, retention="pinned"),
            ReceiptEvidence(ref=provider_fact, retention="pinned"),
        ],
    )
    try:
        project.db.execute("BEGIN IMMEDIATE")
        _persist_embedding_provider_fact(
            project,
            run_store=run_store,
            run_id=run_id,
            fact=provider_fact,
            sheet_id=int(params.sheet_id),
            row_ids=probe_row_ids,
            commit=False,
            writer_attempt_id=attempt_id,
            claimless_direct_effect=True,
            authorized_attempt_id=attempt_id,
        )
        landed = ReceiptStore(project).update_body_status(
            checkpoint_receipt, require_status="running", commit=False
        )
        if not landed:
            raise RuntimeError("embedding probe reservation was lost")
        set_attempt_state(project, attempt_id, "effected", commit=False)
        project.db.commit()
    except BaseException as exc:
        project.db.rollback()
        if isinstance(exc, StaleAttemptWriter):
            raise
        # The provider has returned. Even if the checkpoint write itself breaks,
        # preserve its call fact and cost; the bare reservation then refuses a
        # second egress rather than guessing that the first call was free.
        try:
            _persist_embedding_provider_fact(
                project,
                run_store=run_store,
                run_id=run_id,
                fact=provider_fact,
                sheet_id=int(params.sheet_id),
                row_ids=probe_row_ids,
                writer_attempt_id=attempt_id,
                claimless_direct_effect=True,
                authorized_attempt_id=attempt_id,
            )
            project.db.execute("BEGIN IMMEDIATE")
            run_store.finish_run(run_id, "failed", commit=False)
            set_attempt_state(project, attempt_id, "effected", commit=False)
            project.db.commit()
        except BaseException:
            # The durable egress reservation still fences retries if accounting
            # itself fails. Never leave its partial transaction for a later commit.
            project.db.rollback()
            raise
        if not isinstance(exc, Exception):
            raise
        return _error(
            action,
            project_id,
            "project_write_failed",
            "embedding probe reconciliation checkpoint could not be written",
        )
    return {
        "receipt": checkpoint_receipt,
        "checkpoint": checkpoint_ref,
        "provider_fact": provider_fact,
    }


def _create_index(
    project: Project,
    action: ActionSpec,
    params: IndexCreateParams,
    *,
    project_id: str,
    params_hash: str,
    router: Any | None = None,
    embedding_gateway: Any | None = None,
) -> ActionResult:
    checkpoint_state: dict[str, Any] | None = None
    existing = _receipt_for_idempotency(project, action.idempotency_key)
    if existing is not None:
        existing_state = _embedding_create_existing_state(
            project,
            action,
            existing,
            params_hash=params_hash,
            project_id=project_id,
        )
        if isinstance(existing_state, ActionResult):
            return existing_state
        checkpoint_state = existing_state

    if checkpoint_state is not None and checkpoint_state["checkpoint"].get(
        "operation_args_sha256"
    ) != _source_policy_hash(dict(action.params)):
        return _error(
            action,
            project_id,
            "idempotency_checkpoint_invalid",
            "the actual embedding creation arguments no longer match the paid probe checkpoint",
            field="idempotency_key",
        )

    modality = _resolve_modality(params.modality)
    # Capability facts are declared (model id, dimension) regardless of runtime
    # availability — an index may be created now and refreshed once keys exist.
    cap = resolve_embedding_capability(
        modality=modality,
        provider=params.provider,
        model=params.model,
        router=router,
    )
    if cap is None:
        # An explicitly-requested model that resolves to NOTHING is an unsupported
        # model id (not a built-in, not fastembed-supported, and — for remote
        # providers — not an OpenRouter/custom-remote id), distinct from "no
        # provider/backend matches the modality at all".
        if params.model:
            return _error(
                action,
                project_id,
                "embedding_model_unsupported",
                (
                    f"model {params.model!r} is not a built-in or fastembed-supported "
                    "model; choose a listed model or a remote provider/model"
                ),
                field="params.model",
                details={"provider": params.provider, "model": params.model},
            )
        return _error(
            action,
            project_id,
            "embedding_backend_unavailable",
            f"no embedding provider/model matches modality={modality!r} "
            f"provider={params.provider!r} model={params.model!r}",
            field="params.provider",
        )
    # Media modalities have NO local fallback (a remote text key can fill a text
    # index later, but an image/audio/video/file engine that isn't built can never
    # refresh). So a media index whose engine is unavailable is blocked AT CREATE
    # with the disabled reason, rather than minting an un-refreshable index. Text/row
    # keep the lenient "create now, refresh once installed/keyed" behavior.
    if modality in _MEDIA_MODALITIES and not cap.get("available"):
        return _error(
            action,
            project_id,
            "embedding_backend_unavailable",
            (
                f"{modality} embedding engine {cap['model_id']!r} is not available: "
                f"{cap.get('error')}"
            ),
            field="params.modality",
            details={"disabled_reason": cap.get("error"), "modality": modality},
        )

    # Validate the visible sheet + source columns BEFORE any write AND before any
    # discovery probe: an unknown sheet / missing column must leave 0 spaces and must
    # NOT egress a probe embed for a request that was always invalid.
    scope_error = _validate_create_scope(project, action, params, project_id)
    if scope_error is not None:
        return scope_error

    # Dimension-discovery (custom remote / OpenRouter): the engine has no fixed
    # dimension in the registry, so it is discovered with ONE short probe embed
    # whose width the adapter reports. Defaults below are overwritten by the probe;
    # curated engines keep their registry dimension + requested model id.
    discovery_required = bool(cap.get("dimension_discovery_required")) or (
        checkpoint_state is not None
    )
    actual_model_id = cap["model_id"]
    # The model id recorded on the SPACE (refresh re-sends it verbatim). For curated
    # engines this is the registry id; the discovery branch overrides it with the
    # requested id (what the remote provider actually accepts).
    space_model_id = cap["model_id"]
    probe_used = False
    probe_input_count = 0
    discovered_dimension: int | None = None
    probe_op_id: int | None = None
    probe_run_id: int | None = None
    probe_run_store: RunResultStore | None = None
    probe_fact: dict[str, Any] | None = None
    reserved_action_id: str | None = None
    reserved_receipt_id: str | None = None
    if discovery_required:
        provider_policy = params.provider_policy or {"allow_remote": False}
        # CREATE-TIME egress gate, BEFORE any probe call: a discovery probe egresses
        # one short text to a remote provider, so it must be confirmed first. Cost is
        # negligible (one short text) and embedding unit pricing is not modeled for
        # arbitrary remote ids, so the gate is on EGRESS, not cost. No probe call and
        # no space are made when this blocks. (Refresh has its own allow_remote gate.)
        if provider_policy.get("allow_remote") is not True:
            return _error(
                action,
                project_id,
                "embedding_remote_confirmation_required",
                (
                    f"provider {cap['provider_id']!r} is remote and the dimension must "
                    "be discovered with a probe embed; its provider policy must set "
                    "allow_remote=true before any data can egress"
                ),
                field="params.provider_policy",
                details={
                    "provider_kind": cap["provider_kind"],
                    "provider_id": cap["provider_id"],
                },
            )
        if checkpoint_state is None:
            if (
                cap["provider_kind"] in REMOTE_PROVIDER_KINDS
                and project.effective_network_policy() == "off"
            ):
                return _error(
                    action,
                    project_id,
                    "network_disabled",
                    "Project network policy blocks the remote embedding dimension probe.",
                    field="params.provider",
                    details={"capability": f"provider:{cap['provider_kind']}"},
                )
            reservation = _reserve_embedding_create_probe(
                project,
                action,
                params_hash=params_hash,
                project_id=project_id,
            )
            if isinstance(reservation, ActionResult):
                return reservation
            if "checkpoint" in reservation:
                checkpoint_state = reservation
            else:
                reserved_action_id = str(reservation["action_id"])
                reserved_receipt_id = str(reservation["receipt_id"])
                probe_result = _run_paid_embedding_dimension_probe(
                    project,
                    action,
                    params,
                    cap=cap,
                    modality=modality,
                    project_id=project_id,
                    params_hash=params_hash,
                    action_id=reserved_action_id,
                    receipt_id=reserved_receipt_id,
                    router=router,
                    embedding_gateway=embedding_gateway,
                )
                if isinstance(probe_result, ActionResult):
                    return probe_result
                checkpoint_state = probe_result

        checkpoint_receipt = checkpoint_state["receipt"]
        checkpoint = checkpoint_state["checkpoint"]
        if checkpoint.get("operation_args_sha256") != _source_policy_hash(
            dict(action.params)
        ):
            return _error(
                action,
                project_id,
                "idempotency_checkpoint_invalid",
                "the actual embedding creation arguments do not match the paid probe checkpoint",
                field="idempotency_key",
            )
        probe_fact = checkpoint_state["provider_fact"]
        reserved_action_id = checkpoint_receipt.action_id
        reserved_receipt_id = checkpoint_receipt.receipt_id
        probe_run_id = int(checkpoint_receipt.run_id)
        probe_op_id = int(checkpoint_receipt.op_ids[0])
        probe_run_store = RunResultStore(project)
        if probe_run_store.get_run(probe_run_id) is None:
            return _error(
                action,
                project_id,
                "idempotency_checkpoint_invalid",
                "paid embedding probe checkpoint references a missing run",
                field="idempotency_key",
                details={"receipt_id": reserved_receipt_id},
            )
        discovered_dimension = int(checkpoint["dimension"])
        actual_model_id = str(checkpoint["actual_model_id"])
        probe_used = True
        probe_input_count = int(checkpoint.get("probe_input_count") or 1)
        dimensions: list[int] | None = [discovered_dimension]
        # The SPACE records the REQUESTED id as its model: that is the id the
        # provider accepts, and refresh re-sends space["actual_model_id"] verbatim.
        # The provider-echoed id (which may be normalized/qualified) is preserved
        # separately in the receipt's probe facts below.
        space_model_id = str(checkpoint["space_model_id"])
    else:
        dimensions = cap["dimensions"]
        if not dimensions:
            return _error(
                action,
                project_id,
                "embedding_source_unsupported",
                f"engine {cap['model_id']!r} has no fixed dimension yet",
                field="params.modality",
            )
    # Size guard: refuse a model whose weights exceed the budget BEFORE any download
    # (the next refresh would otherwise pull GBs / blow up the box).
    size_gb = cap.get("size_gb")
    if size_gb is not None:
        budget = embedding_model_size_budget_gb()
        if size_gb > budget:
            if probe_run_id is not None and probe_run_store is not None:
                probe_run_store.finish_run(probe_run_id, "failed")
            return _error(
                action,
                project_id,
                "embedding_model_too_large",
                f"model {cap['model_id']!r} is {size_gb} GB, over the "
                f"{budget:.1f} GB limit (FRISKET_EMBEDDING_MAX_MODEL_GB)",
                field="params.model",
                details={"size_gb": size_gb, "max_gb": budget},
            )
    descriptor = make_space_descriptor(
        provider_id=cap["provider_id"],
        provider_kind=cap["provider_kind"],
        # requested_model threads the user's id through so refresh re-embeds with
        # the SAME model the space was probed/minted against.
        requested_model=params.model,
        actual_model_id=space_model_id,
        modality=modality,
        dimension=int(dimensions[0]),
        distance_metric=(cap["distance_metrics"] or ["cosine"])[0],
        normalization=provider_normalization(cap["provider_id"]),
        vector_options={},
    )

    action_id = reserved_action_id or _new_id("act")
    receipt_id = reserved_receipt_id or _new_id("receipt")
    store = EmbeddingStore(project)
    try:
        project.db.execute("BEGIN IMMEDIATE")
        existing = (
            ReceiptStore(project).find_by_id(reserved_receipt_id)
            if reserved_receipt_id is not None
            else _receipt_for_idempotency(project, action.idempotency_key)
        )
        if reserved_receipt_id is not None:
            if existing is None or existing["id"] != reserved_receipt_id:
                project.db.rollback()
                return _error(
                    action,
                    project_id,
                    "idempotency_checkpoint_invalid",
                    "paid embedding probe reservation was lost",
                    field="idempotency_key",
                    details={"receipt_id": reserved_receipt_id},
                )
            if existing["status"] != "running":
                project.db.rollback()
                existing_state = _embedding_create_existing_state(
                    project,
                    action,
                    existing,
                    params_hash=params_hash,
                    project_id=project_id,
                )
                if isinstance(existing_state, ActionResult):
                    return existing_state
                return _error(
                    action,
                    project_id,
                    "idempotency_in_progress",
                    "embedding index creation is already being reconciled",
                    field="idempotency_key",
                )
        elif existing is not None:
            project.db.rollback()
            replay = _replay(
                project,
                action,
                params_hash=params_hash,
                project_id=project_id,
            )
            if replay is not None:
                return replay
            raise RuntimeError("embedding create receipt disappeared during replay")
        # commit=False: space + index + receipt commit (or roll back) together.
        space_id = store.create_space(descriptor, commit=False)
        index_id = store.create_index(
            name=params.name or f"{modality}:{','.join(params.source_columns)}",
            space_id=space_id,
            sheet_id=params.sheet_id,
            source_query=params.source_query,
            source_columns=params.source_columns,
            source_policy=params.source_policy or {"kind": "text_cell"},
            maintenance_policy=params.maintenance_policy or {"mode": "manual"},
            provider_policy=params.provider_policy or {"allow_remote": False},
            commit=False,
        )
        output_ref = {
            "kind": "embedding_index",
            "operation_args_sha256": _source_policy_hash(dict(action.params)),
            "index_id": index_id,
            "space_id": space_id,
            "modality": modality,
            "provider_id": cap["provider_id"],
            "provider_kind": cap["provider_kind"],
            "space_model_id": space_model_id,
            # the user's requested id + the actual id the provider used (the probe
            # echo for discovery, else the registry id) + the (discovered) dimension.
            "requested_model": params.model,
            "actual_model_id": actual_model_id,
            "dimension": int(dimensions[0]),
            "sheet_id": params.sheet_id,
            "source_query": params.source_query,
            "source_policy_hash": _source_policy_hash(
                params.source_policy or {"kind": "text_cell"}
            ),
            # probe facts: was a dimension-discovery probe run, and how many inputs.
            "probe_used": probe_used,
            "probe_input_count": probe_input_count,
        }
        receipt = Receipt(
            receipt_id=receipt_id,
            project_id=project_id,
            action_id=action_id,
            action_kind=action.kind,
            idempotency_key=action.idempotency_key,
            params_hash=params_hash,
            status="completed",
            run_id=probe_run_id,
            op_ids=[probe_op_id] if probe_op_id is not None else [],
            inputs=[
                ReceiptIO(
                    name="source",
                    ref={
                        "kind": "embedding_source",
                        "sheet_id": params.sheet_id,
                        "source_columns": params.source_columns,
                        "modality": modality,
                    },
                )
            ],
            outputs=[ReceiptIO(name="embedding_index", ref=output_ref)],
            provider_use=[
                {
                    "provider": cap["provider_id"],
                    "service": "embedding.index_create",
                    "external_api": bool(
                        probe_used and cap["provider_kind"] in REMOTE_PROVIDER_KINDS
                    ),
                    "cost_actual": _provider_fact_cost_actual(probe_fact),
                }
            ],
            evidence=[
                ReceiptEvidence(ref=output_ref, retention="pinned"),
                *(
                    [ReceiptEvidence(ref=probe_fact, retention="pinned")]
                    if probe_fact is not None
                    else []
                ),
            ],
        )
        if reserved_receipt_id is not None:
            landed = ReceiptStore(project).update_body_status(
                receipt, require_status="running", commit=False
            )
            if not landed:
                raise RuntimeError("embedding probe reservation was lost")
        else:
            _insert_receipt(project, receipt)
        if probe_run_id is not None and probe_run_store is not None:
            probe_run_store.finish_run(probe_run_id, "completed", commit=False)
        project.db.commit()
    except BaseException as exc:
        project.db.rollback()
        if isinstance(exc, StaleAttemptWriter):
            raise
        if probe_run_id is not None and probe_run_store is not None:
            probe_run_store.finish_run(probe_run_id, "failed")
        if not isinstance(exc, Exception):
            raise
        logger.debug(
            "action_failed",
            exc_info=True,
            extra={"event": "action_failed", "action_kind": action.kind},
        )
        return _error(
            action,
            project_id,
            "project_write_failed",
            "embedding index metadata could not be written",
        )

    return ActionResult(
        action=ActionIdentity(kind=action.kind, action_id=action_id),
        status="completed",
        project_id=project_id,
        run_id=probe_run_id,
        op_ids=[probe_op_id] if probe_op_id is not None else [],
        outputs=[
            ActionOutput(
                kind="embedding_index",
                name=index_id,
                sheet_id=params.sheet_id,
                ref=output_ref,
            )
        ],
        receipt_id=receipt_id,
    )


# --------------------------------------------------------------------------
# embedding.index_refresh
# --------------------------------------------------------------------------


def _refresh_index(
    project: Project,
    action: ActionSpec,
    params: IndexRefreshParams,
    *,
    project_id: str,
    params_hash: str,
    router: Any | None,
    embedding_gateway: Any | None,
    reserved_action_id: str | None = None,
    reserved_receipt_id: str | None = None,
    skip_replay: bool = False,
) -> ActionResult:
    if not skip_replay:
        replay = _replay(
            project,
            action,
            params_hash=params_hash,
            project_id=project_id,
        )
        if replay is not None:
            return replay

    store = EmbeddingStore(project)
    index = store.get_index(params.index_id)
    if index is None:
        return _error(
            action,
            project_id,
            "embedding_index_not_found",
            f"no embedding index {params.index_id!r}",
            field="params.index_id",
        )
    space = store.get_space(index["space_id"])
    if space is None:
        return _error(
            action,
            project_id,
            "embedding_index_not_found",
            f"embedding index {params.index_id!r} references a missing space",
            field="params.index_id",
        )
    modality = space["modality"]
    if modality not in _EMBEDDABLE_MODALITIES:
        return _error(
            action,
            project_id,
            "embedding_source_unsupported",
            f"modality {modality!r} cannot be embedded in this build",
            field="params.index_id",
        )

    # Remote egress gate — BEFORE any claim or provider call. A remote-backed
    # index without an explicit allow_remote policy blocks here; no data leaves.
    provider_kind = space["provider_kind"]
    provider_policy = _json_obj(index["provider_policy_json"])
    remote = provider_kind in REMOTE_PROVIDER_KINDS
    automatic = _is_automatic_refresh(params)
    # Per-project network gate: a remote-backed index's
    # refresh egresses row content to the provider, but the kind carries no
    # static ``external:*`` tag (a LOCAL-provider refresh is genuinely
    # offline, so a static kind-level tag would be a lie in the other
    # direction). The gate is therefore taught about remote-backed indexes
    # here, at the same pre-claim/pre-provider choke point as the
    # allow_remote policy — which stays the primary consent gate; this one
    # only enforces the project-wide ``network=off`` safety default.
    if remote and project.effective_network_policy() == "off":
        return _error(
            action,
            project_id,
            "network_disabled",
            (
                f"this project's network setting is off; it blocks "
                f"provider:{provider_kind}. Turn network on in the project "
                "settings to refresh this remote-backed index."
            ),
            field="params.index_id",
            details={"capability": f"provider:{provider_kind}"},
        )
    if remote and provider_policy.get("allow_remote") is not True:
        return _error(
            action,
            project_id,
            "embedding_remote_confirmation_required",
            (
                f"index provider_kind={provider_kind!r} is remote; its provider "
                "policy must set allow_remote=true before refresh can egress data"
            ),
            field="params.index_id",
            details={"provider_kind": provider_kind},
        )
    if remote:
        # The refresh calls the same project-owned provider key as chat/media
        # paths, even though embeddings are not a MapRunner LLM recipe.  Check
        # the durable key ledger at this final pre-claim/pre-egress choke point.
        from frisket.engine.runner.validation import (
            ProviderKeyRefusal,
            assert_provider_spend_cap,
        )

        try:
            assert_provider_spend_cap(project, str(space["provider_id"]))
        except ProviderKeyRefusal as exc:
            return _error(
                action,
                project_id,
                exc.error_code,
                exc.action_message(),
                field="params.index_id",
                details=exc.details,
            )
    # Automatic (scheduled / source-triggered) remote refresh needs a SECOND
    # explicit opt-in: allow_remote alone confirms a manual remote refresh, not
    # unattended ones. Blocks here, before claim/provider — no data leaves.
    if (
        remote
        and automatic
        and provider_policy.get("allow_remote_automatic_refresh") is not True
    ):
        return _error(
            action,
            project_id,
            "embedding_remote_confirmation_required",
            (
                "automatic remote refresh requires provider policy "
                "allow_remote_automatic_refresh=true"
            ),
            field="params.index_id",
            details={"provider_kind": provider_kind, "automatic": True},
        )

    # Resolve the row scope (stored source_query narrowed by params.row_scope)
    # BEFORE the claim/provider: an unsupported or mismatched query fails here
    # without holding the index or calling a provider.
    try:
        candidate_row_ids = _resolve_refresh_rows(project, index, params)
    except _RefreshScopeError as exc:
        return _error(
            action, project_id, exc.code, exc.message, field="params.index_id"
        )

    claim = store.acquire_refresh_claim(params.index_id)
    if claim is None:
        return _error(
            action,
            project_id,
            "embedding_index_busy",
            f"embedding index {params.index_id!r} is being refreshed",
            field="params.index_id",
            details={"retryable": True},
        )

    release_status = "error"
    stale_writer = False
    try:
        # Repair is covered by the same claim cleanup as provider work: an
        # interrupted repair must not strand the acquired refresh lease.
        repair_unfinalized_refresh(project, params.index_id)
        result = _do_refresh(
            project,
            action,
            params,
            project_id=project_id,
            store=store,
            index=index,
            space=space,
            modality=modality,
            gateway=embedding_gateway or EmbeddingGateway(router=router),
            params_hash=params_hash,
            row_ids=candidate_row_ids,
            reserved_action_id=reserved_action_id,
            reserved_receipt_id=reserved_receipt_id,
        )
        if result.status == "completed":
            release_status = "ready"
        return result
    except StaleAttemptWriter:
        stale_writer = True
        raise
    except BaseException:
        # A repair can fail before entering _do_refresh's guarded transactions.
        # Do not let the lease-release commit publish its unfinished writes.
        project.db.rollback()
        raise
    finally:
        if not stale_writer:
            store.release_refresh_claim(params.index_id, claim, status=release_status)


def _do_refresh(
    project,
    action,
    params,
    *,
    project_id,
    store,
    index,
    space,
    modality,
    gateway,
    params_hash,
    row_ids,
    reserved_action_id=None,
    reserved_receipt_id=None,
) -> ActionResult:
    from frisket.engine.store.effect_checkpoints import (
        EffectCheckpointRefused,
        EffectCheckpointStore,
    )

    source_columns = json.loads(index["source_columns_json"])
    payloads = build_source_payloads(project, index, source_columns, row_ids)
    backend = VectorBackend(project)
    backend.ensure_schema()
    run_store = RunResultStore(project)
    run_id: int | None = None
    run_finalized = False
    provider_fact: dict[str, Any] | None = None
    attempt_id: str | None = None
    attempt_closed = False
    stale_writer = False
    effect_unit: dict[str, str] | None = None
    checkpoint_store: EffectCheckpointStore | None = None
    try:
        full = params.mode == "full"
        to_embed: list[dict[str, Any]] = []
        skipped = 0
        for item in payloads:
            if not full:
                existing = backend.get_item(params.index_id, item["source_key"])
                if (
                    existing is not None
                    and existing["status"] == "ready"
                    and existing["source_hash"] == item["source_hash"]
                ):
                    skipped += 1
                    continue
            to_embed.append(item)

        refreshed = 0
        batch_result: dict[str, Any] | None = None
        if to_embed:
            cost_block = _automatic_cost_gate(
                action, params, index, space, project_id, units=len(to_embed)
            )
            if cost_block is not None:
                return cost_block
            # A remote-provider batch operates under
            # effect-checkpoint authority.  Local providers spend nothing and
            # produce zero durable checkpoint writes.
            effect_checkpointed = str(space["provider_kind"]) in REMOTE_PROVIDER_KINDS
            stored_payload: dict[str, Any] | None = None
            if effect_checkpointed:
                checkpoint_store = EffectCheckpointStore(project.db)
                effect_unit = _embedding_refresh_effect_unit(space, modality, to_embed)
                prior = checkpoint_store.find_unit(
                    family=_EMBEDDING_REFRESH_EFFECT_FAMILY,
                    group_key=str(params.index_id),
                    unit_key=effect_unit["unit_key"],
                )
                if prior is not None:
                    # The recovery decision tree: returned replays without
                    # another egress; reserved is ambiguous and refuses;
                    # identity drift refuses by name.
                    try:
                        returned = checkpoint_store.returned_for_replay(
                            family=_EMBEDDING_REFRESH_EFFECT_FAMILY,
                            group_key=str(params.index_id),
                            unit_key=effect_unit["unit_key"],
                            action_kind=_EMBEDDING_REFRESH_EFFECT_KIND,
                            identity=effect_unit["identity"],
                        )
                    except EffectCheckpointRefused as exc:
                        return _error(
                            action,
                            project_id,
                            "external_effect_reconciliation_required",
                            str(exc),
                            field="params.index_id",
                            details={"checkpoint_code": exc.code},
                        )
                    stored_payload = returned["payload"]
                    stored_error = (
                        stored_payload.get("error")
                        if isinstance(stored_payload, dict)
                        else None
                    )
                    if isinstance(stored_error, dict):
                        # A durable post-egress provider failure: replay the
                        # same named error at zero provider cost.  The
                        # checkpoint stays returned — a changed source set is
                        # a different unit and buys fresh; this exact request
                        # never silently re-buys.
                        return _error(
                            action,
                            project_id,
                            str(stored_error.get("code") or "embedding_provider_error"),
                            str(
                                stored_error.get("message")
                                or "the embedding provider failed for this batch"
                            ),
                            field="params.index_id",
                            details={"replayed_effect_error": True},
                        )
                    if (
                        not isinstance(stored_payload, dict)
                        or not isinstance(stored_payload.get("result"), dict)
                        or not isinstance(stored_payload.get("provider_fact"), dict)
                    ):
                        return _error(
                            action,
                            project_id,
                            "external_effect_reconciliation_required",
                            (
                                "a returned embedding-refresh effect checkpoint "
                                "is incomplete or corrupt; refusing another "
                                "provider call until it is reconciled"
                            ),
                            field="params.index_id",
                        )
            if stored_payload is not None:
                # Materialize the durable paid response without re-calling.
                # The crashed invocation's accounting envelope (op/run, with
                # the fact and spend already booked) is reused when it still
                # exists, so the receipt and the fact share one run.
                replay_resume_admission = None
                stored_effect = stored_payload.get("effect") or {}
                prior_run_id = stored_effect.get("run_id")
                prior_op_id = stored_effect.get("op_id")
                if (
                    isinstance(prior_run_id, int)
                    and isinstance(prior_op_id, int)
                    and run_store.get_run(prior_run_id) is not None
                ):
                    op_id = int(prior_op_id)
                    run_id = int(prior_run_id)
                    # An in-process failure closes the original accounting
                    # run as failed. Replaying its durable returned checkpoint
                    # is a genuine resume: reopen it explicitly before minting
                    # the replacement attempt so terminalization can retain
                    # its running-only monotonic guard.
                    replay_resume_admission = run_store.begin_run_resume(run_id)
                    if not replay_resume_admission.admitted:
                        return _error(
                            action,
                            project_id,
                            "project_write_failed",
                            "embedding refresh accounting run could not be resumed",
                            field="params.index_id",
                            details={"retryable": True},
                        )
                else:
                    op_id = project.append_op(
                        "embedding.index_refresh",
                        {"action_id": action.kind, "params": dict(action.params)},
                        label=f"Refresh embedding index {params.index_id}",
                    )
                    run_id = run_store.start_run(
                        op_id,
                        int(index["sheet_id"]),
                        "embedding.index_refresh",
                        model=str(space["actual_model_id"]),
                        params=action.params,
                        total_rows=len(to_embed),
                        row_ids=[int(item["row_id"]) for item in to_embed],
                    )
                try:
                    commitment = mint_direct_effect_attempt(
                        project,
                        action_kind=_EMBEDDING_REFRESH_EFFECT_KIND,
                        run_id=run_id,
                        scope=tuple(int(item["row_id"]) for item in to_embed),
                    )
                except AttemptClaimRefused as exc:
                    if replay_resume_admission is not None:
                        run_store.revert_run_resume(run_id, replay_resume_admission)
                    return _error(
                        action,
                        project_id,
                        "project_write_failed",
                        str(exc),
                        field="params.index_id",
                        details={"retryable": True},
                    )
                attempt_id = commitment.attempt_id
                provider_fact = dict(stored_payload["provider_fact"])
                result = stored_payload["result"]
                batch_result = result
            else:
                # Establish the durable accounting envelope BEFORE remote
                # dispatch.  A process crash during/after the call therefore
                # leaves a visible running run for recovery instead of an
                # untracked billable call.
                op_id = project.append_op(
                    "embedding.index_refresh",
                    {"action_id": action.kind, "params": dict(action.params)},
                    label=f"Refresh embedding index {params.index_id}",
                )
                run_id = run_store.start_run(
                    op_id,
                    int(index["sheet_id"]),
                    "embedding.index_refresh",
                    model=str(space["actual_model_id"]),
                    params=action.params,
                    total_rows=len(to_embed),
                    row_ids=[int(item["row_id"]) for item in to_embed],
                )
                try:
                    commitment = mint_direct_effect_attempt(
                        project,
                        action_kind=_EMBEDDING_REFRESH_EFFECT_KIND,
                        run_id=run_id,
                        scope=tuple(int(item["row_id"]) for item in to_embed),
                    )
                except AttemptClaimRefused as exc:
                    return _error(
                        action,
                        project_id,
                        "project_write_failed",
                        str(exc),
                        field="params.index_id",
                        details={"retryable": True},
                    )
                attempt_id = commitment.attempt_id
                try:
                    with ExitStack() as leases:
                        gateway_inputs = _gateway_inputs(
                            modality,
                            to_embed,
                            project=project,
                            leases=leases,
                        )
                        if effect_checkpointed:
                            assert checkpoint_store is not None
                            assert effect_unit is not None
                            reserved = checkpoint_store.reserve(
                                effect_unit["checkpoint_id"],
                                family=_EMBEDDING_REFRESH_EFFECT_FAMILY,
                                group_key=str(params.index_id),
                                unit_key=effect_unit["unit_key"],
                                action_kind=_EMBEDDING_REFRESH_EFFECT_KIND,
                                identity=effect_unit["identity"],
                                authorized_attempt_id=attempt_id,
                                run_id=run_id,
                                writer_attempt_id=attempt_id,
                                claimless_direct_effect=True,
                                payload={
                                    "effect": {
                                        "op_id": op_id,
                                        "run_id": run_id,
                                        "attempt_id": attempt_id,
                                    }
                                },
                            )
                            if not reserved:
                                return _error(
                                    action,
                                    project_id,
                                    "external_effect_reconciliation_required",
                                    (
                                        "a concurrent invocation reserved this "
                                        "refresh batch first; refusing to race "
                                        "it with another provider call"
                                    ),
                                    field="params.index_id",
                                )
                        try:
                            result = gateway.embed(
                                gateway_inputs,
                                provider=space["provider_id"],
                                model=space["actual_model_id"],
                                modality=modality,
                            )
                        except EmbeddingBackendUnavailable:
                            # Genuinely pre-egress: the gateway raises this
                            # before any wire I/O (missing adapter/model/key),
                            # so the discard contract's "egress did not start"
                            # is provable and the refresh stays retryable.
                            if effect_checkpointed:
                                assert checkpoint_store is not None
                                assert effect_unit is not None
                                checkpoint_store.discard_reserved(
                                    effect_unit["checkpoint_id"],
                                    family=_EMBEDDING_REFRESH_EFFECT_FAMILY,
                                    group_key=str(params.index_id),
                                    unit_key=effect_unit["unit_key"],
                                    action_kind=_EMBEDDING_REFRESH_EFFECT_KIND,
                                    identity=effect_unit["identity"],
                                    run_id=run_id,
                                    writer_attempt_id=attempt_id,
                                    claimless_direct_effect=True,
                                )
                            raise
                        except EmbeddingProviderError as exc:
                            # Post-egress on every reachable remote path (the
                            # gateway wraps HTTP error responses, post-send
                            # timeouts, and post-acceptance parse failures in
                            # this type): the provider may have billed the
                            # request, so the failure is durable truth.
                            # Complete as returned-with-error — no vectors,
                            # and no fact, since the error carries nothing a
                            # fact could honestly be derived from — so resume
                            # replays the same named error at zero provider
                            # cost instead of re-buying.  Anything
                            # unclassified propagates and leaves the
                            # reservation standing — fail closed on ambiguity.
                            if effect_checkpointed:
                                assert checkpoint_store is not None
                                assert effect_unit is not None
                                checkpoint_store.complete(
                                    effect_unit["checkpoint_id"],
                                    family=_EMBEDDING_REFRESH_EFFECT_FAMILY,
                                    group_key=str(params.index_id),
                                    unit_key=effect_unit["unit_key"],
                                    action_kind=_EMBEDDING_REFRESH_EFFECT_KIND,
                                    identity=effect_unit["identity"],
                                    payload={
                                        "effect": {
                                            "op_id": op_id,
                                            "run_id": run_id,
                                            "attempt_id": attempt_id,
                                        },
                                        "error": {
                                            "code": "embedding_provider_error",
                                            "message": str(exc)[:500],
                                        },
                                    },
                                    run_id=run_id,
                                    writer_attempt_id=attempt_id,
                                    claimless_direct_effect=True,
                                )
                            raise
                        batch_result = result
                        provider_fact = _embedding_provider_fact(batch_result)
                        if effect_checkpointed:
                            assert checkpoint_store is not None
                            assert effect_unit is not None
                            assert provider_fact is not None
                            # The durable call id, minted at call time: replay
                            # dedup requires it, and the run id keeps it
                            # unique per purchase.
                            provider_fact = dict(provider_fact)
                            provider_fact["id"] = (
                                f"embedding_refresh_{run_id}_{effect_unit['identity']}"
                            )
                            completion_payload = {
                                "effect": {
                                    "op_id": op_id,
                                    "run_id": run_id,
                                    "attempt_id": attempt_id,
                                },
                                "result": json.loads(
                                    json.dumps(result, allow_nan=False)
                                ),
                                "provider_fact": provider_fact,
                            }

                            def accrue(
                                checkpoint: dict[str, Any],
                                *,
                                _run_id: int = run_id,
                                _fact: dict[str, Any] = provider_fact,
                            ) -> float:
                                _persist_embedding_provider_fact(
                                    project,
                                    run_store=run_store,
                                    run_id=_run_id,
                                    fact=_fact,
                                    index=index,
                                    row_ids=row_ids,
                                    commit=False,
                                    writer_attempt_id=attempt_id,
                                    claimless_direct_effect=True,
                                    authorized_attempt_id=checkpoint[
                                        "authorized_attempt_id"
                                    ],
                                )
                                return float(_fact.get("provider_cost_usd") or 0.0)

                            # Facts + cap accrual + replay payload move to
                            # durable truth in ONE transaction, immediately
                            # after the provider returned — the crash window
                            # between "provider charged" and "anything
                            # recorded" is the reservation alone.
                            checkpoint_store.complete(
                                effect_unit["checkpoint_id"],
                                family=_EMBEDDING_REFRESH_EFFECT_FAMILY,
                                group_key=str(params.index_id),
                                unit_key=effect_unit["unit_key"],
                                action_kind=_EMBEDDING_REFRESH_EFFECT_KIND,
                                identity=effect_unit["identity"],
                                payload=completion_payload,
                                accrue=accrue,
                                run_id=run_id,
                                writer_attempt_id=attempt_id,
                                claimless_direct_effect=True,
                            )
                        else:
                            _persist_embedding_provider_fact(
                                project,
                                run_store=run_store,
                                run_id=run_id,
                                fact=provider_fact,
                                index=index,
                                row_ids=row_ids,
                                writer_attempt_id=attempt_id,
                                claimless_direct_effect=True,
                            )
                except _ImageSourceError as exc:
                    return _error(
                        action,
                        project_id,
                        "embedding_source_unsupported",
                        str(exc),
                        field="params.index_id",
                        details={"source_key": exc.source_key},
                    )
                except EmbeddingBackendUnavailable as exc:
                    return _error(
                        action,
                        project_id,
                        "embedding_backend_unavailable",
                        str(exc),
                        field="params.index_id",
                    )
                except EmbeddingProviderError as exc:
                    return _error(
                        action,
                        project_id,
                        "embedding_provider_error",
                        str(exc),
                        field="params.index_id",
                        details={"retryable": True},
                    )
            vectors = result["vectors"]
            # Validate the whole batch BEFORE writing any sidecar row: a short
            # batch is a provider error; a wrong-width vector is a space mismatch.
            if len(vectors) != len(to_embed):
                return _error(
                    action,
                    project_id,
                    "embedding_provider_error",
                    (
                        f"provider returned {len(vectors)} vectors for "
                        f"{len(to_embed)} inputs"
                    ),
                    field="params.index_id",
                    details={"expected": len(to_embed), "actual": len(vectors)},
                )
            space_dim = int(space["dimension"])
            bad_width = next((len(v) for v in vectors if len(v) != space_dim), None)
            if result["dimension"] != space_dim or bad_width is not None:
                return _error(
                    action,
                    project_id,
                    "embedding_space_mismatch",
                    (
                        "provider returned a vector of dimension "
                        f"{bad_width if bad_width is not None else result['dimension']} "
                        f"but space {space['id']} is {space_dim}"
                    ),
                    field="params.index_id",
                    details={
                        "expected": space_dim,
                        "actual": bad_width
                        if bad_width is not None
                        else int(result["dimension"]),
                    },
                )
            # The vector sidecar is a second SQLite file, but replacement
            # authority lives in the project DB.  Hold the project write lock
            # from the current-writer check through every sidecar commit so a
            # stale refresh cannot write vectors after B takes over.
            project.db.execute("BEGIN IMMEDIATE")
            try:
                assert run_id is not None
                assert attempt_id is not None
                OutputColumnClaimStore.require_current_writer(
                    project.db,
                    run_id=run_id,
                    writer_attempt_id=attempt_id,
                    claim_token=None,
                    claimless_direct_effect=True,
                )
                for item, vector in zip(to_embed, vectors):
                    backend.upsert_item(
                        index_id=params.index_id,
                        space_id=space["id"],
                        source_key=item["source_key"],
                        source_ref=item["source_ref"],
                        source_hash=item["source_hash"],
                        status="ready",
                        vector=list(vector),
                    )
                    refreshed += 1
                project.db.commit()
            except BaseException:
                project.db.rollback()
                raise

        counts = backend.item_counts(params.index_id)
        ready_items = counts.get("ready", 0)
        total_items = len(payloads)

        # Token-limit guard: providers/fastembed SILENTLY TRUNCATE inputs beyond a
        # model's max_input_tokens, yielding a misleading vector. We don't reject
        # (truncation is the provider default) — we SURFACE the count of likely-
        # truncated items (truncated_items) so the user can see it. Estimate is a
        # conservative chars/token heuristic; None limit -> skipped.
        cap = resolve_embedding_capability(
            modality=modality,
            provider=space["provider_id"],
            model=space["actual_model_id"],
        )
        max_input_tokens = cap.get("max_input_tokens") if cap else None
        truncated_items = _count_likely_truncated(to_embed, max_input_tokens)

        action_id = reserved_action_id or _new_id("act")
        receipt_id = reserved_receipt_id or _new_id("receipt")
        output_ref = {
            "kind": "embedding_index_refresh",
            "operation_args_sha256": _source_policy_hash(dict(action.params)),
            "index_id": params.index_id,
            "space_id": space["id"],
            "mode": params.mode,
            "trigger_ref": params.trigger_ref,
            "total_items": total_items,
            "refreshed": refreshed,
            "skipped_current": skipped,
            "ready_items": ready_items,
            "truncated_items": truncated_items,
            "max_input_tokens": max_input_tokens,
            "backend_id": VectorBackend.BACKEND_ID,
            "backend_version": VectorBackend.BACKEND_VERSION,
        }
        item_evidence = _refresh_items_evidence(
            backend=backend,
            index=index,
            space=space,
            params=params,
            payloads=payloads,
            output_ref=output_ref,
        )
        receipt = _refresh_receipt(
            action=action,
            project_id=project_id,
            action_id=action_id,
            receipt_id=receipt_id,
            params_hash=params_hash,
            index=index,
            space=space,
            output_ref=output_ref,
            item_evidence=item_evidence,
            trigger_ref=params.trigger_ref,
            run_id=run_id,
            provider_fact=provider_fact,
        )
        # Counts + last_refresh_receipt_id commit in the SAME transaction as the
        # receipt insert, so last_refresh_receipt_id never points at a receipt
        # that failed to land. Status is left to the claim release (still held).
        try:
            project.db.execute("BEGIN IMMEDIATE")
            if run_id is not None:
                if attempt_id is None:
                    raise StaleAttemptWriter(
                        "embedding refresh terminal write supplied no writer attempt"
                    )
                OutputColumnClaimStore.require_current_writer(
                    project.db,
                    run_id=run_id,
                    writer_attempt_id=attempt_id,
                    claim_token=None,
                    output_column_ids=frozenset(),
                    claimless_direct_effect=True,
                )
            else:
                # A no-op refresh has no accounting run or attempt. Preserve
                # its receipt-only transaction: there is no project-run tuple
                # for the shared terminal kernel to fence or close.
                if reserved_receipt_id is not None:
                    landed = ReceiptStore(project).update_if_status_in(
                        receipt,
                        {"queued", "running"},
                        commit=False,
                    )
                    if not landed:
                        raise RuntimeError(
                            f"reserved refresh receipt {reserved_receipt_id!r} "
                            "not writable"
                        )
                else:
                    _insert_receipt(project, receipt)
            store.update_index_counts(
                params.index_id,
                total=total_items,
                ready=ready_items,
                stale=0,
                error=0,
                last_refresh_receipt_id=receipt_id,
                commit=False,
            )
            if checkpoint_store is not None and effect_unit is not None:
                # Retire the batch checkpoint in the SAME transaction as the
                # terminal receipt: the refresh can never finalize while
                # replay authority survives, and a rollback keeps both.
                checkpoint_store.consume_and_retire(
                    effect_unit["checkpoint_id"],
                    family=_EMBEDDING_REFRESH_EFFECT_FAMILY,
                    group_key=str(params.index_id),
                    unit_key=effect_unit["unit_key"],
                    action_kind=_EMBEDDING_REFRESH_EFFECT_KIND,
                    identity=effect_unit["identity"],
                    run_id=run_id,
                    writer_attempt_id=attempt_id,
                    claimless_direct_effect=True,
                    commit=False,
                )
            if run_id is not None:
                assert attempt_id is not None
                terminalization = terminalize_project_run(
                    project,
                    run_id=run_id,
                    receipt_id=receipt_id,
                    status="completed",
                    authority=CurrentWriterTerminalAuthority(
                        writer_attempt_id=attempt_id,
                        claim_token=None,
                        claimless_direct_effect=True,
                    ),
                    terminal_receipt=receipt,
                    receipt_source_statuses=(
                        {"queued", "running"}
                        if reserved_receipt_id is not None
                        else None
                    ),
                    commit=False,
                )
                expected_receipt_disposition = (
                    "updated" if reserved_receipt_id is not None else "inserted"
                )
                if (
                    terminalization.disposition != "terminalized"
                    or terminalization.receipt_disposition
                    != expected_receipt_disposition
                    or terminalization.run_status != "completed"
                    or terminalization.receipt_status != "completed"
                ):
                    raise StaleAttemptWriter(
                        terminalization.reason
                        or (
                            "embedding refresh terminal tuple refused: "
                            f"{terminalization.disposition}/"
                            f"{terminalization.receipt_disposition}"
                        )
                    )
            project.db.commit()
            run_finalized = True
            if attempt_id is not None:
                attempt_closed = True
        except StaleAttemptWriter:
            project.db.rollback()
            raise
        except BaseException as exc:
            project.db.rollback()
            if not isinstance(exc, Exception):
                raise
            return _error(
                action,
                project_id,
                "project_write_failed",
                "embedding refresh receipt could not be written",
                details={"retryable": True},
            )
        return ActionResult(
            action=ActionIdentity(kind=action.kind, action_id=action_id),
            status="completed",
            project_id=project_id,
            run_id=run_id,
            outputs=[
                ActionOutput(
                    kind="embedding_index_refresh",
                    name=params.index_id,
                    sheet_id=index["sheet_id"],
                    ref=output_ref,
                )
            ],
            receipt_id=receipt_id,
        )
    except StaleAttemptWriter:
        stale_writer = True
        raise
    finally:
        if not stale_writer and run_id is not None and not run_finalized:
            run = run_store.get_run(run_id)
            if run is not None and run["status"] == "running":
                # Normal Python failures quarantine the accounting envelope.
                # A hard process crash intentionally leaves it running so stale-
                # run recovery can surface/reconcile it rather than call it free.
                run_store.finish_run(run_id, "failed")
        if not stale_writer and attempt_id is not None and not attempt_closed:
            # In-process failure: the dispatch is over.  A hard process crash
            # skips this by construction, leaving 'dispatching' for the
            # stale-attempt sweep — the ambiguous case stays visible.
            set_attempt_state(project, attempt_id, "halted")
        backend.close()


def repair_unfinalized_refresh(project: Any, index_id: str) -> dict[str, int] | None:
    """Reconcile an index whose sidecar vectors were written but whose terminal
    project receipt/count update did not land — a crash AFTER the per-item sidecar
    commits but BEFORE the finalize transaction (receipt + counts).

    Recomputes the index counts from the durable sidecar so the index never
    advertises a phantom-ready state, WITHOUT setting ``last_refresh_receipt_id``
    (there is no terminal receipt). Idempotent: safe to call before each refresh /
    after reclaiming a stale refresh lease. Returns the reconciled counts, or None
    if the index is gone.
    """

    from frisket.ai.embeddings.store import EmbeddingStore
    from frisket.ai.embeddings.vector_backend import VectorBackend

    store = EmbeddingStore(project)
    if store.get_index(index_id) is None:
        return None
    backend = VectorBackend(project)
    try:
        backend.ensure_schema()
        by_status = backend.item_counts(index_id)
    finally:
        backend.close()
    total = sum(by_status.values())
    ready = int(by_status.get("ready", 0))
    stale = int(by_status.get("stale", 0))
    error = int(by_status.get("error", 0))
    # Reconcile counts to the sidecar reality; do NOT touch last_refresh_receipt_id
    # so a crashed refresh is never recorded as a completed one.
    store.update_index_counts(
        index_id,
        total=total,
        ready=ready,
        stale=stale,
        error=error,
        touch_refreshed_at=False,
        commit=True,
    )
    return {"total": total, "ready": ready, "stale": stale, "error": error}


def run_index_refresh_action_job(
    project: Project,
    envelope: Any,
    *,
    router: Any | None = None,
    embedding_gateway: Any | None = None,
) -> ActionResult:
    """Run embedding.index_refresh inside the generic action.run worker."""

    from frisket.engine.executor.action_jobs import bind_typed_action_job

    bound = bind_typed_action_job(envelope)
    if isinstance(bound, ActionResult):
        return bound
    if bound.action.definition.run.capabilities != (IndexRefresher,):
        raise TypeError("embedding refresh worker requires IndexRefresher")
    return run_typed_embedding_action(
        project,
        envelope.project_id,
        bound,
        router=router,
        embedding_gateway=embedding_gateway,
        reserved_action_id=envelope.action_id,
        reserved_receipt_id=envelope.receipt_id,
        skip_replay=True,
    )


def _refresh_receipt(
    *,
    action,
    project_id,
    action_id,
    receipt_id,
    params_hash,
    index,
    space,
    output_ref,
    item_evidence,
    trigger_ref,
    run_id,
    provider_fact,
) -> Receipt:
    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="completed",
        run_id=run_id,
        inputs=[
            ReceiptIO(
                name="embedding_index",
                ref={
                    "kind": "embedding_index_ref",
                    "index_id": index["id"],
                    "space_id": space["id"],
                    "provider_id": space["provider_id"],
                    "provider_kind": space["provider_kind"],
                    "actual_model_id": space["actual_model_id"],
                    "dimension": int(space["dimension"]),
                    "distance_metric": space["distance_metric"],
                    "normalization": space["normalization"],
                    "modality": space["modality"],
                    "source_policy_hash": index["source_policy_hash"],
                    "trigger_ref": trigger_ref,
                },
            )
        ],
        outputs=[ReceiptIO(name="embedding_index_refresh", ref=output_ref)],
        provider_use=[
            {
                "provider": space["provider_id"],
                "service": "embedding.index_refresh",
                "external_api": space["provider_kind"] in REMOTE_PROVIDER_KINDS,
                "cost_actual": _provider_fact_cost_actual(provider_fact),
            }
        ],
        evidence=[
            ReceiptEvidence(ref=output_ref, retention="pinned"),
            ReceiptEvidence(ref=item_evidence, retention="pinned"),
            *(
                [ReceiptEvidence(ref=provider_fact, retention="pinned")]
                if provider_fact is not None
                else []
            ),
        ],
    )


#: Effect-checkpoint family coordinates. A refresh
#: makes ONE batched provider call, so its natural checkpoint unit is that
#: batch: the unit key/identity digest binds the exact provider inputs
#: (provider, model, modality, and every item's source key + content hash),
#: and the group key is the index id — the recovery scope a retry re-derives.
_EMBEDDING_REFRESH_EFFECT_FAMILY = "embedding_index_refresh"
_EMBEDDING_REFRESH_EFFECT_KIND = "embedding.index_refresh"


def _embedding_refresh_effect_unit(
    space: Any,
    modality: str,
    to_embed: list[dict[str, Any]],
) -> dict[str, str]:
    items = sorted(
        (str(item["source_key"]), str(item["source_hash"])) for item in to_embed
    )
    payload = {
        "schema_version": "frisket.embedding_index_refresh_effect_identity.v1",
        "provider_id": str(space["provider_id"]),
        "model_id": str(space["actual_model_id"]),
        "modality": str(modality),
        "items": items,
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "unit_key": f"batch:{digest}",
        "identity": digest,
        "checkpoint_id": f"embedding_refresh_{digest}",
    }


def _embedding_provider_fact(
    result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if result is None:
        return None
    units = dict(result.get("usage") or {})
    units.setdefault("input_count", 0)
    return ModelCallMeta.provider_call(
        capability="llm.embed",
        engine=f"{result['provider_id']}/{result['actual_model_id']}",
        provider=result["provider_id"],
        provider_kind=result["provider_kind"],
        model_ids=[result["actual_model_id"]],
        credential_source=result.get("credential_source", "local"),
        provider_reported_cost_usd=result.get("provider_reported_cost_usd"),
        provider_cost_usd=result.get("provider_cost_usd"),
        units=units,
        cost_source=result.get("cost_source", "unknown"),
        request_id=result.get("provider_request_id"),
        warnings=[],
        duration_ms=None,
    ).as_dict()


def _provider_fact_cost_actual(fact: dict[str, Any] | None) -> float | None:
    """Receipt-facing cost for one provider fact without turning unknown into $0."""
    if fact is None:
        return 0.0
    cost = fact.get("provider_cost_usd")
    return None if cost is None else float(cost)


def _persist_embedding_provider_fact(
    project: Project,
    *,
    run_store: RunResultStore,
    run_id: int,
    fact: dict[str, Any],
    row_ids: list[int],
    index: Any | None = None,
    sheet_id: int | None = None,
    commit: bool = True,
    writer_attempt_id: str | None = None,
    claimless_direct_effect: bool = False,
    authorized_attempt_id: str | None = None,
) -> None:
    """Persist the returned provider fact before vector/receipt finalization.

    ``authorized_attempt_id`` is the immutable owner of the fact (the attempt
    that authorized egress, pinned at execution start); ``None``
    keeps the legacy single-attempt fallback for the probe paths."""
    if sheet_id is None:
        if index is None:
            raise TypeError("index or sheet_id is required for embedding accounting")
        sheet_id = int(index["sheet_id"])
    first_row_id = int(row_ids[0]) if row_ids else None
    first_column = next(
        iter(project.columns(sheet_id, include_hidden=True)),
        None,
    )
    if first_column is None:
        raise RuntimeError("embedding refresh source sheet has no durable column")
    stored = dict(fact)
    stored.update(
        {
            "run_id": run_id,
            "row_id": first_row_id,
            "column_id": int(first_column["id"]),
        }
    )
    # write_model_calls reprojects runs.cost_actual from the run's durable
    # facts inside this same transaction (the one mint) — a multi-batch
    # refresh accumulates naturally, and an unknown provider cost stays an
    # honest NULL instead of overwriting the run figure with $0.
    run_store.write_model_calls(
        run_id,
        [
            {
                "row_id": first_row_id,
                "column_id": stored["column_id"],
                "model_calls": [stored],
            }
        ],
        writer_attempt_id=writer_attempt_id,
        claimless_direct_effect=claimless_direct_effect,
        authorized_attempt_id=authorized_attempt_id,
    )
    if commit:
        project.db.commit()


def _refresh_items_evidence(
    *,
    backend: VectorBackend,
    index,
    space,
    params: IndexRefreshParams,
    payloads: list[dict[str, Any]],
    output_ref: dict[str, Any],
) -> dict[str, Any]:
    source_keys = [str(item["source_key"]) for item in payloads]
    evidence = {
        "kind": "embedding_index_refresh_items",
        "index_id": index["id"],
        "space_id": space["id"],
        "mode": params.mode,
        "row_scope": params.row_scope,
        "backend_id": VectorBackend.BACKEND_ID,
        "backend_version": VectorBackend.BACKEND_VERSION,
        "source_query": _json_obj(index["source_query_json"]),
        "source_columns": json.loads(index["source_columns_json"]),
        "source_policy_hash": index["source_policy_hash"],
        "counts": {
            "refreshed": output_ref["refreshed"],
            "skipped_current": output_ref["skipped_current"],
            "ready_items": output_ref["ready_items"],
            "total_items": output_ref["total_items"],
        },
    }
    evidence.update(
        _sidecar_item_evidence(
            backend,
            index_id=index["id"],
            space_id=space["id"],
            source_keys=source_keys,
        )
    )
    return evidence


# --------------------------------------------------------------------------
# embedding.index_update_policy
# --------------------------------------------------------------------------

_MAINTENANCE_MODES = frozenset({"manual", "on_source_append", "scheduled"})


def _validate_policies(
    action, project_id, maintenance, provider
) -> ActionResult | None:
    """Typed validation of the replacement policies (mode/schedule + provider flag
    types). The automatic-remote + cost gates are NOT applied here — they fire at
    refresh time, so a policy update only stores fields."""
    if maintenance is not None:
        mode = maintenance.get("mode", "manual")
        if not isinstance(mode, str) or mode not in _MAINTENANCE_MODES:
            return _error(
                action,
                project_id,
                "invalid_params",
                f"maintenance mode must be one of {sorted(_MAINTENANCE_MODES)}",
                field="params.maintenance_policy.mode",
            )
        if mode == "scheduled":
            schedule = maintenance.get("schedule")
            if not isinstance(schedule, str) or not schedule.strip():
                return _error(
                    action,
                    project_id,
                    "invalid_params",
                    "scheduled maintenance requires a non-empty schedule",
                    field="params.maintenance_policy.schedule",
                )
    if provider is not None:
        for flag in ("allow_remote", "allow_remote_automatic_refresh"):
            if flag in provider and not isinstance(provider[flag], bool):
                return _error(
                    action,
                    project_id,
                    "invalid_params",
                    f"provider_policy.{flag} must be a boolean",
                    field=f"params.provider_policy.{flag}",
                )
        cap = provider.get("max_cost_usd_per_refresh")
        if cap is not None and not _is_positive_finite_number(cap):
            return _error(
                action,
                project_id,
                "invalid_params",
                "provider_policy.max_cost_usd_per_refresh must be a positive finite number or null",
                field="params.provider_policy.max_cost_usd_per_refresh",
            )
    return None


class _PolicyUpdateRefused(Exception):
    def __init__(self, result: ActionResult):
        self.result = result


class _IndexPolicyUpdater:
    def __init__(
        self, project: Project, action: _TypedProjectEnvelope, project_id: str
    ):
        self.project = project
        self.action = action
        self.project_id = project_id
        self.result: EmbeddingIndexPolicy | None = None

    def update_policy(
        self,
        index_id: str,
        *,
        maintenance_policy: dict[str, Any] | None = None,
        provider_policy: dict[str, Any] | None = None,
    ) -> EmbeddingIndexPolicy:
        if self.result is not None:
            raise RuntimeError("index policy capability may be called only once")
        store = EmbeddingStore(self.project)
        index = store.get_index(index_id)
        if index is None:
            raise _PolicyUpdateRefused(
                _error(
                    self.action,
                    self.project_id,
                    "embedding_index_not_found",
                    f"no embedding index {index_id!r}",
                    field="params.index_id",
                )
            )
        invalid = _validate_policies(
            self.action, self.project_id, maintenance_policy, provider_policy
        )
        if invalid is not None:
            raise _PolicyUpdateRefused(invalid)
        store.update_index_policy(
            index_id,
            maintenance_policy=maintenance_policy,
            provider_policy=provider_policy,
            commit=False,
        )
        updated = store.get_index(index_id)
        self.result = EmbeddingIndexPolicy(
            index_id=updated["id"],
            space_id=updated["space_id"],
            maintenance_policy=_json_obj(updated["maintenance_policy_json"]),
            provider_policy=_json_obj(updated["provider_policy_json"]),
        )
        # Keep the host's recorded facts independent of the handler's return value.
        return self.result.model_copy(deep=True)


def _perform_index_update_policy_in_txn(
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
    capability = _IndexPolicyUpdater(project, action, project_id)
    try:
        returned = terminal.handler(params, capability)
    except _PolicyUpdateRefused as error:
        # Capability arguments may be derived from any author-owned Params fields.
        return error.result.model_copy(
            update={
                "errors": [
                    item.model_copy(update={"field": "params"})
                    for item in error.result.errors
                ]
            }
        )
    result = capability.result
    if result is None or not isinstance(returned, EmbeddingIndexPolicy):
        raise TypeError(
            "index policy handler must call its capability and return a policy"
        )
    output_ref = {
        "kind": "embedding_index_update_policy",
        **result.model_dump(mode="json"),
    }
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
        evidence=[
            ReceiptEvidence(
                ref={
                    "kind": "embedding_index_policy",
                    "maintenance_policy": result.maintenance_policy,
                    "provider_policy": result.provider_policy,
                },
                retention="pinned",
            )
        ],
    )
    _insert_receipt(project, receipt)
    return _result_from_receipt(receipt)


class _EmbeddingOperationRefused(Exception):
    def __init__(self, result: ActionResult):
        self.result = result


class _EmbeddingLifecycleCapability:
    def __init__(
        self,
        project,
        bound,
        project_id,
        *,
        router,
        embedding_gateway,
        reserved_action_id,
        reserved_receipt_id,
        skip_replay,
    ):
        from frisket.engine.executor.map_rows_action import typed_request_hash

        self.project = project
        self.bound = bound
        self.project_id = project_id
        self.params_hash = typed_request_hash(bound)
        self.router = router
        self.embedding_gateway = embedding_gateway
        self.reserved_action_id = reserved_action_id
        self.reserved_receipt_id = reserved_receipt_id
        self.skip_replay = skip_replay
        self.result: ActionResult | None = None
        self.called = False

    def _run(self, args, *, create):
        if self.called:
            raise ValueError(
                "one embedding lifecycle operation is allowed per invocation"
            )
        self.called = True
        # This is operation-argument validation, never validation of author Params.
        model = IndexCreateParams if create else IndexRefreshParams
        try:
            params = model.model_validate(args)
            admitted = params.model_dump(mode="json")
            params = model.model_validate(admitted)
        except (TypeError, ValueError) as exc:
            raise _EmbeddingOperationRefused(
                _failed_result(
                    project_id=self.project_id,
                    action_kind=self.bound.action.action_id,
                    error=ActionError(
                        code="invalid_params",
                        message="Invalid embedding operation arguments.",
                        action_kind=self.bound.action.action_id,
                        field="params",
                    ),
                )
            ) from exc
        action = _TypedProjectEnvelope(
            kind=self.bound.action.action_id,
            idempotency_key=self.bound.request.idempotency_key,
            params=admitted,
        )
        kwargs = dict(
            project_id=self.project_id,
            params_hash=self.params_hash,
            router=self.router,
            embedding_gateway=self.embedding_gateway,
        )
        if not create:
            kwargs.update(
                reserved_action_id=self.reserved_action_id,
                reserved_receipt_id=self.reserved_receipt_id,
                skip_replay=self.skip_replay,
            )
        result = (_create_index if create else _refresh_index)(
            self.project,
            action,
            params,
            **kwargs,
        )
        self.result = result.model_copy(deep=True)
        if result.status != "completed":
            raise _EmbeddingOperationRefused(result)
        output = CreatedEmbeddingIndex if create else RefreshedEmbeddingIndex
        ref = result.outputs[0].ref
        return output.model_validate({key: ref[key] for key in output.model_fields})


class _IndexCreator(_EmbeddingLifecycleCapability):
    def create(
        self,
        *,
        source_columns,
        sheet_id=None,
        modality="text",
        provider=None,
        model=None,
        source_query=None,
        source_policy=None,
        maintenance_policy=None,
        provider_policy=None,
        name=None,
    ) -> CreatedEmbeddingIndex:
        return self._run(
            dict(
                source_columns=source_columns,
                sheet_id=sheet_id,
                modality=modality,
                provider=provider,
                model=model,
                source_query={} if source_query is None else source_query,
                source_policy={} if source_policy is None else source_policy,
                maintenance_policy={}
                if maintenance_policy is None
                else maintenance_policy,
                provider_policy={} if provider_policy is None else provider_policy,
                name=name,
            ),
            create=True,
        )


class _IndexRefresher(_EmbeddingLifecycleCapability):
    def refresh(
        self,
        index_id,
        *,
        mode="incremental",
        trigger_ref=None,
        row_scope=None,
    ) -> RefreshedEmbeddingIndex:
        return self._run(
            dict(
                index_id=index_id,
                mode=mode,
                trigger_ref=trigger_ref,
                row_scope=row_scope,
            ),
            create=False,
        )


def supports_typed_embedding_action(terminal: object) -> bool:
    return isinstance(terminal, _ProjectAction) and any(
        terminal.capabilities == (cap,)
        for cap in {
            EmbeddingIndexPolicyUpdater,
            IndexDeleter,
            IndexExporter,
            IndexCreator,
            IndexRefresher,
        }
    )


def run_typed_embedding_action(
    project: Project,
    project_id: str,
    bound: BoundTypedActionRequest,
    *,
    router=None,
    embedding_gateway=None,
    reserved_action_id=None,
    reserved_receipt_id=None,
    skip_replay=False,
) -> ActionResult:
    from frisket.engine.executor.map_rows_action import typed_request_hash

    terminal = bound.action.definition.run
    if not supports_typed_embedding_action(terminal):
        raise TypeError("typed embedding executor requires an embedding capability")
    envelope = _TypedProjectEnvelope(
        kind=bound.action.action_id,
        idempotency_key=bound.request.idempotency_key,
        params=bound.params.model_dump(mode="json"),
    )
    params_hash = typed_request_hash(bound)
    if any(terminal.capabilities == (cap,) for cap in {IndexCreator, IndexRefresher}):
        # Terminal replay precedes the authored handler and any fresh provider work.
        if not skip_replay:
            existing = _receipt_for_idempotency(project, envelope.idempotency_key)
            if existing is not None and existing["status"] != "running":
                return _replay(
                    project,
                    envelope,
                    params_hash=params_hash,
                    project_id=project_id,
                )
        capability_type = (
            _IndexCreator
            if terminal.capabilities == (IndexCreator,)
            else _IndexRefresher
        )
        capability = capability_type(
            project,
            bound,
            project_id,
            router=router,
            embedding_gateway=embedding_gateway,
            reserved_action_id=reserved_action_id,
            reserved_receipt_id=reserved_receipt_id,
            skip_replay=skip_replay,
        )
        try:
            returned = terminal.handler(bound.params, capability)
        except _EmbeddingOperationRefused as exc:
            return exc.result
        # Domain and control-flow failures from the operation propagate to the
        # worker unchanged. Only validation of the author's returned value is
        # translated here, after the host operation has settled.
        try:
            terminal.output_model.model_validate(returned)
        except (TypeError, ValueError):
            # A committed capability operation remains durable even if authored
            # postprocessing fails. Its host facts cannot be replaced or rolled back.
            if capability.result is not None:
                failed = _error(
                    envelope,
                    project_id,
                    "invalid_params",
                    "embedding handler failed after its recorded operation",
                    field="params",
                )
                return failed.model_copy(
                    update={
                        "receipt_id": capability.result.receipt_id,
                        "run_id": capability.result.run_id,
                        "op_ids": capability.result.op_ids,
                        "outputs": capability.result.outputs,
                    }
                )
            return _error(
                envelope,
                project_id,
                "invalid_params",
                "invalid embedding operation arguments or result",
                field="params",
            )
        if capability.result is None:
            return _error(
                envelope,
                project_id,
                "invalid_params",
                "handler did not invoke the embedding capability",
                field="params",
            )
        return capability.result
    if terminal.capabilities == (IndexExporter,):
        return run_index_export(
            project,
            envelope,
            bound.params,
            terminal=terminal,
            params_hash=params_hash,
            project_id=project_id,
        )
    perform = _perform_index_update_policy_in_txn
    replay_validate = _embedding_update_policy_replay_error
    if terminal.capabilities == (IndexDeleter,):
        from frisket.engine.executor.embedding_delete import _perform_delete_in_txn

        perform = _perform_delete_in_txn
        replay_validate = _embedding_delete_replay_error
    run = build_plain_action_run_fn(
        kind=envelope.kind,
        params_model=terminal.params_model,
        perform_in_txn_fn=lambda project_, cur, action, params, **kwargs: perform(
            project_, cur, action, params, terminal=terminal, **kwargs
        ),
        params_hash_fn=lambda _action: params_hash,
        replay_validate_fn=replay_validate,
    )
    return run(project, envelope, bound.params, project_id=project_id)
