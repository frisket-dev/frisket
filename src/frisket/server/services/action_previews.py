"""Action estimate preview services."""

from __future__ import annotations

from typing import Any

from frisket.actions.core import SemanticJoin
from frisket.actions.registry import NEW_ACTION_IDS
from frisket.actions.system import action_id_from_request, typed_action_for_request
from frisket.contracts.action import (
    ActionError,
)
from frisket.authoring.action_metadata import (
    action_available_in_edition,
    action_edition_unavailable_message,
)
from frisket.engine.executor.run_backfill_action import (
    BackfillRefused,
    backfill_program,
    prepare_backfill_action,
    supports_typed_backfill_action,
)
from frisket.engine.executor.map_rows_action import (
    build_typed_map_rows_plan,
    bound_typed_program_request_from_runner_spec,
)
from frisket.engine.executor.selection_scope import resolve_action_selection
from frisket.actions.types import (
    IndexCreator,
    IndexRefresher,
    RowError,
    require_sheet_rows,
)
from frisket.contracts.http.action_estimate_validation import ActionEstimate
from frisket.engine.executor.action_families.embeddings import (
    prepare_embedding_create,
    prepare_embedding_refresh,
)
from frisket.engine.executor.action_support import _row_input_action_error
from frisket.engine.runner import MapRunner
from frisket.engine.runner.confirmation_context import (
    BILLED_COST_KEY,
    POLICY_ID_KEY,
)
from frisket.engine.runner.validation import (
    confirmation_estimate,
    action_program_for_spec,
)
from frisket.server.workspace import Workspace
from frisket.engine.store import Project
from frisket.engine.views.specs import SelectionUnavailable
from frisket.server.route_errors import RouteError
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.execution.pricing_policy import default_pricing_policy


ACTION_ESTIMATE_RESULT_SCHEMA_VERSION = "frisket.action_estimate_result.v1"


class ActionPreviewRouteError(RouteError):
    pass


def _action_error(status_code: int, detail: str) -> ActionPreviewRouteError:
    return ActionPreviewRouteError(status_code, detail)


def _prepared_model_estimate(
    bound: Any, prepared: Any, estimate: dict[str, Any], *, consent_coverage
) -> dict[str, Any]:
    """Expose the actual prepared quote with the execution gate's consent identity."""
    from frisket.contracts.http.action_estimate_validation import ActionEstimate
    from frisket.engine.executor.map_rows_action import typed_request_hash
    from frisket.engine.runner.confirmation_context import (
        ActionScope,
        action_confirmation,
        mint_confirmation_hash,
        estimated_usd,
        rate_estimate,
    )

    if POLICY_ID_KEY not in estimate or BILLED_COST_KEY not in estimate:
        estimate = rate_estimate(estimate, policy=default_pricing_policy())
    token = mint_confirmation_hash(
        action_confirmation(
            family_kind=bound.action.action_id,
            scope=ActionScope(action_hash=typed_request_hash(bound)),
            estimate=estimate,
        )
    )
    price = estimated_usd(estimate)
    return ActionEstimate.model_validate(
        {
            **{
                key: value
                for key, value in estimate.items()
                if key in ActionEstimate.model_fields
            },
            "rows": estimate.get("rows", len(prepared.resolved["source_row_ids"]))
            if hasattr(prepared, "resolved")
            else estimate["rows"],
            "promise_set_hash": token,
            "requires_confirmation": (
                price is None or price > float(consent_coverage.threshold_usd)
            )
            and bound.request.confirmation != token,
        }
    ).model_dump(mode="json")


