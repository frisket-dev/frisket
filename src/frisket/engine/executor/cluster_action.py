"""Admission and atomic publication of one prepared clustering operation."""

from __future__ import annotations

import copy
import json
from functools import partial
from types import MappingProxyType

from frisket.actions.cluster_types import (
    ClusterColumn,
    ClusterOptions,
    PreparedClustering,
    ValueClusterer,
)
from frisket.actions.core import OutputField, _ProjectAction
from frisket.actions.types import SheetRows
from frisket.engine.executor.cluster_program import _ClusterProgram
from frisket.engine.executor.map_rows_action import (
    TypedMapRowsPlan,
    TypedMapRowsPlanError,
    normalized_typed_request_identity,
    _TypedExecutionEnvelope,
    _typed_model_cost_error,
    _typed_plan_error,
    _typed_receipt,
    typed_request_hash,
)
from frisket.engine.executor.value_cluster import (
    ClusterSourceChanged,
    cluster_source_snapshot,
    validate_cluster_source,
)
from frisket.contracts.action import ActionError, ReceiptEvidence
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_reservations import (
    _finalize_reserved_action_receipt,
    _output_claim_token,
    _receipt_for_idempotency,
)
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.execution.attempt import StaleAttemptWriter, set_attempt_state


class AdmittedValueClusterer:
    def __init__(self, project, scope):
        self._project = project
        self._scope = scope
        self.plans = {}

    def prepare(self, source: ClusterColumn, *, options: ClusterOptions):
        source = ClusterColumn.model_validate(source)
        options = ClusterOptions.model_validate(options.model_dump()).model_copy(
            deep=True
        )
        if not isinstance(self._scope, SheetRows) or self._scope.row_ids is not None:
            raise ValueError("Clustering requires the whole source column")
        source_fact, _ = cluster_source_snapshot(
            self._project, self._scope.sheet_id, source
        )
        if options.review and options.review.source_hash is not None:
            if options.review.source_hash != source_fact["value_hash"]:
                raise TypedMapRowsPlanError(
                    "stale_input",
                    "The reviewed cluster source changed; review its groups again",
                )
        token = PreparedClustering(row_count=len(source_fact["row_ids"]))
        self.plans[token] = {"source": source_fact, "options": options.model_dump()}
        return token


def prepare_cluster_action(project, bound, *, router=None, admission=True):
    from frisket.semantic import embedder_is_remote, resolve_embedder
    from frisket.engine.runner.validation import NetworkDisabled

    terminal = bound.action.definition.run
    if not isinstance(terminal, _ProjectAction) or terminal.capabilities != (
        ValueClusterer,
    ):
        raise TypeError("Expected a ValueClusterer action")
    capability = AdmittedValueClusterer(project, bound.request.scope)
    token = terminal.handler(bound.params.model_copy(deep=True), capability)
    if not isinstance(token, PreparedClustering) or token not in capability.plans:
        raise ValueError(
            "The action must return its invocation-owned prepared clustering"
        )
    state = copy.deepcopy(capability.plans[token])
    model = None
    if state["options"]["method"] == "semantic":
        backend = resolve_embedder(router, allow_remote=True)
        if backend is None:
            raise TypedMapRowsPlanError(
                "embedding_backend_unavailable",
                "Semantic clustering requires an embedding backend",
            )
        model = backend[1]
        if embedder_is_remote(model) and project.effective_network_policy() == "off":
            raise TypedMapRowsPlanError(
                "network_disabled", str(NetworkDisabled("model:embed"))
            )
    state["embedding_model"] = model
    name = bound.request.output_names.get("canonical", "canonical")
    if name == state["source"]["name"]:
        raise TypedMapRowsPlanError(
            "output_column_exists", "Clustering must preserve its source column"
        )
    if admission and any(
        col["name"] == name for col in project.columns(state["source"]["sheet_id"])
    ):
        raise TypedMapRowsPlanError(
            "output_column_exists", "The canonical output column already exists"
        )
    return cluster_plan_from_state(bound, state)


