"""Narrow async orchestration for research-owned Project Ask actions.

Preparation and dispatch stay with the canonical Project Ask and ActionRun
services.  This adapter only joins their synchronous launch boundary to the
research run's asynchronous approval, budget, child-wait, and checkpoint
lifecycle.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol, TypedDict

from frisket.authoring.project_ask_actions import OutputGrantAllows
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.server.services.action_runs import ActionRunResponse
from frisket.server.services.project_qa_actions import (
    CatalogPayloadProvider,
    PreparedActionReference,
    PreparedProjectAskAction,
    ProjectAskActionUnpriced,
    ProjectAskActionService,
    OutputGrantsProvider,
    QuoteProvider,
    save_prepared_project_ask_action,
)
from frisket.server.services.project_qa_child_runs import (
    ChildRunDispatch,
    ChildRunDrainTimeout,
    ChildRunOutcome,
    RateActualCost,
)
from frisket.server.thread_worker import await_thread_worker


ActionRunner = Callable[[str, dict[str, Any]], Any]
WaitChild = Callable[..., Awaitable[ChildRunOutcome]]

_FORBIDDEN_ACTION_IDS = frozenset({"map.mcp_extract", "map.python", "plugin.load"})
_FORBIDDEN_CAPABILITY_PREFIXES = ("admin:", "unsafe:")
_FORBIDDEN_CAPABILITIES = frozenset({"plugin:load"})
_FORBIDDEN_EFFECT_MARKERS = ("execute_code", "execute_trusted_local_python")
_TYPED_MAP_CATALOG_EFFECTS = frozenset(
    {
        "read_input_rows",
        "read_pdf_blob_cells",
        "call_natural_pdf_adapter",
        "read_media_blobs",
        "call_local_metadata_adapters",
        "write_media_metadata_cache",
        "call_model_router",
        "call_external_provider",
        "create_generated_columns",
        "write_run_results",
        "write_map_op",
        "write_model_calls",
        "write_trace",
        "write_receipt",
    }
)


class ResearchSessionLike(Protocol):
    """The small ResearchSession surface used by this bridge."""

    store: Any
    id: str

    async def check_authority(self) -> None: ...

    async def pause(self, approval: dict[str, Any]) -> str: ...

    async def admit(
        self,
        operation_id: str,
        payload: str,
        kind: str,
        estimate: int | None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class ProjectQAExecutionRefused(ValueError):
    """The action category is never available to autonomous research."""


class ResearchActionSkipped(RuntimeError):
    """The user declined a proposed child action or its budget pause."""


class PreparedResearchAction(TypedDict):
    proposal: dict[str, Any]
    event_ref: dict[str, Any]
    automatic_eligible: bool
    preparation_reason: str | None


class ResearchActionResult(TypedDict):
    status: str
    receipt_id: str | None
    outputs: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    warnings: list[str]


class _ActionApprovalRequired(Exception):
    def __init__(self, approval: dict[str, Any]) -> None:
        self.approval = approval
        super().__init__("action approval required")


@dataclass(frozen=True)
class _Admission:
    operation_id: str
    payload_identity: str
    operation_kind: str
    estimate_micros: int | None
    metadata: dict[str, Any]


class _AdmissionBlocked(Exception):
    def __init__(self, admission: _Admission, cause: BaseException) -> None:
        self.admission = admission
        self.cause = cause
        super().__init__(str(cause))


async def _await_launch_result(awaitable: Awaitable[Any]) -> tuple[Any, bool]:
    """Keep a canonical launch result available when its caller is cancelled."""

    task = asyncio.create_task(awaitable)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.done() and task.cancelled():
                raise
            cancelled = True
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise


def _strings(value: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProjectQAExecutionRefused(f"prepared action has unknown {name}")
    return tuple(value)


def _request_action_id(authority: Mapping[str, Any]) -> str:
    request = authority.get("request")
    action_id = request.get("action_id") if isinstance(request, Mapping) else None
    if not isinstance(action_id, str) or not action_id:
        raise ProjectQAExecutionRefused("prepared action has no action identity")
    return action_id


def _promise_set_hash(authority: Mapping[str, Any]) -> str | None:
    quote = authority.get("quote")
    estimate = quote.get("estimate") if isinstance(quote, Mapping) else None
    if not isinstance(estimate, Mapping):
        raise ProjectQAExecutionRefused("prepared action has no current quote")
    if not estimate.get("requires_confirmation"):
        return None
    promise = estimate.get("promise_set_hash")
    if not isinstance(promise, str) or not promise:
        raise ProjectQAExecutionRefused("prepared action quote has no promise")
    return promise


def _forbidden_reason(
    action_id: str, capabilities: tuple[str, ...], effects: tuple[str, ...]
) -> str | None:
    if action_id in _FORBIDDEN_ACTION_IDS:
        return "unsafe, administrative, or code actions are unavailable to research"
    for capability in capabilities:
        lowered = capability.casefold()
        if capability in _FORBIDDEN_CAPABILITIES or lowered.startswith(
            _FORBIDDEN_CAPABILITY_PREFIXES
        ):
            return "unsafe, administrative, or code capabilities are unavailable"
        if "local_code" in lowered or lowered.endswith(":code"):
            return "unsafe, administrative, or code capabilities are unavailable"
    if any(
        marker in effect.casefold()
        for effect in effects
        for marker in _FORBIDDEN_EFFECT_MARKERS
    ):
        return "unsafe, administrative, or code effects are unavailable"
    return None


def _approval_reason(
    mode: str,
    authority: Mapping[str, Any],
    capabilities: tuple[str, ...],
    effects: tuple[str, ...],
) -> str | None:
    if mode not in {"ask_each", "ask_overwrite", "full_access"}:
        raise ProjectQAExecutionRefused(f"unsupported research write mode: {mode}")
    if mode == "ask_each":
        return "ask_each"
    if any(capability.startswith("external:") for capability in capabilities) or any(
        effect.startswith(("fetch_external_", "write_google_")) for effect in effects
    ):
        return "external_effect"
    output_effects = authority.get("effects")
    if not isinstance(output_effects, Mapping):
        return "unknown_effect"
    request = authority.get("request")
    request_replace = (
        request.get("replace_existing") if isinstance(request, Mapping) else None
    )
    prepared_replace = output_effects.get("replace_existing")
    if (
        output_effects.get("kind") != "typed_map_rows"
        or not set(effects).issubset(_TYPED_MAP_CATALOG_EFFECTS)
        or not isinstance(request_replace, bool)
        or not isinstance(prepared_replace, bool)
        or request_replace != prepared_replace
    ):
        return "unknown_effect"
    if mode == "ask_overwrite" and prepared_replace:
        return "overwrite_or_delete"
    return None


class ProjectQAExecutionService:
    """Prepare and execute one research turn's canonical child actions.

    The caller serializes these methods with the runner's existing tool lock.
    This class deliberately creates no scheduler or independent execution path.
    """

    def __init__(
        self,
        project: Project,
        project_id: str,
        turn: Mapping[str, Any],
        store: ProjectQAStore,
        research_session: ResearchSessionLike,
        *,
        catalog_payload_provider: CatalogPayloadProvider,
        quote_provider: QuoteProvider,
        run_action: ActionRunner,
        wait_child: WaitChild,
        output_grants_provider: OutputGrantsProvider | None = None,
        output_grant_allows: OutputGrantAllows | None = None,
        rate_actual_cost: RateActualCost | None = None,
    ) -> None:
        self.project = project
        self.project_id = project_id
        self.turn = dict(turn)
        self.store = store
        self.research = research_session
        self._catalog_payload_provider = catalog_payload_provider
        self._quote_provider = quote_provider
        self._run_action = run_action
        self._wait_child = wait_child
        self._output_grants_provider = output_grants_provider
        self._output_grant_allows = output_grant_allows
        self._rate_actual_cost = rate_actual_cost
        self._approved_payloads: set[str] = set()
        self._launch_identities: dict[str, str] = {}
        self._recorded_dispatches: set[tuple[str, ChildRunDispatch]] = set()

    async def prepare_action(
        self, title: str, draft: Mapping[str, Any]
    ) -> PreparedResearchAction:
        """Persist one reviewable, canonical proposal after current authority."""

        await self.research.check_authority()

        def prepare() -> PreparedProjectAskAction:
            resolved = (
                self._output_grants_provider()
                if self._output_grants_provider is not None
                else ()
            )
            output_grants = getattr(resolved, "grants", resolved)
            if isinstance(output_grants, (str, bytes)):
                raise ValueError("output grants are malformed")
            try:
                output_grants = tuple(output_grants)
            except TypeError as error:
                raise ValueError("output grants are malformed") from error
            return save_prepared_project_ask_action(
                self.project,
                self.turn,
                self.store,
                title=title,
                draft=draft,
                catalog_payload=self._catalog_payload_provider(),
                quote_provider=self._quote_provider,
                output_grants=output_grants,
                output_grant_allows=self._output_grant_allows,
            )

        saved = await await_thread_worker(prepare)
        authority = saved.event["payload"]["prepared_action"]
        return {
            "proposal": saved.proposal,
            "event_ref": {
                "event_seq": saved.reference.event_seq,
                "dispatch_id": saved.reference.dispatch_id,
            },
            "automatic_eligible": bool(authority["automatic_eligible"]),
            "preparation_reason": authority.get("preparation_reason"),
        }

    def _authorize_for_policy(self, authority: Mapping[str, Any]) -> dict[str, Any]:
        """Apply the current write mode to one adapter-revalidated payload."""

        identity = authority.get("payload_identity")
        if not isinstance(identity, str) or not identity:
            raise ProjectQAExecutionRefused("prepared action has no payload identity")
        action_id = _request_action_id(authority)
        capabilities = _strings(
            authority.get("required_capabilities"), name="capabilities"
        )
        effects = _strings(authority.get("catalog_effects"), name="effects")
        forbidden = _forbidden_reason(action_id, capabilities, effects)
        if forbidden is not None:
            raise ProjectQAExecutionRefused(forbidden)
        research = self.research.store.get(self.research.id)
        if research.get("turn_id") != self.turn.get("id"):
            raise ProjectQAExecutionRefused(
                "research session is not bound to the current turn"
            )
        mode = research.get("write_mode")
        if not isinstance(mode, str):
            raise ProjectQAExecutionRefused("research write mode is unavailable")
        reason = _approval_reason(mode, authority, capabilities, effects)
        if reason is not None and identity not in self._approved_payloads:
            request = authority["request"]
            assert isinstance(request, Mapping)
            raise _ActionApprovalRequired(
                {
                    "kind": "action",
                    "operation_id": str(authority.get("dispatch_id") or ""),
                    "payload_identity": identity,
                    "action_id": action_id,
                    "scope": dict(authority.get("scope") or {}),
                    "effects": dict(authority.get("effects") or {}),
                    "catalog_effects": list(effects),
                    "estimate": dict(authority["quote"]["estimate"]),
                    "reason": reason,
                }
            )
        self._launch_identities[str(authority.get("dispatch_id") or "")] = identity
        approval: dict[str, Any] = {"payload_identity": identity}
        promise = _promise_set_hash(authority)
        if promise is not None:
            approval["promise_set_hash"] = promise
        return approval

    async def execute_action(
        self, event_ref: Mapping[str, Any]
    ) -> ResearchActionResult:
        """Admit, launch, wait, settle, and grant one exact prepared action."""

        reference = self._reference(event_ref)
        response: ActionRunResponse
        launch_cancelled = False
        while True:
            await self.research.check_authority()
            action_service = ProjectAskActionService(
                catalog_payload_provider=self._catalog_payload_provider,
                quote_provider=self._quote_provider,
                research_for_turn=lambda turn_id: self.research.store.get(
                    self.research.id
                ),
                authorize_dispatch=self._authorize_for_policy,
                admit_operation=self._admit_sync,
                run_action=self._run_action,
                output_grants_provider=self._output_grants_provider,
                output_grant_allows=self._output_grant_allows,
            )
            try:
                launched, cancelled = await _await_launch_result(
                    await_thread_worker(
                        action_service.launch,
                        self.project,
                        self.store,
                        reference,
                        project_id=self.project_id,
                    )
                )
                launch_cancelled = launch_cancelled or cancelled
            except _ActionApprovalRequired as required:
                decision = await self.research.pause(required.approval)
                if decision != "approve":
                    raise ResearchActionSkipped("research action was not approved")
                self._approved_payloads.add(required.approval["payload_identity"])
                continue
            except ProjectAskActionUnpriced as unpriced:
                decision = await self.research.pause(
                    {
                        "kind": "unknown_cost",
                        "operation": "action",
                        "operation_id": unpriced.dispatch_id,
                        "action_id": unpriced.action_id,
                        "payload_identity": unpriced.payload_identity,
                        "reason": unpriced.reason,
                    }
                )
                if decision == "skip":
                    raise ResearchActionSkipped(
                        "research action with unknown cost was skipped"
                    )
                continue
            except _AdmissionBlocked as blocked:
                await self._admit_after_pause(blocked.admission)
                continue
            if inspect.isawaitable(launched):
                launched, cancelled = await _await_launch_result(launched)
                launch_cancelled = launch_cancelled or cancelled
            if not isinstance(launched, ActionRunResponse):
                raise TypeError("canonical action runner returned an invalid response")
            response = launched
            break

        identity = self._launch_identities.get(reference.dispatch_id)
        if identity is None:
            raise RuntimeError("launched research action has no payload identity")

        async def record_terminal(outcome: ChildRunOutcome) -> None:
            await self._finalize_outcome(
                reference,
                identity,
                outcome,
                pause_for_unknown=False,
            )

        try:
            outcome = await self._wait_child(
                project=self.project,
                project_id=self.project_id,
                response=response,
                record_dispatch=lambda dispatch: self._record_dispatch(
                    reference, dispatch
                ),
                stop_requested=lambda: launch_cancelled,
                rate_actual_cost=self._rate_actual_cost,
                record_terminal=record_terminal,
            )
        except ChildRunDrainTimeout:
            if launch_cancelled:
                raise asyncio.CancelledError from None
            raise
        await self._finalize_outcome(
            reference,
            identity,
            outcome,
            pause_for_unknown=not launch_cancelled,
        )
        if launch_cancelled:
            raise asyncio.CancelledError
        return {
            "status": outcome.status,
            "receipt_id": self._receipt_id(outcome),
            "outputs": [dict(output) for output in outcome.outputs],
            "errors": [dict(error) for error in outcome.errors],
            "warnings": list(outcome.warnings),
        }

    def _reference(self, event_ref: Mapping[str, Any]) -> PreparedActionReference:
        if set(event_ref) != {"event_seq", "dispatch_id"}:
            raise ValueError("prepared action event reference is malformed")
        event_seq = event_ref.get("event_seq")
        dispatch_id = event_ref.get("dispatch_id")
        if (
            isinstance(event_seq, bool)
            or not isinstance(event_seq, int)
            or event_seq <= 0
            or not isinstance(dispatch_id, str)
            or not dispatch_id
        ):
            raise ValueError("prepared action event reference is malformed")
        return PreparedActionReference(
            thread_id=str(self.turn["thread_id"]),
            turn_id=str(self.turn["id"]),
            event_seq=event_seq,
            dispatch_id=dispatch_id,
        )

    def _admit_sync(self, research_id: str, **facts: Any) -> dict[str, Any]:
        admission = _Admission(
            operation_id=str(facts["operation_id"]),
            payload_identity=str(facts["payload_identity"]),
            operation_kind=str(facts["operation_kind"]),
            estimate_micros=facts.get("estimate_micros"),
            metadata=dict(facts.get("metadata") or {}),
        )
        try:
            return self.research.store.admit_operation(
                research_id,
                operation_id=admission.operation_id,
                payload_identity=admission.payload_identity,
                operation_kind=admission.operation_kind,
                estimate_micros=admission.estimate_micros,
                metadata=admission.metadata,
            )
        except Exception as exc:
            raise _AdmissionBlocked(admission, exc) from exc

    async def _admit_after_pause(self, admission: _Admission) -> None:
        while True:
            try:
                await self.research.admit(
                    admission.operation_id,
                    admission.payload_identity,
                    admission.operation_kind,
                    admission.estimate_micros,
                    metadata=admission.metadata,
                )
                return
            except Exception as exc:
                approval = getattr(exc, "approval", None)
                if not isinstance(approval, dict):
                    raise
                decision = await self.research.pause(approval)
                if decision == "skip":
                    raise ResearchActionSkipped("research action budget was skipped")

    def _record_dispatch(
        self, reference: PreparedActionReference, dispatch: ChildRunDispatch
    ) -> None:
        key = (reference.dispatch_id, dispatch)
        if key in self._recorded_dispatches:
            return
        payload = asdict(dispatch)
        payload.update(
            {
                "operation_id": reference.dispatch_id,
                "dispatch_id": reference.dispatch_id,
                "prepared_event_seq": reference.event_seq,
            }
        )
        self.store.append_event(
            str(self.turn["id"]), kind="research_child", payload=payload
        )
        self._recorded_dispatches.add(key)

    async def _settle(
        self,
        reference: PreparedActionReference,
        identity: str,
        outcome: ChildRunOutcome,
        *,
        pause_for_unknown: bool,
    ) -> None:
        actual = outcome.billed_cost_micros
        if actual is None:
            if pause_for_unknown:
                await self.research.pause(
                    {
                        "kind": "unknown_cost",
                        "operation": "action",
                        "operation_id": reference.dispatch_id,
                    }
                )
            return
        await await_thread_worker(
            self.research.store.settle_operation,
            self.research.id,
            operation_id=reference.dispatch_id,
            payload_identity=identity,
            actual_micros=actual,
        )

    async def _finalize_outcome(
        self,
        reference: PreparedActionReference,
        identity: str,
        outcome: ChildRunOutcome,
        *,
        pause_for_unknown: bool,
    ) -> None:
        await self._settle(
            reference,
            identity,
            outcome,
            pause_for_unknown=pause_for_unknown,
        )
        await self._save_output_grant(outcome)
        await self._record_child_usage(reference, outcome)

    async def _save_output_grant(self, outcome: ChildRunOutcome) -> None:
        if outcome.status != "completed" or not outcome.outputs:
            return
        receipt_id = self._receipt_id(outcome)
        if receipt_id is None:
            return

        def save() -> dict[str, Any]:
            current = self.research.store.get(self.research.id)
            grants = [
                str(value)
                for value in current.get("output_grants", [])
                if isinstance(value, str) and value
            ]
            if receipt_id in grants:
                return current
            grants.append(receipt_id)
            return self.research.store.update(
                self.research.id,
                expected_revision=current["revision"],
                output_grants=grants,
            )

        await await_thread_worker(save)

    @staticmethod
    def _receipt_id(outcome: ChildRunOutcome) -> str | None:
        receipt_id = outcome.dispatch.receipt_id
        if receipt_id is None and isinstance(outcome.public_job_status, Mapping):
            value = outcome.public_job_status.get("receipt_id")
            receipt_id = value if isinstance(value, str) and value else None
        return receipt_id

    async def _record_child_usage(
        self, reference: PreparedActionReference, outcome: ChildRunOutcome
    ) -> None:
        """Append one durable Ask usage event for this replay-stable dispatch."""

        def record() -> None:
            cursor = 0
            while True:
                page = self.store.events(
                    str(self.turn["thread_id"]), after=cursor, limit=200
                )
                for event in page["events"]:
                    payload = event.get("payload")
                    if (
                        event.get("turn_id") == self.turn["id"]
                        and event.get("kind") == "usage"
                        and isinstance(payload, Mapping)
                        and payload.get("operation") == "action"
                        and payload.get("operation_id") == reference.dispatch_id
                    ):
                        return
                if not page["has_more"]:
                    break
                cursor = int(page["cursor"])
            self.store.append_event(
                str(self.turn["id"]),
                kind="usage",
                payload={
                    "operation": "action",
                    "operation_id": reference.dispatch_id,
                    "cost": outcome.provider_cost_usd,
                    "receipt_id": self._receipt_id(outcome),
                },
            )

        await await_thread_worker(record)


__all__ = [
    "ProjectQAExecutionRefused",
    "ProjectQAExecutionService",
    "PreparedResearchAction",
    "ResearchActionSkipped",
    "ResearchActionResult",
    "ResearchSessionLike",
]
