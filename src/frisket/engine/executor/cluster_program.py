"""Internal whole-column execution for an admitted clustering operation."""

from __future__ import annotations

import copy
import json
import hashlib
import inspect
from contextlib import asynccontextmanager

from frisket.actions.cluster_types import ClusterOptions
from frisket.engine.executor.embedding_batches import (
    record_embedding_batch,
)
from frisket.engine.executor.map_rows_action import _TypedMapBatchProgram
from frisket.engine.executor.value_cluster import (
    ClusterSourceChanged,
    compute_reviewed_clusters,
    cluster_embedding_inputs,
    missing_cluster_vectors,
    validate_cluster_source,
)
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.execution.attempt import attempt_in_scope
from frisket.ops.base import RecipeInvocationHalt
from frisket.ops.cost_source import free_local_estimate, unknown_cost_estimate


def _cache_only_embedding(_texts):
    raise RuntimeError(
        "Clustering lost admitted vectors before computation; refusing another embedding call"
    )


class _ClusterEmbeddingBatch:
    """The existing paid checkpoint lifecycle around one fixed admitted batch."""

    def __init__(self, ctx, state, texts, embed, action_kind):
        from frisket.engine.store.effect_checkpoints import (
            EffectCheckpointStore,
            canonical_json,
        )

        self.ctx, self.embed, self.model = ctx, embed, state["embedding_model"]
        self.store = EffectCheckpointStore(ctx.project.db)
        self.identity = canonical_json(
            {"run_id": ctx.extras["run_id"], "state": state, "texts": texts}
        )
        self.id = "cluster_embed_" + hashlib.sha256(self.identity.encode()).hexdigest()
        self.texts = texts
        self.unit = dict(
            family="model_call",
            group_key=str(ctx.extras["run_id"]),
            unit_key=self.id,
            action_kind=action_kind,
            identity=self.identity,
        )
        attempt = attempt_in_scope(ctx.extras)
        self.fence = dict(
            run_id=ctx.extras["run_id"],
            writer_attempt_id=attempt.attempt_id if attempt else None,
            claim_token=ctx.extras["claim_token"],
        )

    async def __call__(self, texts):
        from frisket.engine.store.effect_checkpoints import is_operator_attestation

        checkpoint = self.store.get(self.id)
        if checkpoint is None:
            created = self.store.reserve(
                self.id,
                **self.unit,
                **self.fence,
                authorized_attempt_id=self.fence["writer_attempt_id"],
                payload={"texts": self.texts},
            )
            if not created:
                checkpoint = self.store.get(self.id)
                if checkpoint is None:
                    raise RecipeInvocationHalt(
                        "idempotency_checkpoint_invalid",
                        "The embedding reservation disappeared before recovery",
                    )
        if checkpoint is not None:
            if checkpoint["identity"] != self.identity:
                raise RecipeInvocationHalt(
                    "idempotency_checkpoint_invalid",
                    "Embedding checkpoint identity changed",
                )
            if checkpoint["state"] == "consumed" and is_operator_attestation(
                checkpoint.get("payload")
            ):
                raise RecipeInvocationHalt(
                    "idempotency_checkpoint_operator_decided",
                    "An operator accepted this embedding as charged; reconcile it before retrying",
                )
            if checkpoint["state"] == "reserved":
                raise RecipeInvocationHalt(
                    "idempotency_checkpoint_ambiguous",
                    "A previous embedding has an unknown provider outcome; refusing another call",
                )
            recovered = self.store.returned_for_replay(**self.unit)["payload"]
            try:
                vectors = dict(
                    zip(recovered["returned_inputs"], recovered["vectors"], strict=True)
                )
                return [vectors[text] for text in texts]
            except (KeyError, TypeError, ValueError) as exc:
                raise RecipeInvocationHalt(
                    "idempotency_checkpoint_invalid",
                    "Returned embeddings do not cover the missing vectors",
                ) from exc
        async_embed = getattr(self.embed, "embed_batch_async", None)
        result = async_embed(texts) if callable(async_embed) else self.embed(texts)
        if inspect.isawaitable(result):
            result = await result
        vectors = result.get("vectors") if isinstance(result, dict) else result
        payload = {"returned_inputs": texts, "vectors": vectors}
        try:
            json.dumps(payload, allow_nan=False)
        except (TypeError, ValueError):
            payload = {"invalid_response": True}
        self.store.complete(
            self.id,
            **self.unit,
            **self.fence,
            payload=payload,
            accrue=lambda _checkpoint: record_embedding_batch(
                self.ctx,
                self.model,
                result,
                texts,
                commit=False,
                identity_texts=self.texts,
            ),
        )
        return result


