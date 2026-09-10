"""Durable embedding facts before rebuildable vector-cache publication."""

import hashlib
import json
from typing import Any

from frisket.ops.base import OpContext


def embedding_call_id(ctx: OpContext, model_id: str, texts: list[str]) -> str:
    encoded = json.dumps(
        {"run_id": ctx.extras.get("run_id"), "model_id": model_id, "texts": texts},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"semantic_join_embed_{hashlib.sha256(encoded).hexdigest()}"


def refuse_missing_paid_vectors(
    ctx: OpContext, model_id: str, texts: list[str]
) -> None:
    from frisket.semantic import embedder_is_remote

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


def record_embedding_batch(
    ctx: OpContext,
    model_id: str,
    batch: Any,
    texts: list[str],
    *,
    commit: bool = True,
    identity_texts: list[str] | None = None,
) -> float:
    from frisket.ai.llm.types import provider_from_model_id
    from frisket.ai.models.metadata import ModelCallMeta, PROVIDER_KIND
    from frisket.engine.store.runs import RunResultStore
    from frisket.execution.attempt import attempt_in_scope
    from frisket.semantic import embedder_is_remote

    if not embedder_is_remote(model_id):
        return 0.0
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
    return RunResultStore(ctx.project).write_returned_call_accounting(
        run_id,
        [{"row_id": fact["row_id"], "column_id": None, "model_calls": [fact]}],
        writer_attempt_id=attempt.attempt_id if attempt is not None else None,
        claim_token=ctx.extras.get("claim_token"),
        authorized_attempt_id=attempt.attempt_id if attempt is not None else None,
        commit=commit,
    )
