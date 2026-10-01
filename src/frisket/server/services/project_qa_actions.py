"""Prepare and dispatch Project Ask actions through existing action services."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from frisket.actions.core import MapRows, ModelRows, SemanticJoin
from frisket.authoring.project_ask_actions import (
    PreparedProjectAskDraft,
    prepare_validated_project_ask_draft,
)
from frisket.engine.executor.map_rows_action import (
    TypedMapRowsPlanError,
    build_typed_map_rows_plan,
)
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.features.watchlists.specs import canonical_json


CatalogPayloadProvider = Callable[[], Mapping[str, Any]]
QuoteProvider = Callable[[dict[str, Any]], Mapping[str, Any]]
ResearchForTurn = Callable[[str], Mapping[str, Any]]
DispatchAuthorizer = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]
ActionRunner = Callable[[str, dict[str, Any]], Any]
OperationAdmitter = Callable[..., Any]


@dataclass(frozen=True)
class PreparedActionReference:
    """An immutable action-proposal event reference used as a dispatch key."""

    thread_id: str
    turn_id: str
    event_seq: int
    dispatch_id: str


@dataclass(frozen=True)
class PreparedProjectAskAction:
    """Saved review-only action facts with no execution authority."""

    proposal: dict[str, Any]
    event: dict[str, Any]
    reference: PreparedActionReference
    prepared: PreparedProjectAskDraft
    payload_identity: str


def _hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(payload)).encode()).hexdigest()


def _implementation_identity(prepared: PreparedProjectAskDraft) -> dict[str, Any]:
    if prepared.implementation_identity is not None:
        return dict(prepared.implementation_identity)
    return {
        "kind": prepared.request.action_id,
        "catalog_sha256": _hash(prepared.catalog_entry),
    }


def _action_body(
    prepared: PreparedProjectAskDraft, *, dispatch_id: str
) -> dict[str, Any]:
    """The ordinary ActionRun request, with a server-owned dispatch key."""

    return {
        **prepared.request.model_dump(mode="json"),
        "idempotency_key": dispatch_id,
    }


def _prepared_target_facts(
    project: Project, prepared: PreparedProjectAskDraft
) -> tuple[dict[str, Any] | None, str | None]:
    """Freeze normal typed-map output preconditions, never an Ask-local plan."""

    terminal = prepared.bound.action.definition.run
    if not isinstance(terminal, (MapRows, ModelRows)) or isinstance(
        terminal, SemanticJoin
    ):
        return None, "normal prepared output facts are unavailable for this action"
    try:
        plan = build_typed_map_rows_plan(project, prepared.bound)
    except TypedMapRowsPlanError as error:
        return None, f"normal output preparation refused: {error.code}"
    except (KeyError, TypeError, ValueError):
        return None, "normal prepared output facts are unavailable for this action"
    return (
        {
            "kind": "typed_map_rows",
            "output_names": dict(plan.output_names),
            "output_target_preconditions": dict(plan.output_target_preconditions),
            "replace_existing": prepared.request.replace_existing,
        },
        None,
    )


def _quote_facts(
    quote_provider: QuoteProvider | None, action: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """Accept only a normal server estimate with an integer billed amount."""

    if quote_provider is None:
        return None, "normal action quote is unavailable"
    try:
        result = quote_provider(action)
    except (KeyError, TypeError, ValueError):
        return None, "normal action quote is unavailable"
    estimate = result.get("estimate") if isinstance(result, Mapping) else None
    if not isinstance(estimate, Mapping):
        return None, "normal action quote is unavailable"
    billed_cost = estimate.get("billed_cost")
    if (
        isinstance(billed_cost, bool)
        or not isinstance(billed_cost, int)
        or billed_cost < 0
    ):
        return None, "normal action quote has unknown cost"
    requires_confirmation = estimate.get("requires_confirmation")
    if not isinstance(requires_confirmation, bool):
        return None, "normal action quote is malformed"
    promise_set_hash = estimate.get("promise_set_hash")
    if requires_confirmation and (
        not isinstance(promise_set_hash, str) or not promise_set_hash
    ):
        return None, "normal action quote is missing its confirmation promise"
    policy_id = estimate.get("policy_id")
    if not isinstance(policy_id, str) or not policy_id:
        return None, "normal action quote is missing its policy"
    return {"estimate": dict(estimate)}, None


def _prepared_payload(
    project: Project,
    prepared: PreparedProjectAskDraft,
    *,
    dispatch_id: str,
    quote_provider: QuoteProvider | None,
) -> dict[str, Any]:
    """Canonical facts a launch must reproduce before budget admission."""

    action = _action_body(prepared, dispatch_id=dispatch_id)
    effects, effects_reason = _prepared_target_facts(project, prepared)
    quote, quote_reason = _quote_facts(quote_provider, action)
    catalog_effects = prepared.catalog_entry.get("side_effects")
    required_capabilities = prepared.catalog_entry.get("required_capabilities")
    if not isinstance(catalog_effects, list) or not isinstance(
        required_capabilities, list
    ):
        raise ValueError("prepared action catalog facts are malformed")
    automatic_eligible = (
        effects_reason is None
        and quote_reason is None
        and not prepared.request.replace_existing
    )
    authority = {
        "dispatch_id": dispatch_id,
        "draft": prepared.draft,
        "request": action,
        "implementation_identity": _implementation_identity(prepared),
        "effects": effects,
        "catalog_effects": list(catalog_effects),
        "required_capabilities": list(required_capabilities),
        "quote": quote,
        "automatic_eligible": automatic_eligible,
        "preparation_reason": effects_reason or quote_reason,
        "scope": prepared.request.scope.model_dump(mode="json"),
    }
    return {**authority, "payload_identity": _hash(authority)}


def save_prepared_project_ask_action(
    project: Project,
    turn: Mapping[str, Any],
    store: ProjectQAStore,
    *,
    title: str,
    draft: Mapping[str, Any],
    catalog_payload: Mapping[str, Any],
    quote_provider: QuoteProvider | None = None,
) -> PreparedProjectAskAction:
    """Validate and persist a canonical action proposal on its Ask event.

    A proposal remains visible to the existing frontend if normal automatic
    preparation cannot quote or freeze its output facts. Such a proposal is
    review-only and cannot enter the automatic dispatch path.
    """

    prepared = prepare_validated_project_ask_draft(
        project,
        draft,
        catalog_payload=catalog_payload,
        scope=turn["scope"],
    )
    proposal = {"title": title, "spec": prepared.draft}
    dispatch_id = f"ask-action:{uuid4()}"
    stored = _prepared_payload(
        project,
        prepared,
        dispatch_id=dispatch_id,
        quote_provider=quote_provider,
    )
    event = store.append_event(
        str(turn["id"]),
        kind="action_proposal",
        payload={"proposal": proposal, "prepared_action": stored},
    )
    return PreparedProjectAskAction(
        proposal=proposal,
        event=event,
        reference=PreparedActionReference(
            thread_id=str(event["thread_id"]),
            turn_id=str(event["turn_id"]),
            event_seq=int(event["seq"]),
            dispatch_id=dispatch_id,
        ),
        prepared=prepared,
        payload_identity=stored["payload_identity"],
    )


def _stored_event(
    store: ProjectQAStore, reference: PreparedActionReference
) -> dict[str, Any]:
    events = store.events(reference.thread_id, after=reference.event_seq - 1, limit=1)[
        "events"
    ]
    if len(events) != 1:
        raise ValueError("prepared action reference is unavailable")
    event = events[0]
    if (
        event.get("seq") != reference.event_seq
        or event.get("turn_id") != reference.turn_id
        or event.get("kind") != "action_proposal"
    ):
        raise ValueError("prepared action reference is unavailable")
    return event


def _stored_prepared_action(
    event: Mapping[str, Any], reference: PreparedActionReference
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("prepared action reference is malformed")
    proposal = payload.get("proposal")
    stored = payload.get("prepared_action")
    if not isinstance(proposal, Mapping) or not isinstance(stored, Mapping):
        raise ValueError("prepared action reference is malformed")
    if stored.get("dispatch_id") != reference.dispatch_id:
        raise ValueError("prepared action reference does not match dispatch")
    if not isinstance(proposal.get("spec"), Mapping):
        raise ValueError("prepared action reference is malformed")
    return dict(proposal), dict(stored)


class ProjectAskActionService:
    """Revalidate and dispatch prepared Ask actions through normal services."""

    def __init__(
        self,
        *,
        catalog_payload_provider: CatalogPayloadProvider,
        quote_provider: QuoteProvider,
        research_for_turn: ResearchForTurn,
        authorize_dispatch: DispatchAuthorizer,
        admit_operation: OperationAdmitter,
        run_action: ActionRunner,
    ) -> None:
        self._catalog_payload_provider = catalog_payload_provider
        self._quote_provider = quote_provider
        self._research_for_turn = research_for_turn
        self._authorize_dispatch = authorize_dispatch
        self._admit_operation = admit_operation
        self._run_action = run_action

    def launch(
        self,
        project: Project,
        store: ProjectQAStore,
        reference: PreparedActionReference,
        *,
        project_id: str,
        confirmation_hash: str | None = None,
    ) -> Any:
        """Reprepare, quote, admit once, then invoke ordinary ActionRun.

        The research record binds a proposal event to its owning research turn;
        callers never supply a research id or billable amount.
        """

        research = self._research_for_turn(reference.turn_id)
        research_id = research.get("id") if isinstance(research, Mapping) else None
        if (
            not isinstance(research_id, str)
            or not research_id
            or research.get("turn_id") != reference.turn_id
        ):
            raise ValueError("prepared action does not belong to this research turn")
        event = _stored_event(store, reference)
        proposal, stored = _stored_prepared_action(event, reference)
        turn = store.get_turn(reference.turn_id)
        if turn["thread_id"] != reference.thread_id:
            raise ValueError("prepared action reference is unavailable")
        prepared = prepare_validated_project_ask_draft(
            project,
            proposal["spec"],
            catalog_payload=self._catalog_payload_provider(),
            scope=turn["scope"],
        )
        current = _prepared_payload(
            project,
            prepared,
            dispatch_id=reference.dispatch_id,
            quote_provider=self._quote_provider,
        )
        if current != stored:
            raise ValueError("prepared action changed; reprepare it before launch")
        if current["quote"] is None:
            raise ValueError(str(current["preparation_reason"]))
        approval = self._authorize_dispatch(current)
        if (
            not isinstance(approval, Mapping)
            or approval.get("payload_identity") != current["payload_identity"]
        ):
            raise ValueError("prepared action requires current approval")
        estimate = current["quote"]["estimate"]
        promise_set_hash = estimate.get("promise_set_hash")
        if estimate["requires_confirmation"]:
            if confirmation_hash != promise_set_hash:
                raise ValueError(
                    "confirmation_hash does not match the normal action quote"
                )
        elif confirmation_hash is not None and (
            not isinstance(confirmation_hash, str) or not confirmation_hash
        ):
            raise ValueError("confirmation_hash must be a non-empty string")
        self._admit_operation(
            research_id,
            operation_id=reference.dispatch_id,
            payload_identity=current["payload_identity"],
            operation_kind="action",
            estimate_micros=estimate["billed_cost"],
            metadata={
                "prepared_event_seq": reference.event_seq,
                "prepared_turn_id": reference.turn_id,
                "action_id": prepared.request.action_id,
            },
        )
        body = _action_body(prepared, dispatch_id=reference.dispatch_id)
        if confirmation_hash is not None:
            body["confirmation"] = confirmation_hash
        return self._run_action(project_id, body)
