"""V1 action executor facade.

The executor is the mutation boundary for public v1 actions. This first slice
implements the admitted v1 action slices and writes project truth plus queryable
receipts instead of returning synthetic success payloads.

This module intentionally exposes only the public action entry point and
reservation policy metadata. Callers that need dependency injection should
import ``ExecutorDeps`` from ``frisket.executor``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from frisket.contracts.action import (
    ActionError,
    ActionResult,
)
from frisket.engine.executor import action_inventory as _runtime_inventory
from frisket.engine.executor import action_lifecycle as _runtime_lifecycle
from frisket.engine.executor import action_support as _runtime_support
from frisket.engine.store import Project
from frisket.engine.runner.preview import PREVIEW_MAX_ROWS
from frisket.engine.runner.validation import (
    InvalidTargetRows,
    InvalidTargetSheet,
    target_rows,
)
from frisket.ops.base import Recipe
from frisket.execution.provider import (
    ExecutionComposition,
    ExecutionCompositionContext,
    open_execution_composition,
)

__all__ = ["run_action_spec"]


def _default_map_runner_factory(
    project: Project,
    router: Any | None,
    *,
    execution_composition: ExecutionComposition | None = None,
) -> Any:
    from frisket.engine.jobs.runs import build_attempt_authority, project_scoped_router
    from frisket.engine.runner import MapRunner

    effective_router = project_scoped_router(project, router)
    composition = execution_composition or open_execution_composition(
        project, effective_router, ExecutionCompositionContext.direct()
    )
    return MapRunner(
        project,
        effective_router,
        concurrency=1,
        allow_action_lifecycle_only_recipes=True,
        # The same authority the queued project.run handler
        # builds (build_attempt_authority is the ONE construction) — a direct
        # action (e.g. run.backfill resuming a routed transcription run) mints
        # its attempt, and therefore verifies consent/coverage and its promise
        # set, exactly like a queued fresh claim.
        authority=build_attempt_authority(
            project,
            composition=composition,
        ),
        execution_composition=composition,
    )


def _composed_map_runner_factory(
    execution_composition: ExecutionComposition | None = None,
) -> Callable[[Project, Any | None], Any]:
    def factory(project: Project, router: Any | None) -> Any:
        from frisket.engine.jobs.runs import (
            build_attempt_authority,
            project_scoped_router,
        )
        from frisket.engine.runner import MapRunner

        effective_router = project_scoped_router(project, router)
        composition = execution_composition or open_execution_composition(
            project, effective_router, ExecutionCompositionContext.direct()
        )
        return MapRunner(
            project,
            effective_router,
            concurrency=1,
            allow_action_lifecycle_only_recipes=True,
            authority=build_attempt_authority(
                project,
                composition=composition,
            ),
            execution_composition=composition,
        )

    return factory


def _composition_bound_map_runner_factory(
    factory: Callable[[Project, Any | None], Any],
    execution_composition: ExecutionComposition,
) -> Callable[[Project, Any | None], Any]:
    """Make a custom runner factory consume, rather than drop, the carrier."""

    def bound(project: Project, router: Any | None) -> Any:
        runner = factory(project, router)
        return _bind_runner_execution_composition(runner, execution_composition)

    return bound


def _bind_runner_execution_composition(
    runner: Any,
    execution_composition: ExecutionComposition,
) -> Any:
    """Bind both resolution and resume authority to one request provider."""

    if not hasattr(runner, "execution_composition"):
        raise RuntimeError(
            "a map_runner_factory used with execution_composition must "
            "return a runner that consumes execution_composition"
        )
    runner.execution_composition = execution_composition

    # Custom factories are an intended deployment injection seat.  Binding
    # only the runner would let fresh resolution use provider F while a
    # resumed attempt silently re-dereferenced through the factory's old
    # authority provider S.  Keep non-routed preview authorities untouched.
    from frisket.execution.attempt_authority import AttemptAuthority

    if isinstance(getattr(runner, "authority", None), AttemptAuthority):
        runner.authority = replace(
            runner.authority,
            composition=execution_composition,
        )
    return runner


def _bind_runner_consent_coverage(runner: Any, consent_coverage: Any) -> Any:
    """Keep one request's consent authority on both runner admission seams."""

    from frisket.execution.attempt_authority import AttemptAuthority

    runner.consent_coverage = consent_coverage
    if isinstance(getattr(runner, "authority", None), AttemptAuthority):
        runner.authority = replace(
            runner.authority,
            consent_coverage=consent_coverage,
        )
    return runner


