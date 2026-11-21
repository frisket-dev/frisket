"""Durable embedding facts before rebuildable vector-cache publication."""

import asyncio
import hashlib
import json
from typing import Any

from frisket.engine.store.runs import RunResultStore
from frisket.ops.base import ActionContext


def embedding_call_id(ctx: ActionContext, model_id: str, texts: list[str]) -> str:
    encoded = json.dumps(
        {"run_id": ctx.extras.get("run_id"), "model_id": model_id, "texts": texts},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"semantic_join_embed_{hashlib.sha256(encoded).hexdigest()}"


def refuse_missing_paid_vectors(
    ctx: ActionContext, model_id: str, texts: list[str]
) -> None:
    from frisket.ai.embeddings.backends import embedder_is_remote

    cancelled = ctx.extras.get("cancelled")
    if callable(cancelled) and cancelled():
        raise asyncio.CancelledError

    if not embedder_is_remote(model_id):
        return
    run_id = ctx.extras.get("run_id")
    if type(run_id) is not int:
        raise RuntimeError("paid semantic matching requires a durable run")
    prior = ctx.project.db.execute(
        "SELECT id FROM model_calls WHERE id=? AND run_id=?",
        (embedding_call_id(ctx, model_id, texts), run_id),
    ).fetchone()
    if prior is not None:
        raise RuntimeError(
            "semantic join has a durable paid embedding fact but its cached vectors "
            "are missing; refusing to call the provider again and double-spend"
        )


def _prepare_embedding_batch_write(
    ctx: ActionContext,
    model_id: str,
    batch: Any,
    texts: list[str],
    *,
    identity_texts: list[str] | None = None,
) -> tuple[int, dict[str, Any], str | None, str | None] | None:
    from frisket.ai.embeddings.backends import embedder_is_remote
    from frisket.ai.llm.types import provider_from_model_id
    from frisket.ai.models.metadata import PROVIDER_KIND, ModelCallMeta
    from frisket.execution.attempt import attempt_in_scope

    if not embedder_is_remote(model_id):
        return None
    run_id = ctx.extras.get("run_id")
    if type(run_id) is not int:
        raise RuntimeError("paid semantic matching requires a durable run")
    router = ctx.extras.get("router")
    metadata = batch if isinstance(batch, dict) else {}
    provider = str(metadata.get("provider_id") or provider_from_model_id(model_id))
    actual_model = str(metadata.get("actual_model_id") or model_id.split("/", 1)[-1])
    engine = (
        actual_model
        if actual_model.startswith(f"{provider}/")
        else f"{provider}/{actual_model}"
    )
    credential_source = metadata.get("credential_source")
    if credential_source is None:
        credential_source = (
            str(router.credential_source_for(provider))
            if router is not None
            and callable(getattr(router, "credential_source_for", None))
            else "none"
        )
    units = dict(metadata.get("usage") or {})
    units.setdefault("input_count", len(texts))
    fact = ModelCallMeta.provider_call(
        capability="llm.embed",
        engine=engine,
        provider=provider,
        provider_kind=str(
            metadata.get("provider_kind") or PROVIDER_KIND.get(provider, "platform_api")
        ),
        model_ids=[actual_model],
        credential_source=str(credential_source),
        provider_reported_cost_usd=metadata.get("provider_reported_cost_usd"),
        provider_cost_usd=metadata.get("provider_cost_usd"),
        units=units,
        cost_source=str(metadata.get("cost_source") or "unknown"),
        request_id=(
            str(metadata["provider_request_id"])
            if metadata.get("provider_request_id") is not None
            else None
        ),
        warnings=[]
        if isinstance(batch, dict)
        else ["embedding provider returned vectors without usage metadata"],
        duration_ms=None,
    ).as_dict()
    row_id = ctx.extras.get("row_id")
    fact.update(
        id=embedding_call_id(
            ctx, model_id, identity_texts if identity_texts is not None else texts
        ),
        run_id=run_id,
        row_id=row_id if type(row_id) is int else None,
        column_id=None,
    )
    attempt = attempt_in_scope(ctx.extras)
    attempt_id = attempt.attempt_id if attempt is not None else None
    return run_id, fact, attempt_id, ctx.extras.get("claim_token")


def record_embedding_batch_in_transaction(
    ctx: ActionContext,
    model_id: str,
    batch: Any,
    texts: list[str],
    *,
    identity_texts: list[str] | None = None,
) -> float:
    prepared = _prepare_embedding_batch_write(
        ctx, model_id, batch, texts, identity_texts=identity_texts
    )
    if prepared is None:
        return 0.0
    run_id, fact, attempt_id, claim_token = prepared
    return RunResultStore(ctx.project).write_returned_call_accounting_in_transaction(
        run_id,
        [{"row_id": fact["row_id"], "column_id": None, "model_calls": [fact]}],
        writer_attempt_id=attempt_id,
        claim_token=claim_token,
        authorized_attempt_id=attempt_id,
    )


def record_embedding_batch(
    ctx: ActionContext,
    model_id: str,
    batch: Any,
    texts: list[str],
    *,
    identity_texts: list[str] | None = None,
) -> float:
    prepared = _prepare_embedding_batch_write(
        ctx, model_id, batch, texts, identity_texts=identity_texts
    )
    if prepared is None:
        return 0.0
    run_id, fact, attempt_id, claim_token = prepared
    return RunResultStore(ctx.project).write_returned_call_accounting(
        run_id,
        [{"row_id": fact["row_id"], "column_id": None, "model_calls": [fact]}],
        writer_attempt_id=attempt_id,
        claim_token=claim_token,
        authorized_attempt_id=attempt_id,
    )