def cluster_plan_from_state(bound, state):
    """Reconstruct the internal executable, never authorize job-supplied state."""
    source = state["source"]
    final_name = bound.request.output_names.get("canonical", "canonical")
    names = {"canonical": final_name}
    fields = (
        {
            "name": final_name,
            "column_type": "text",
            "schema": {},
            "description": "",
            "publication_required": True,
            "hidden": False,
        },
    )
    output_fields = (OutputField("canonical", "text", {}, str),)
    identity = normalized_typed_request_identity(bound)
    spec = {
        "action_kind": bound.action.action_id,
        "action_version": "1",
        "sheet_id": source["sheet_id"],
        "input_columns": [source["name"]],
        "params": identity["params"],
        "output_names": names,
        "output_target_preconditions": {final_name: None},
        "replace_existing": False,
        "cluster_values": copy.deepcopy(state),
        "deferred_publication": True,
        "idempotency_key": bound.request.idempotency_key,
    }
    if bound.request.confirmation is not None:
        spec["consented_promise_set_hash"] = bound.request.confirmation
    program = _ClusterProgram(bound.action, bound.params, names, output_fields, fields)
    return TypedMapRowsPlan(
        action=bound.action,
        request=bound.request,
        source_columns=(source["name"],),
        source_column_ids=MappingProxyType({source["name"]: source["column_id"]}),
        source_column_types=MappingProxyType({source["name"]: source["type"]}),
        output_names=MappingProxyType(names),
        output_target_preconditions=MappingProxyType({final_name: None}),
        output_fields=fields,
        request_identity=MappingProxyType(identity),
        evaluation_context=None,
        spec=MappingProxyType(spec),
        program=program,
    )


def _replay_error(project, receipt):
    if receipt.status in {"queued", "running", "failed", "cancelled"}:
        return None
    from frisket.engine.executor.cluster_receipt_read import _published_clusters
    from frisket.actions.types import TableError

    try:
        _published_clusters(project, receipt.receipt_id)
    except TableError as exc:
        return ActionError(
            code="stale_replay", message=str(exc), action_kind=receipt.action_kind
        )
    return None