@dataclass
class MapPreviewPlan:
    """A resolved, guard-checked map preview ready to execute in memory. The
    preview service builds the runner via ``make_runner`` and calls
    ``runner.preview(runner_spec, ...)`` on a job thread; ``runner_spec`` has
    already passed the per-action resolve+precheck (see
    ``resolve_map_preview``)."""

    action_kind: str
    runner_spec: dict[str, Any]
    make_runner: Callable[[], Any]
    semantic_scope_total: int | None = None
    program: Recipe | None = None


def build_map_preview_plan(
    project: Project,
    *,
    action_kind: str,
    runner_spec: dict[str, Any],
    router: Any | None,
    deps: _runtime_inventory.ExecutorDeps | None,
    semantic_scope_total: int | None = None,
    program: Recipe | None = None,
) -> MapPreviewPlan:
    """Bind canonical runner-factory and hosted composition preview seams."""

    factory = (
        deps.map_runner_factory
        if deps is not None and deps.map_runner_factory is not None
        else _runtime_lifecycle.preview_map_runner_factory
    )
    effective_router = router if router is not None else (deps.router if deps else None)

    def make_runner() -> Any:
        runner = factory(project, effective_router)
        if deps is not None and deps.url_capture_browser is not None:
            runner.op_context_extras = {
                **runner.op_context_extras,
                "url_capture_browser": deps.url_capture_browser,
            }
        if deps is not None and deps.execution_composition is not None:
            runner = _bind_runner_execution_composition(
                runner,
                deps.execution_composition,
            )
        if deps is not None and deps.consent_coverage is not None:
            runner = _bind_runner_consent_coverage(runner, deps.consent_coverage)
        return runner

    return MapPreviewPlan(
        action_kind=action_kind,
        runner_spec=runner_spec,
        make_runner=make_runner,
        semantic_scope_total=semantic_scope_total,
        program=program,
    )


def _resolve_bounded_preview_scope(
    project: Project,
    runner_spec: dict[str, Any],
    *,
    action_kind: str,
    sheet_field: str,
    rows_field: str,
) -> "tuple[dict[str, Any], int] | ActionError":
    """Resolve one semantic row scope into the bounded concrete preview sample."""

    try:
        scoped_row_ids = target_rows(project, runner_spec)
    except InvalidTargetSheet as exc:
        return ActionError(
            code="invalid_input_ref",
            message=str(exc),
            action_kind=action_kind,
            field=sheet_field,
            details={"sheet_id": exc.sheet_id},
        )
    except InvalidTargetRows as exc:
        return ActionError(
            code="invalid_input_ref",
            message=f"{action_kind} row_ids must belong to the target sheet",
            action_kind=action_kind,
            field=rows_field,
            details={"missing": exc.missing},
        )
    return (
        {**runner_spec, "row_ids": scoped_row_ids[:PREVIEW_MAX_ROWS]},
        len(scoped_row_ids),
    )


def resolve_map_preview(
    project: Project,
    data: dict[str, Any],
    *,
    router: Any | None = None,
    deps: _runtime_inventory.ExecutorDeps | None = None,
) -> "MapPreviewPlan | ActionError":
    """Validate a v1 action envelope and run the SAME resolve+precheck the run
    path uses, returning a ``MapPreviewPlan`` or
    the first ``ActionError``. Only reserved-maprunner (map.*) actions support
    preview; anything else is a typed ``unsupported_action_kind`` error. Writes
    nothing — the returned plan is executed in memory by ``MapRunner.preview``.
    """
    if not isinstance(data, dict):
        return ActionError(
            code="invalid_action_spec",
            message="ActionSpec must be a JSON object",
        )

    if isinstance(data.get("action_id"), str):
        from frisket.actions.core import (
            ColumnTransform,
            CreateSheet,
            GoogleSheetsExport,
            SemanticJoin,
            _ProjectAction,
        )
        from frisket.actions.system import typed_action_for_request
        from frisket.engine.executor.column_transform_action import (
            build_column_transform_preview_plan,
        )
        from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
        from frisket.actions.types import ActionRequest
        from frisket.authoring.workbench.installed_actions import bind_installed_action

        try:
            typed = bind_installed_action(project, ActionRequest.model_validate(data))
            if typed is None:
                typed = typed_action_for_request(data)
            if isinstance(
                typed.action.definition.run,
                (CreateSheet, _ProjectAction, GoogleSheetsExport),
            ):
                return ActionError(
                    code="unsupported_action_kind",
                    message="project actions do not support row preview",
                    action_kind=typed.action.action_id,
                )
            if isinstance(typed.action.definition.run, ColumnTransform):
                return build_column_transform_preview_plan(project, typed)
            typed_plan = build_typed_map_rows_plan(
                project, typed, _admit_output_targets=False
            )
            preview_spec = typed_plan.spec_dict()
            if isinstance(typed.action.definition.run, SemanticJoin):
                from frisket.engine.executor.semantic_join_action import _resolve

                _resolve(
                    project,
                    typed,
                    preview_spec,
                    router if router is not None else (deps.router if deps else None),
                    admission=False,
                )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            return ActionError(
                code=getattr(exc, "code", "invalid_action_request"),
                message=str(exc),
                action_kind=str(data["action_id"]),
            )
        preview_scope = _resolve_bounded_preview_scope(
            project,
            preview_spec,
            action_kind=typed.action.action_id,
            sheet_field="scope.sheet_id",
            rows_field="scope.row_ids",
        )
        if isinstance(preview_scope, ActionError):
            return preview_scope
        runner_spec, semantic_scope_total = preview_scope
        return build_map_preview_plan(
            project,
            action_kind=typed.action.action_id,
            runner_spec=runner_spec,
            router=router,
            deps=deps,
            semantic_scope_total=semantic_scope_total,
            program=typed_plan.program,
        )
    return ActionError(
        code="unsupported_action_kind",
        message="Preview is only available for map actions.",
        field="action_id",
    )