def cluster_fact(project, state, computation, *, run_id, output_name):
    column = project.db.execute(
        "SELECT id,type FROM columns WHERE sheet_id=? AND name=?",
        (state["source"]["sheet_id"], output_name),
    ).fetchone()
    if column is None:
        raise RuntimeError("The prepared canonical output disappeared")
    return {
        "kind": "value_clusters",
        "run_id": run_id,
        "source": copy.deepcopy(state["source"]),
        "options": copy.deepcopy(state["options"]),
        "clusters": copy.deepcopy(computation.clusters),
        "envelope": copy.deepcopy(computation.envelope),
        "canonical_values": [
            {"row_id": row_id, "value": value}
            for row_id, value in computation.values.items()
        ],
        "output": {
            "sheet_id": state["source"]["sheet_id"],
            "column_id": column["id"],
            "name": output_name,
            "type": column["type"],
            "run_id": run_id,
        },
    }


def persist_cluster_fact(project, fact, *, writer_attempt_id, claim_token):
    db = project.db
    db.execute("SAVEPOINT value_clusters_evidence")
    try:
        OutputColumnClaimStore.require_current_writer(
            db,
            run_id=fact["run_id"],
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            output_column_ids=[fact["output"]["column_id"]],
        )
        run = db.execute(
            "SELECT ops.id,ops.spec FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.id=?",
            (fact["run_id"],),
        ).fetchone()
        provenance = json.loads(run["spec"])
        if (
            "value_clusters_result" in provenance
            and provenance["value_clusters_result"] != fact
        ):
            raise RuntimeError(
                "The prepared cluster operation already recorded different groups"
            )
        provenance["value_clusters_result"] = fact
        db.execute(
            "UPDATE ops SET spec=? WHERE id=?",
            (json.dumps(provenance, sort_keys=True, allow_nan=False), run["id"]),
        )
        db.execute("RELEASE SAVEPOINT value_clusters_evidence")
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT value_clusters_evidence")
        db.execute("RELEASE SAVEPOINT value_clusters_evidence")
        raise