def finalize_cluster_action(project, action, _params, *, bound, **kwargs):
    """Publish canonical heads and their actual groups in one writer fence."""
    from frisket.ops.base import persisted_recipe_invocation_halt
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.executor.project_run_terminalization import (
        CurrentWriterTerminalAuthority,
        terminalize_project_run,
    )

    state = kwargs["resolved"].facts["cluster_values"]
    run_id, receipt_id = kwargs["run_id"], kwargs["receipt_id"]
    writer, claim = kwargs.get("writer_attempt_id"), kwargs.get("claim_token")
    if writer is None or claim != _output_claim_token(receipt_id):
        raise StaleAttemptWriter("Cluster publication needs its exact writer claim")

    def abort(code, message, *, status="failed"):
        project.db.execute("BEGIN IMMEDIATE")
        try:
            prior = ReceiptStore(project).parsed_by_id(receipt_id)
            terminal = prior.model_copy(
                update={
                    "status": status,
                    "outputs": [],
                    "provider_use": _cluster_provider_use(project, run_id),
                    "errors": [
                        ActionError(code=code, message=message, action_kind=action.kind)
                    ],
                }
            )
            outcome = terminalize_project_run(
                project,
                run_id=run_id,
                receipt_id=receipt_id,
                status=status,
                authority=CurrentWriterTerminalAuthority(
                    writer_attempt_id=writer, claim_token=claim
                ),
                terminal_receipt=terminal,
                commit=False,
            )
            if outcome.disposition not in {"terminalized", "already_terminal"}:
                if outcome.reason == "reserved_effect_checkpoint":
                    project.db.rollback()
                    # This response reports a refusal, not a terminal receipt.
                    # Keep its reservation identity so generic error cleanup
                    # cannot discard the unresolved paid-effect authority.
                    return _result_from_receipt(terminal).model_copy(
                        update={
                            "errors": [
                                ActionError(
                                    code="idempotency_checkpoint_ambiguous",
                                    message="An embedding may have been billed but its response is unknown; reconcile this effect before retrying.",
                                    action_kind=action.kind,
                                    details={
                                        "run_id": run_id,
                                        "receipt_id": receipt_id,
                                    },
                                )
                            ],
                        }
                    )
                raise StaleAttemptWriter(f"Cluster abort refused: {outcome.reason}")
            project.db.commit()
        except BaseException:
            project.db.rollback()
            raise
        return _result_from_receipt(ReceiptStore(project).parsed_by_id(receipt_id))

    run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    from frisket.engine.store.effect_checkpoints import (
        EffectCheckpointStore,
        is_operator_attestation,
    )

    if any(
        checkpoint["state"] == "consumed"
        and is_operator_attestation(checkpoint.get("payload"))
        for checkpoint in EffectCheckpointStore(project.db).list_group(
            family="model_call", group_key=str(run_id)
        )
    ):
        return abort(
            "idempotency_checkpoint_operator_decided",
            "An operator accepted this embedding as charged; no clustering result was produced.",
        )
    if halt := persisted_recipe_invocation_halt(run["params"]):
        return abort(*halt)
    if run["status"] != "completed":
        return abort(
            "cluster_failed",
            "Clustering did not complete the whole column",
            status="cancelled" if run["status"] == "cancelled" else "failed",
        )
    plan = cluster_plan_from_state(bound, state)
    try:
        project.db.execute("BEGIN IMMEDIATE")
        claims = OutputColumnClaimStore(project)
        column_ids = claims.active_column_ids(claim_token=claim, run_id=run_id)
        claims.require_current_writer(
            project.db,
            run_id=run_id,
            writer_attempt_id=writer,
            claim_token=claim,
            output_column_ids=column_ids,
        )
        validate_cluster_source(project, state["source"])
        op = project.db.execute(
            "SELECT spec,undo_info FROM ops WHERE id=?", (run["op_id"],)
        ).fetchone()
        fact = json.loads(op["spec"]).get("value_clusters_result")
        if not fact or fact["source"] != state["source"]:
            raise ClusterSourceChanged("Clustering has no complete admitted result")
        if [item["row_id"] for item in fact["canonical_values"]] != state["source"][
            "row_ids"
        ]:
            raise ClusterSourceChanged(
                "Clustering did not cover the whole source column"
            )
        ResultGenerationStore(project).seal(
            run_id,
            column_ids,
            claim_token=claim,
            terminal_disposition="completed",
            publish=True,
            commit=False,
        )
        for column_id in json.loads(op["undo_info"] or "{}").get("created_columns", []):
            if column_id in column_ids:
                project.db.execute(
                    "UPDATE columns SET hidden=0 WHERE id=?", (column_id,)
                )
        actual = project.get_values(
            state["source"]["sheet_id"], fact["output"]["column_id"]
        )
        if any(
            actual.get(item["row_id"]) != item["value"]
            for item in fact["canonical_values"]
        ):
            raise ClusterSourceChanged(
                "Published canonical values differ from actual groups"
            )
        receipt = _typed_receipt(
            project,
            run_id,
            kwargs["action_id"],
            receipt_id,
            project_id=kwargs["project_id"],
            plan=plan,
            params_hash=kwargs["params_hash"],
        )
        provider_use = _cluster_provider_use(project, run_id)
        if not provider_use and state["embedding_model"] is not None:
            from frisket.semantic import embedder_is_remote
            from frisket.ai.llm.types import provider_from_model_id

            if embedder_is_remote(state["embedding_model"]):
                provider_use = [
                    {
                        "provider": provider_from_model_id(state["embedding_model"]),
                        "model": state["embedding_model"],
                        "service": "frisket.cluster.semantic.embedding_cache",
                        "credential_source": "cache",
                        "external_api": False,
                        "model_call_count": 0,
                        "cost_actual": 0.0,
                    }
                ]
        from frisket.engine.store.effect_checkpoints import EffectCheckpointStore

        checkpoints = EffectCheckpointStore(project.db)
        for checkpoint_id in fact.get("embedding_checkpoint_ids", []):
            checkpoint = checkpoints.get(checkpoint_id)
            if checkpoint is None or checkpoint["group_key"] != str(run_id):
                raise StaleAttemptWriter(
                    "Cluster embedding checkpoint is missing or belongs to another run"
                )
            checkpoints.consume_and_retire(
                checkpoint_id,
                family="model_call",
                group_key=str(run_id),
                unit_key=checkpoint_id,
                action_kind=action.kind,
                identity=checkpoint["identity"],
                run_id=run_id,
                writer_attempt_id=writer,
                claim_token=claim,
                commit=False,
            )
        receipt = receipt.model_copy(
            update={
                "evidence": [
                    *receipt.evidence,
                    ReceiptEvidence(ref=fact, retention="pinned"),
                ],
                "provider_use": provider_use or receipt.provider_use,
            }
        )
        result = _finalize_reserved_action_receipt(
            project,
            action,
            params_hash=kwargs["params_hash"],
            project_id=kwargs["project_id"],
            receipt=receipt,
            reservation_lost_message="Cluster reservation was lost",
            update_failed_message="Cluster receipt update failed",
            require_running_status=False,
            replay_error_fn=lambda prior: _replay_error(project, prior),
            commit=False,
        )
        if result is not None:
            project.db.rollback()
            return result
        set_attempt_state(project, writer, "effected", commit=False)
        if not claims.release(claim_token=claim, status="released", commit=False):
            raise StaleAttemptWriter("Cluster publication lost its output claim")
        project.db.commit()
        return _result_from_receipt(receipt)
    except ClusterSourceChanged as exc:
        project.db.rollback()
        return abort("stale_input", str(exc))
    except BaseException:
        project.db.rollback()
        raise