def _executor_deps_with_defaults(
    *,
    deps: _runtime_inventory.ExecutorDeps | None,
    router: Any | None,
    rss_fetcher: Any | None,
    enclosure_fetcher: Any | None,
    url_capture_fetcher: Any | None,
    url_capture_browser: Any | None,
) -> _runtime_inventory.ExecutorDeps:
    base = deps or _runtime_inventory.ExecutorDeps()
    execution_composition = base.execution_composition
    map_runner_factory = base.map_runner_factory
    if map_runner_factory is None:
        map_runner_factory = (
            _composed_map_runner_factory(
                execution_composition,
            )
            if execution_composition is not None
            else _default_map_runner_factory
        )
    elif execution_composition is not None:
        map_runner_factory = _composition_bound_map_runner_factory(
            map_runner_factory,
            execution_composition,
        )
    browser_renderer = url_capture_browser or base.url_capture_browser
    if base.consent_coverage is not None:
        consent_factory = map_runner_factory

        def map_runner_factory(project, router):
            runner = consent_factory(project, router)
            return _bind_runner_consent_coverage(runner, base.consent_coverage)

    if browser_renderer is not None:
        base_factory = map_runner_factory

        def map_runner_factory(project, router):
            runner = base_factory(project, router)
            runner.op_context_extras = {
                **runner.op_context_extras,
                "url_capture_browser": browser_renderer,
            }
            return runner

    return _runtime_inventory.ExecutorDeps(
        router=router if router is not None else base.router,
        execution_composition=execution_composition,
        rss_fetcher=rss_fetcher if rss_fetcher is not None else base.rss_fetcher,
        enclosure_fetcher=(
            enclosure_fetcher
            if enclosure_fetcher is not None
            else base.enclosure_fetcher
        ),
        url_capture_fetcher=(
            url_capture_fetcher
            if url_capture_fetcher is not None
            else base.url_capture_fetcher
        ),
        url_capture_browser=(
            url_capture_browser
            if url_capture_browser is not None
            else base.url_capture_browser
        ),
        map_runner_factory=map_runner_factory,
        reserved_maprunner_write_overrides=base.reserved_maprunner_write_overrides,
        connected_account_resolver=base.connected_account_resolver,
        google_sheets_client=base.google_sheets_client,
        embedding_gateway=base.embedding_gateway,
        email_sources=base.email_sources,
        sheet_export_limits=base.sheet_export_limits,
        url_import_limits=base.url_import_limits,
        import_workload_limits=base.import_workload_limits,
        cell_edit_query_limits=base.cell_edit_query_limits,
        cancelled=base.cancelled,
    )


