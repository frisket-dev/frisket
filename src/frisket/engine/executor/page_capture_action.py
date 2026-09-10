"""Actual-argument preparation for the page-capture callable."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from frisket.actions.file_types import UrlColumn
from frisket.actions.page_capture_types import CaptureOptions, PreparedPageCapture
from frisket.actions.types import SheetRows
from frisket.contracts.action import ActionError
from frisket.engine.executor.page_capture import (
    CapturePlan,
    _resolve_web_capture_page_inputs,
    execute_prepared_capture,
    _web_capture_page_result_from_existing_receipt,
)


class CaptureRefused(ValueError):
    def __init__(self, error: ActionError):
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True)
class PreparedCapturePlan:
    capture: CapturePlan
    resolved: dict[str, Any]
    output_fields: tuple[dict[str, str], ...]
    output_names: dict[str, str]
    required_capabilities: tuple[str, ...]

    @property
    def creates_sheet(self):
        return self.capture.output_mode == "links"


class _PageCapturer:
    def __init__(self, project, bound, *, admission):
        self.project = project
        self.bound = bound
        self.admission = admission
        self.plans: dict[PreparedPageCapture, PreparedCapturePlan] = {}

    def prepare(self, source: UrlColumn, *, options: CaptureOptions):
        # These are the actual capability arguments, including custom callables
        # whose parameter names or option derivation differ from the builtin.
        source = UrlColumn.model_validate(source)
        if not isinstance(options, CaptureOptions):
            raise TypeError("capture options must be a CaptureOptions value")
        options = CaptureOptions.model_validate(
            {name: getattr(options, name) for name in CaptureOptions.model_fields}
        )
        scope = self.bound.request.scope
        if not isinstance(scope, SheetRows):
            raise ValueError("page capture requires a sheet_rows scope")
        links = options.output_mode == "links"
        sheet_name = self.bound.request.sheet_name
        if self.admission and bool(sheet_name) != links:
            raise ValueError("sheet_name is required only for links output")
        fields = (
            (("url", "link"), ("anchor_text", "text"), ("source_url", "link"))
            if links
            else (("page", "file"),)
        )
        renames = self.bound.request.output_names
        if set(renames) - {key for key, _ in fields}:
            raise ValueError("output_names contains an inactive capture output")
        names = {key: renames.get(key, key) for key, _ in fields}
        if len(set(names.values())) != len(names):
            raise ValueError("capture output names must be distinct")
        plan = CapturePlan(
            sheet_id=scope.sheet_id,
            input_column=source.name,
            row_ids=None if scope.row_ids is None else list(scope.row_ids),
            output_name=names.get("page", "page"),
            links_sheet_name=sheet_name or "",
            full_page=True,
            output_column_type="file",
            primary_role="html",
            output_names=names,
            request=self.bound.request.model_dump(mode="json"),
            **options.model_dump(),
        )
        resolved = _resolve_web_capture_page_inputs(
            self.project, self.bound.action.action_id, plan, admission=self.admission
        )
        if isinstance(resolved, ActionError):
            raise CaptureRefused(resolved)
        prepared = PreparedCapturePlan(
            plan,
            resolved,
            tuple(
                {
                    "key": key,
                    "column_type": typ,
                    **(
                        {"existing_column_policy": "compatible"}
                        if key == "page"
                        else {}
                    ),
                }
                for key, typ in fields
            ),
            names,
            ("project:write", "external:url_capture")
            + (
                ("external:browser_render",)
                if options.render_mode == "playwright"
                else ()
            ),
        )
        handle = PreparedPageCapture(selected_row_count=len(resolved["row_ids"]))
        self.plans[handle] = prepared
        return handle


def prepare_page_capture_action(
    project, bound, *, admission=True
) -> PreparedCapturePlan:
    capability = _PageCapturer(project, bound, admission=admission)
    returned = bound.action.definition.run.handler(bound.params, capability)
    if (
        not isinstance(returned, PreparedPageCapture)
        or returned not in capability.plans
    ):
        raise ValueError("capture must return a preparation issued by this invocation")
    return capability.plans[returned]


def run_typed_page_capture_action(
    project,
    project_id,
    bound,
    *,
    url_capture_fetcher=None,
    url_capture_browser=None,
):
    from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
    from frisket.engine.executor.action_reservations import _receipt_for_idempotency
    from frisket.engine.executor.action_support import _failed_result
    from frisket.engine.executor.map_rows_action import typed_request_hash

    action = _TypedProjectEnvelope(
        kind=bound.action.action_id,
        idempotency_key=bound.request.idempotency_key,
        params=dict(bound.request.params),
    )
    params_hash = typed_request_hash(bound)
    existing = _receipt_for_idempotency(project, action.idempotency_key)
    if existing is not None:
        return _web_capture_page_result_from_existing_receipt(
            project,
            existing,
            params_hash=params_hash,
            project_id=project_id,
            action=action,
        )
    try:
        if project.effective_network_policy() == "off":
            raise CaptureRefused(
                ActionError(
                    code="network_disabled",
                    message="Project network access is disabled",
                    action_kind=action.kind,
                )
            )
        plan = prepare_page_capture_action(project, bound)
    except (TypeError, ValueError) as exc:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=exc.error
            if isinstance(exc, CaptureRefused)
            else ActionError(
                code="invalid_params", message=str(exc), action_kind=action.kind
            ),
        )
    return execute_prepared_capture(
        project,
        action,
        plan.capture,
        project_id=project_id,
        params_hash=params_hash,
        resolved=plan.resolved,
        url_capture_fetcher=url_capture_fetcher,
        url_capture_browser=url_capture_browser,
    )