class _ClusterProgram(_TypedMapBatchProgram):
    """Reuse batch result storage; only the returned prepared operation executes."""

    consumes_resolution = False
    cost_class = "metered"
    llm = False
    auto_verify = True
    allow_all_empty_input = True
    defer_generation_seal = True
    keep_zero_success_output_columns = True

    def estimate(self, project, spec, rows, *, resolution=None):
        from frisket.semantic import embedder_is_remote

        model = spec["cluster_values"]["embedding_model"]
        if model is None or not embedder_is_remote(model):
            return free_local_estimate()
        state = spec["cluster_values"]
        values = validate_cluster_source(project, state["source"])
        options = ClusterOptions.model_validate(state["options"])
        texts = cluster_embedding_inputs(values, options)
        missing = missing_cluster_vectors(project, texts, model)
        # A returned paid batch can fill the cache before publication crashes.
        # Resuming that same operation retains its admitted quote; cache state
        # must not silently turn the consent identity into a different quote.
        recovering_paid_batch = any(
            (stored := json.loads(row["spec"])).get("idempotency_key")
            == spec.get("idempotency_key")
            and stored.get("cluster_values") == state
            for row in project.db.execute(
                "SELECT ops.spec FROM runs JOIN ops ON ops.id=runs.op_id "
                "JOIN effect_checkpoints ON effect_checkpoints.group_key=CAST(runs.id AS TEXT) "
                "WHERE runs.action_kind=? AND runs.status='running' "
                "AND effect_checkpoints.family='model_call' "
                "AND effect_checkpoints.action_kind=runs.action_kind",
                (spec["action_kind"],),
            ).fetchall()
        )
        # Embedding provider cost is unknown until the admitted batch returns.
        # The ordinary run gate owns the exact confirmation of this scope.
        return (
            unknown_cost_estimate(
                engine=model,
                requires_confirmation=True,
                remote_capability="model:embed",
            )
            if missing or recovering_paid_batch
            else free_local_estimate()
        )

    def run_provenance_model(self, spec):
        return spec["cluster_values"]["embedding_model"]

    @asynccontextmanager
    async def execution_scope(self, spec, ctx, *, expected_rows):
        try:
            validate_cluster_source(ctx.project, spec["cluster_values"]["source"])
        except ClusterSourceChanged as exc:
            raise RecipeInvocationHalt("promise_violation", str(exc)) from exc
        yield

    async def execute_batch(self, values_by_row, spec, ctx):
        from frisket.ops.cluster import SEMANTIC_EMBED_BATCH_SIZE
        from frisket.semantic import (
            _doc_vectors_async,
            embedder_is_remote,
            resolve_embedder,
        )

        state = spec["cluster_values"]
        options = ClusterOptions.model_validate(state["options"])
        values = validate_cluster_source(ctx.project, state["source"])
        source_name = state["source"]["name"]
        if set(values_by_row) != set(values) or any(
            row.get(source_name) != values[row_id]
            for row_id, row in values_by_row.items()
        ):
            raise RecipeInvocationHalt(
                "promise_violation",
                "Clustering rows differ from the admitted full column",
            )
        model = state["embedding_model"]
        checkpoints = []
        if options.method == "semantic":
            backend = resolve_embedder(ctx.extras.get("router"), allow_remote=True)
            if backend is None or backend[1] != model:
                raise RecipeInvocationHalt(
                    "promise_violation", "The admitted embedding backend changed"
                )
            remote = embedder_is_remote(model)
            if remote and ctx.extras.get("preview") is True:
                raise ValueError("Cluster review preview only uses local embeddings")
            texts = cluster_embedding_inputs(values, options)

            def before_batch(batch):
                if remote:
                    if embed.store.get(embed.id) is not None:
                        # Recovery returns already-paid vectors; it is not egress.
                        return
                    from frisket.ai.llm.types import provider_from_model_id
                    from frisket.engine.runner.validation import (
                        NetworkDisabled,
                        assert_provider_spend_cap,
                    )

                    if ctx.project.effective_network_policy() == "off":
                        raise RecipeInvocationHalt(
                            "network_disabled", str(NetworkDisabled("model:embed"))
                        )
                    assert_provider_spend_cap(
                        ctx.project, provider_from_model_id(model)
                    )

            for offset in range(0, len(texts), SEMANTIC_EMBED_BATCH_SIZE):
                cancelled = ctx.extras.get("cancelled")
                if cancelled and cancelled():
                    import asyncio

                    raise asyncio.CancelledError
                batch_texts = texts[offset : offset + SEMANTIC_EMBED_BATCH_SIZE]
                embed = (
                    _ClusterEmbeddingBatch(
                        ctx, state, batch_texts, backend[0], spec["action_kind"]
                    )
                    if remote
                    else backend[0]
                )
                await _doc_vectors_async(
                    ctx.project,
                    [{"content": text} for text in batch_texts],
                    embed,
                    model,
                    before_fresh_batch=before_batch,
                )
                if remote and embed.store.get(embed.id) is not None:
                    checkpoints.append(embed.id)
        computation = compute_reviewed_clusters(
            ctx.project,
            sheet_id=state["source"]["sheet_id"],
            column=source_name,
            source_values=values,
            options=options,
            embed=_cache_only_embedding if model else None,
            embed_id=model,
        )
        if ctx.extras.get("preview") is not True:
            attempt = attempt_in_scope(ctx.extras)
            fact = cluster_fact(
                ctx.project,
                state,
                computation,
                run_id=ctx.extras["run_id"],
                output_name=self._output_names["canonical"],
            )
            fact["embedding_checkpoint_ids"] = checkpoints
            persist_cluster_fact(
                ctx.project,
                fact,
                writer_attempt_id=attempt.attempt_id if attempt is not None else None,
                claim_token=ctx.extras["claim_token"],
            )
        return {
            row_id: {self._output_names["canonical"]: value}
            for row_id, value in computation.values.items()
        }