def run_action_spec(
    project: Project,
    data: dict[str, Any],
    *,
    project_id: str,
    router: Any | None = None,
    rss_fetcher: Any | None = None,
    enclosure_fetcher: Any | None = None,
    url_capture_fetcher: Any | None = None,
    url_capture_browser: Any | None = None,
    deps: _runtime_inventory.ExecutorDeps | None = None,
    edition_run_context: Mapping[str, Any] | None = None,
) -> ActionResult:
    if not isinstance(data, dict):
        return _runtime_support._failed_result(
            project_id=project_id,
            action_kind="unknown",
            error=ActionError(
                code="invalid_action_request",
                message="ActionRequest must be a JSON object",
            ),
        )

    from frisket.actions.core import (
        ColumnTransform,
        CreateSheet,
        GoogleSheetsExport,
        SemanticJoin,
        _ProjectAction,
    )
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.column_transform_action import (
        run_typed_column_transform_action,
    )
    from frisket.engine.executor.action_families.exports import (
        run_typed_export_action,
        supports_typed_export_action,
    )
    from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
    from frisket.engine.executor.source_action import (
        run_typed_source_action,
        supports_typed_source_action,
    )
    from frisket.engine.executor.source_poll_action import (
        run_typed_source_poll_action,
        supports_typed_source_poll_action,
    )

    from frisket.actions.types import ActionRequest
    from frisket.authoring.workbench.installed_actions import bind_installed_action

    try:
        typed = bind_installed_action(project, ActionRequest.model_validate(data))
        if typed is None:
            typed = typed_action_for_request(data)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        return _runtime_support._failed_result(
            project_id=project_id,
            action_kind=str(data.get("action_id", "unknown")),
            error=ActionError(
                code="invalid_action_request",
                message=str(exc),
                action_kind=str(data.get("action_id", "unknown")),
            ),
        )
    if isinstance(typed.action.definition.run, GoogleSheetsExport):
        from frisket.engine.executor.google_sheets_action import (
            run_typed_google_sheets_export,
        )

        return run_typed_google_sheets_export(project, project_id, typed, deps=deps)
    if isinstance(typed.action.definition.run, ColumnTransform):
        return run_typed_column_transform_action(project, project_id, typed)
    if isinstance(typed.action.definition.run, CreateSheet):
        from frisket.engine.executor.table_action import (
            run_typed_create_sheet_action,
        )

        return run_typed_create_sheet_action(project, project_id, typed, deps=deps)
    if isinstance(typed.action.definition.run, _ProjectAction):
        from frisket.engine.executor.action_families.embeddings import (
            run_typed_embedding_action,
            supports_typed_embedding_action,
        )
        from frisket.engine.executor.mutation_action import (
            run_typed_mutation_action,
            supports_typed_mutation_action,
        )
        from frisket.engine.executor.operation_action import (
            run_typed_operation_action,
            supports_typed_operation_action,
        )

        from frisket.engine.executor.plugin_load_action import (
            run_typed_plugin_load_action,
            supports_typed_plugin_load_action,
        )

        from frisket.engine.executor.sheet_refresh_action import (
            run_typed_sheet_refresh_action,
            supports_typed_sheet_refresh_action,
        )

        terminal = typed.action.definition.run
        if terminal.callable_host:
            from frisket.engine.executor.callable_action import (
                run_typed_callable_action,
            )

            return run_typed_callable_action(
                project,
                project_id,
                typed,
                deps=deps,
                edition_run_context=edition_run_context,
            )
        from frisket.engine.executor.enclosure_action import (
            run_typed_enclosure_action,
            supports_typed_enclosure_action,
        )
        from frisket.engine.executor.run_backfill_action import (
            run_typed_backfill_action,
            supports_typed_backfill_action,
        )
        from frisket.engine.executor.temporal_extract_action import (
            run_typed_temporal_extract_action,
            supports_typed_temporal_extract_action,
        )
        from frisket.actions.page_capture_types import PageCapturer
        from frisket.actions.cluster_types import ValueClusterer
        from frisket.actions.group_summary_types import GroupSummarizer
        from frisket.actions.find_types import FindScanner
        from frisket.engine.executor.cluster_action import run_typed_cluster_action
        from frisket.engine.executor.page_capture_action import (
            run_typed_page_capture_action,
        )

        owners = [
            name
            for name, supports in (
                ("mutation", supports_typed_mutation_action),
                ("operation", supports_typed_operation_action),
                ("source", supports_typed_source_action),
                ("source_poll", supports_typed_source_poll_action),
                ("export", supports_typed_export_action),
                ("embedding", supports_typed_embedding_action),
                ("plugin", supports_typed_plugin_load_action),
                ("sheet_refresh", supports_typed_sheet_refresh_action),
                ("backfill", supports_typed_backfill_action),
                ("enclosure", supports_typed_enclosure_action),
                ("temporal_extract", supports_typed_temporal_extract_action),
                (
                    "page_capture",
                    lambda terminal: terminal.capabilities == (PageCapturer,),
                ),
                (
                    "value_cluster",
                    lambda terminal: terminal.capabilities == (ValueClusterer,),
                ),
                (
                    "group_summary",
                    lambda terminal: terminal.capabilities == (GroupSummarizer,),
                ),
                ("find", lambda terminal: terminal.capabilities == (FindScanner,)),
            )
            if supports(terminal)
        ]
        if len(owners) != 1:
            matches = ", ".join(owners) or "none"
            raise TypeError(
                f"{terminal.single_capability().__name__} typed project action requires "
                f"exactly one host owner; matched {matches}"
            )
        if owners[0] == "mutation":
            return run_typed_mutation_action(project, project_id, typed, deps=deps)
        if owners[0] == "group_summary":
            from frisket.engine.executor.group_summary_action import (
                run_typed_group_summary_action,
            )

            return run_typed_group_summary_action(
                project, project_id, typed, router=router, deps=deps
            )
        if owners[0] == "find":
            from frisket.engine.executor.find_action import run_typed_find_action

            return run_typed_find_action(project, project_id, typed)
        if owners[0] == "enclosure":
            return run_typed_enclosure_action(
                project,
                project_id,
                typed,
                enclosure_fetcher=enclosure_fetcher
                if enclosure_fetcher is not None
                else (deps.enclosure_fetcher if deps is not None else None),
            )
        if owners[0] == "operation":
            return run_typed_operation_action(project, project_id, typed)
        if owners[0] == "source":
            return run_typed_source_action(project, project_id, typed)
        if owners[0] == "source_poll":
            return run_typed_source_poll_action(
                project,
                project_id,
                typed,
                rss_fetcher=rss_fetcher
                if rss_fetcher is not None
                else (deps.rss_fetcher if deps is not None else None),
            )
        if owners[0] == "temporal_extract":
            return run_typed_temporal_extract_action(project, project_id, typed)
        if owners[0] == "page_capture":
            return run_typed_page_capture_action(
                project,
                project_id,
                typed,
                url_capture_fetcher=url_capture_fetcher
                if url_capture_fetcher is not None
                else (deps.url_capture_fetcher if deps is not None else None),
                url_capture_browser=url_capture_browser
                if url_capture_browser is not None
                else (deps.url_capture_browser if deps is not None else None),
            )
        if owners[0] == "value_cluster":
            executor_deps = _executor_deps_with_defaults(
                deps=deps,
                router=router,
                rss_fetcher=rss_fetcher,
                enclosure_fetcher=enclosure_fetcher,
                url_capture_fetcher=url_capture_fetcher,
                url_capture_browser=url_capture_browser,
            )
            return run_typed_cluster_action(
                project,
                project_id,
                typed,
                router=executor_deps.router,
                map_runner_factory=executor_deps.map_runner_factory,
                edition_run_context=edition_run_context,
            )
        if owners[0] == "sheet_refresh":
            return run_typed_sheet_refresh_action(project, project_id, typed)
        if owners[0] == "backfill":
            executor_deps = _executor_deps_with_defaults(
                deps=deps,
                router=router,
                rss_fetcher=rss_fetcher,
                enclosure_fetcher=enclosure_fetcher,
                url_capture_fetcher=url_capture_fetcher,
                url_capture_browser=url_capture_browser,
            )
            return run_typed_backfill_action(
                project,
                project_id,
                typed,
                executor_deps.router,
                executor_deps.map_runner_factory,
            )
        if owners[0] == "plugin":
            return run_typed_plugin_load_action(project, project_id, typed)
        if owners[0] == "embedding":
            return run_typed_embedding_action(
                project,
                project_id,
                typed,
                router=router
                if router is not None
                else (deps.router if deps else None),
                embedding_gateway=deps.embedding_gateway if deps else None,
            )
        return run_typed_export_action(project, project_id, typed, deps=deps)
    executor_deps = _executor_deps_with_defaults(
        deps=deps,
        router=router,
        rss_fetcher=rss_fetcher,
        enclosure_fetcher=enclosure_fetcher,
        url_capture_fetcher=url_capture_fetcher,
        url_capture_browser=url_capture_browser,
    )
    if isinstance(typed.action.definition.run, SemanticJoin):
        from frisket.engine.executor.semantic_join_action import (
            run_typed_semantic_join_action,
        )

        return run_typed_semantic_join_action(
            project,
            project_id,
            typed,
            executor_deps.router,
            executor_deps.map_runner_factory,
        )
    return run_typed_map_rows_action(
        project,
        project_id,
        typed,
        executor_deps.router,
        executor_deps.map_runner_factory,
    )