class ActionPreviewService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def estimate(
        self,
        project_id: str,
        action: dict[str, Any],
        *,
        request_context: Any = None,
    ) -> dict[str, Any]:
        kind = action_id_from_request(action) if isinstance(action, dict) else None
        if not action_available_in_edition(kind, self._workspace.edition):
            raise _action_error(
                400,
                action_edition_unavailable_message(kind, self._workspace.edition),
            )
        project: Project | None = None

        def get_project() -> Project:
            nonlocal project
            if project is None:
                project = self._workspace.get(project_id)
            return project

        from frisket.execution.consent_coverage import effective_consent_coverage

        factory = self._workspace.executor_deps_factory
        coverage = effective_consent_coverage(
            get_project(),
            factory(project_id, request_context).consent_coverage
            if factory is not None
            else None,
        )

        typed_plan = None
        typed = None
        backfill_plan = None
        from frisket.actions.types import ActionRequest
        from frisket.authoring.workbench.installed_actions import bind_installed_action

        try:
            typed = (
                typed_action_for_request(action)
                if kind in NEW_ACTION_IDS
                else bind_installed_action(
                    get_project(), ActionRequest.model_validate(action)
                )
                if isinstance(action.get("action_id"), str)
                else None
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _action_error(400, str(exc)) from exc
        if typed is not None:
            try:
                from frisket.actions.core import ColumnTransform
                from frisket.engine.executor.column_transform_action import (
                    preflight_column_transform_request,
                )

                if isinstance(typed.action.definition.run, ColumnTransform):
                    preflight = preflight_column_transform_request(get_project(), typed)
                    if isinstance(preflight, ActionError):
                        raise ActionPreviewRouteError(
                            400,
                            preflight.model_dump(mode="json"),
                            bare_json=True,
                        )
                    return {
                        "schema_version": ACTION_ESTIMATE_RESULT_SCHEMA_VERSION,
                        "action": {"kind": typed.action.action_id},
                        "project_id": project_id,
                        "estimate": {
                            "rows": len(
                                get_project().visible_row_ids(
                                    require_sheet_rows(typed.request.scope).sheet_id
                                )
                            ),
                            "cost": 0.0,
                            "cost_source": "free_local",
                            "billed_cost": 0,
                            "policy_id": default_pricing_policy().policy_id,
                            "requires_confirmation": False,
                        },
                    }
                from frisket.actions.cluster_types import ValueClusterer
                from frisket.actions.find_types import FindScanner
                from frisket.actions.group_summary_types import GroupSummarizer
                from frisket.engine.executor.find_action import (
                    FindRefused,
                    prepare_find_action,
                )
                from frisket.engine.executor.group_summary_action import (
                    GroupSummaryRefused,
                    prepare_group_summary_action,
                )

                capabilities = getattr(typed.action.definition.run, "capabilities", ())
                from frisket.actions.types import SheetRefresher

                if capabilities == (SheetRefresher,):
                    from frisket.engine.executor.sheet_refresh_action import (
                        prepare_sheet_refresh_action,
                        _RefreshRefused,
                    )
                    from frisket.engine.executor.embedding_read import TableReadRefused

                    try:
                        refresh = prepare_sheet_refresh_action(
                            get_project(),
                            project_id,
                            typed,
                            edition=self._workspace.edition,
                        )
                    except (
                        _RefreshRefused,
                        TableReadRefused,
                        GroupSummaryRefused,
                    ) as exc:
                        raise ActionPreviewRouteError(
                            400, exc.error.model_dump(mode="json"), bare_json=True
                        ) from exc
                    if refresh.prepared_group is not None:
                        estimate = _prepared_model_estimate(
                            typed,
                            refresh.prepared_group,
                            refresh.prepared_group.estimate_for(
                                self._workspace.model_servers()
                            ),
                            consent_coverage=coverage,
                        )
                    else:
                        assert refresh.prepared is not None
                        estimate = {
                            "rows": refresh.prepared.row_count,
                            "cost": 0.0,
                            "cost_source": "free_local",
                            "billed_cost": 0.0,
                            "policy_id": default_pricing_policy().policy_id,
                            "requires_confirmation": False,
                        }
                    return {
                        "schema_version": ACTION_ESTIMATE_RESULT_SCHEMA_VERSION,
                        "action": {"kind": typed.action.action_id},
                        "project_id": project_id,
                        "estimate": estimate,
                    }

                if capabilities == (IndexCreator,):
                    prepared_create = prepare_embedding_create(
                        get_project(),
                        typed,
                        project_id=project_id,
                        router=self._workspace.router_for(get_project()),
                        consent_coverage=coverage,
                        edition=self._workspace.edition,
                    )
                    if isinstance(prepared_create, ActionError):
                        raise ActionPreviewRouteError(
                            400,
                            prepared_create.model_dump(mode="json"),
                            bare_json=True,
                        )
                    return {
                        "schema_version": ACTION_ESTIMATE_RESULT_SCHEMA_VERSION,
                        "action": {"kind": typed.action.action_id},
                        "project_id": project_id,
                        "estimate": ActionEstimate.model_validate(
                            prepared_create.wire_estimate()
                        ).model_dump(mode="json"),
                    }

                if capabilities == (IndexRefresher,):
                    prepared_refresh = prepare_embedding_refresh(
                        get_project(),
                        typed,
                        project_id=project_id,
                        consent_coverage=coverage,
                        edition=self._workspace.edition,
                    )
                    if isinstance(prepared_refresh, ActionError):
                        raise ActionPreviewRouteError(
                            400,
                            prepared_refresh.model_dump(mode="json"),
                            bare_json=True,
                        )
                    return {
                        "schema_version": ACTION_ESTIMATE_RESULT_SCHEMA_VERSION,
                        "action": {"kind": typed.action.action_id},
                        "project_id": project_id,
                        "estimate": ActionEstimate.model_validate(
                            prepared_refresh.wire_estimate()
                        ).model_dump(mode="json"),
                    }

                if capabilities in ((FindScanner,), (GroupSummarizer,)):
                    typed = resolve_action_selection(get_project(), typed)
                    model_servers = self._workspace.model_servers()
                    try:
                        if capabilities == (FindScanner,):
                            prepared = prepare_find_action(
                                get_project(), typed, model_servers=model_servers
                            )
                            prepared_estimate = prepared.estimate
                        else:
                            prepared = prepare_group_summary_action(
                                get_project(), typed
                            )
                            prepared_estimate = prepared.estimate_for(model_servers)
                    except (FindRefused, GroupSummaryRefused) as exc:
                        raise ActionPreviewRouteError(
                            400, exc.error.model_dump(mode="json"), bare_json=True
                        ) from exc
                    return {
                        "schema_version": ACTION_ESTIMATE_RESULT_SCHEMA_VERSION,
                        "action": {"kind": typed.action.action_id},
                        "project_id": project_id,
                        "estimate": _prepared_model_estimate(
                            typed,
                            prepared,
                            prepared_estimate,
                            consent_coverage=coverage,
                        ),
                    }

                if getattr(typed.action.definition.run, "capabilities", ()) == (
                    ValueClusterer,
                ):
                    from frisket.engine.executor.cluster_action import (
                        prepare_cluster_action,
                    )

                    typed_plan = prepare_cluster_action(
                        get_project(),
                        typed,
                        router=self._workspace.router_for(get_project()),
                        admission=False,
                    )
                elif supports_typed_backfill_action(typed.action.definition.run):
                    backfill_plan = prepare_backfill_action(
                        get_project(), typed, edition=self._workspace.edition
                    )
                else:
                    from frisket.actions.core import MapRows, ModelRows

                    if not isinstance(
                        typed.action.definition.run, (MapRows, ModelRows, SemanticJoin)
                    ):
                        raise _action_error(
                            400, "estimate does not support this action"
                        )
                    typed_plan = build_typed_map_rows_plan(
                        get_project(),
                        typed,
                        _admit_output_targets=False,
                    )
            except BackfillRefused as exc:
                raise ActionPreviewRouteError(
                    400, exc.error.model_dump(mode="json"), bare_json=True
                ) from exc
            except ActionPreviewRouteError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, SelectionUnavailable):
                    raise ActionPreviewRouteError(
                        400,
                        ActionError(
                            code=exc.code,
                            message=str(exc),
                            action_kind=typed.action.action_id,
                            field=(
                                "scope.view_id"
                                if exc.code in {"view_not_found", "view_sheet_mismatch"}
                                else "scope"
                            ),
                        ).model_dump(mode="json"),
                        bare_json=True,
                    ) from exc
                raise _action_error(400, str(exc)) from exc

        if typed_plan is None and backfill_plan is None:
            raise _action_error(400, "estimate does not support this action")

        if backfill_plan is not None:
            assert typed is not None
            action_kind = typed.action.action_id
            spec = backfill_plan.spec()
            try:
                program = backfill_program(get_project(), spec)
            except (KeyError, TypeError, ValueError) as exc:
                raise _action_error(400, str(exc)) from exc
        else:
            assert typed_plan is not None
            action_kind = typed_plan.request.action_id
            spec = typed_plan.spec_dict()
            program = typed_plan.program
        project = get_project()
        router = self._workspace.router_for(project)
        producer = (
            bound_typed_program_request_from_runner_spec(spec)
            if backfill_plan is not None
            else typed
            if typed_plan is not None
            else None
        )
        if producer is not None and isinstance(
            producer.action.definition.run, SemanticJoin
        ):
            from frisket.engine.executor.semantic_join_action import _resolve

            try:
                _resolve(project, producer, spec, router, admission=False)
            except (KeyError, TypeError, ValueError) as exc:
                raise _action_error(400, str(exc)) from exc
        runner = MapRunner(
            project,
            router,
            consent_coverage=coverage,
            authority=UnroutedOnlyAuthority(project),
            execution_composition=self._workspace.execution_composition_for(
                project,
                router,
                self._workspace.edition_execution_composition_context_for(
                    request_context
                ),
            ),
        )
        try:
            # RATED here, with the same policy the 402 gate will use, because
            # this endpoint is the figure a user reads BEFORE deciding. A
            # panel that previews the provider cost and a modal that then
            # quotes the billed cost would be the "two surfaces, two answers"
            # defect with money in it.
            action_program = program or action_program_for_spec(spec)
            estimate = runner.estimate(spec, program=program)
            # Resolution-aware estimates are rated inside ``estimate_run``,
            # before their quote is bound into the promise-set hash.  The
            # remaining (unrouted) families still arrive here as provider
            # facts and use the same rating seat once.  Detect the producer
            # shape, rather than the program marker: a routed resolution can
            # honestly refuse and fall back to an unrated UNKNOWN estimate.
            if POLICY_ID_KEY not in estimate or BILLED_COST_KEY not in estimate:
                estimate = confirmation_estimate(
                    action_program,
                    spec,
                    estimate,
                    policy=runner.pricing_policy,
                )
                if not action_program.consumes_resolution:
                    from frisket.engine.runner.confirmation_context import estimated_usd

                    price = estimated_usd(estimate)
                    estimate["requires_confirmation"] = price is None or price > float(
                        coverage.threshold_usd
                    )
        except RowError as exc:
            raise ActionPreviewRouteError(
                400, _row_input_action_error(action_kind, exc).model_dump(mode="json")
            ) from exc
        except (KeyError, ValueError) as exc:
            raise _action_error(400, str(exc)) from exc
        return {
            "schema_version": ACTION_ESTIMATE_RESULT_SCHEMA_VERSION,
            "action": {"kind": action_kind},
            "project_id": project_id,
            "estimate": estimate,
        }