def _cluster_provider_use(project, run_id):
    from frisket.engine.executor.action_support import _routed_call_provider_use

    calls = project.db.execute(
        "SELECT * FROM model_calls WHERE run_id=? ORDER BY id", (run_id,)
    ).fetchall()
    return _routed_call_provider_use(calls, capability="llm.embed")


def typed_cluster_queue_spec(bound, *, initial_plan=None, cluster_state=None):
    from frisket.engine.executor.action_inventory import (
        _QueuedActionSpec,
        _queued_payload_codecs,
    )

    # Only static result fields exist before the actual preparation runs.
    name = bound.request.output_names.get("canonical", "canonical")
    fields = ({"name": name, "column_type": "text", "schema": {}, "hidden": False},)
    program = _ClusterProgram(
        bound.action, bound.params, {"canonical": name}, bound.output_fields, fields
    )
    runner_spec = (
        initial_plan.spec_dict()
        if initial_plan
        else {
            "action_kind": bound.action.action_id,
            "sheet_id": bound.request.scope.sheet_id,
            "params": dict(normalized_typed_request_identity(bound)["params"]),
            "output_names": {"canonical": name},
            "replace_existing": False,
            "idempotency_key": bound.request.idempotency_key,
            "deferred_publication": True,
        }
    )
    if cluster_state is not None:
        runner_spec.update(cluster_plan_from_state(bound, cluster_state).spec_dict())
    envelope = _TypedExecutionEnvelope(
        kind=bound.action.action_id,
        idempotency_key=bound.request.idempotency_key,
        params=MappingProxyType(dict(bound.request.params)),
    )

    def resolve(project, _params, spec, router):
        from frisket.engine.runner import ProviderKeyRefusal

        try:
            plan = initial_plan or prepare_cluster_action(project, bound, router=router)
            validate_cluster_source(project, plan.spec["cluster_values"]["source"])
            spec.clear()
            spec.update(plan.spec_dict())
            return {
                "cluster_values": spec["cluster_values"],
                "resume_run_id": _cluster_resume_run_id(
                    project, bound, spec["cluster_values"]
                ),
                "input_column_ids": dict(plan.source_column_ids),
                "input_column_types": dict(plan.source_column_types),
                "output_names": dict(plan.output_names),
                "output_target_preconditions": dict(plan.output_target_preconditions),
            }
        except ProviderKeyRefusal as exc:
            return ActionError(
                code=exc.error_code,
                message=exc.action_message(),
                action_kind=bound.action.action_id,
                field=exc.field,
                details=dict(exc.details),
            )
        except (TypeError, ValueError) as exc:
            return _typed_plan_error(bound.action.action_id, exc)

    def guard(project, payload, _params):
        try:
            stored = project.db.execute(
                "SELECT ops.spec FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.id=?",
                (payload["run_id"],),
            ).fetchone()
            state = json.loads(stored["spec"])["cluster_values"]
            if (
                state != payload["v1_cluster_values"]
                or state != payload["spec"]["cluster_values"]
            ):
                raise ValueError(
                    "Queued clustering differs from its durable admitted operation"
                )
            validate_cluster_source(project, state["source"])
        except (KeyError, TypeError, ValueError) as exc:
            return ActionError(
                code="stale_input", message=str(exc), action_kind=bound.action.action_id
            )
        return None

    spec = _QueuedActionSpec(
        kind=bound.action.action_id,
        params_model=type(bound.params),
        completed_spec=None,
        runner_spec_fn=lambda _params: dict(runner_spec),
        resolve_fn=resolve,
        precheck_fn=lambda *args, **kwargs: None,
        reservation_kind="typed_cluster_reservation",
        queue_job_kind="typed_cluster_queue_job",
        payload_codecs=_queued_payload_codecs(
            "cluster_values",
            "input_column_ids",
            "input_column_types",
            "output_names",
            "output_target_preconditions",
        ),
        finalize_action=partial(finalize_cluster_action, bound=bound),
        pre_run_guard=guard,
        params_hash_fn=lambda _action: typed_request_hash(bound),
        confirmed_fn=lambda _params: bound.request.confirmation is not None,
        cost_gate_error_fn=_typed_model_cost_error,
        map_error_code="cluster_failed",
        output_claim_error_field="output_names",
        resolve_needs_router=True,
    )
    return envelope, spec, program


def _cluster_resume_run_id(project, bound, state):
    prior = _cluster_abandoned_run(project, bound, state=state)
    return int(prior["id"]) if prior is not None else None


def _cluster_abandoned_run(project, bound, *, state=None):
    """An abandoned invocation keeps its paid-call and vector-cache fence."""
    from frisket.engine.executor.map_rows_action import (
        bound_typed_program_request_from_runner_spec,
    )

    matches = []
    for run in project.db.execute(
        "SELECT runs.id,runs.edition_run_context,ops.spec FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.action_kind=? AND runs.status='running'",
        (bound.action.action_id,),
    ).fetchall():
        spec = json.loads(run["spec"])
        if spec.get("idempotency_key") != bound.request.idempotency_key:
            continue
        original = bound_typed_program_request_from_runner_spec(spec)
        if (
            original is None
            or typed_request_hash(original) != typed_request_hash(bound)
            or state is not None
            and spec.get("cluster_values") != state
        ):
            raise ValueError(
                "Abandoned clustering differs from this request or its source"
            )
        matches.append(run)
    if len(matches) > 1:
        raise ValueError("Clustering has ambiguous abandoned runs")
    return matches[0] if matches else None


def run_typed_cluster_action(
    project, project_id, bound, router, map_runner_factory, *, edition_run_context=None
):
    from frisket.engine.executor.action_inventory import (
        _ReservedMaprunnerActionSpec,
        ExecutorContext,
        ExecutorDeps,
    )
    from frisket.engine.executor.action_lifecycle import (
        _run_reserved_maprunner_action_spec,
    )

    envelope, queued, program = typed_cluster_queue_spec(bound)
    try:
        prior_run = _cluster_abandoned_run(project, bound)
    except ValueError:
        # Normal request admission below reports a conflicting/corrupt run;
        # it must not borrow that run's edition authority first.
        prior_run = None
    if prior_run is not None:
        edition_run_context = (
            json.loads(prior_run["edition_run_context"])
            if prior_run["edition_run_context"] is not None
            else None
        )
    existing = _receipt_for_idempotency(project, bound.request.idempotency_key)
    if existing is not None and existing["status"] == "running":
        from frisket.contracts.action import Receipt
        from frisket.engine.executor.action_specs import ResolvedAction

        receipt = Receipt.model_validate(json.loads(existing["body"]))
        if (
            receipt.params_hash == typed_request_hash(bound)
            and receipt.run_id is not None
        ):
            run = project.db.execute(
                "SELECT runs.*,ops.spec AS runner_spec FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.id=?",
                (receipt.run_id,),
            ).fetchone()
            if (
                run is not None
                and run["status"] in {"completed", "failed", "cancelled"}
                and run["current_attempt_id"] is not None
                and any(
                    binding.state != "sealed"
                    for binding in ResultGenerationStore(project).bindings_for_run(
                        receipt.run_id
                    )
                )
            ):
                return finalize_cluster_action(
                    project,
                    envelope,
                    bound.params,
                    bound=bound,
                    run_id=receipt.run_id,
                    action_id=receipt.action_id,
                    receipt_id=receipt.receipt_id,
                    params_hash=receipt.params_hash,
                    project_id=project_id,
                    writer_attempt_id=run["current_attempt_id"],
                    claim_token=_output_claim_token(receipt.receipt_id),
                    resolved=ResolvedAction.from_resolve_dict(
                        {
                            "cluster_values": json.loads(run["runner_spec"])[
                                "cluster_values"
                            ]
                        }
                    ),
                )

    def factory(project, router):
        runner = map_runner_factory(project, router)
        runner.allow_action_lifecycle_only_recipes = True
        return runner

    spec = _ReservedMaprunnerActionSpec(
        kind=bound.action.action_id,
        params_model=type(bound.params),
        runner_spec_fn=queued.runner_spec_fn,
        resolve_fn=lambda project, params, spec: queued.resolve_fn(
            project, params, spec, router
        ),
        precheck_fn=queued.precheck_fn,
        write_fn=partial(finalize_cluster_action, bound=bound),
        reservation_kind=queued.reservation_kind,
        program_fn=lambda project, spec: program,
        replay_error_fn=lambda project, params, receipt, action: _replay_error(
            project, receipt
        ),
        params_hash_fn=queued.params_hash_fn,
        confirmed_fn=queued.confirmed_fn,
        cost_gate_error_fn=queued.cost_gate_error_fn,
        resume_run_id_fn=lambda _project, _params, resolved: resolved["resume_run_id"],
        map_error_code="cluster_failed",
    )
    return _run_reserved_maprunner_action_spec(
        project,
        envelope,
        bound.params,
        spec=spec,
        ctx=ExecutorContext(
            project_id=project_id,
            deps=ExecutorDeps(router=router, map_runner_factory=factory),
            edition_run_context=edition_run_context,
        ),
        map_runner_factory=factory,
    )
