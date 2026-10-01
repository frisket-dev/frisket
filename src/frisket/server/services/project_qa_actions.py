"""Prepare and dispatch Project Ask actions through existing action services."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from frisket.authoring.project_ask_actions import (
    PreparedProjectAskDraft,
    prepare_validated_project_ask_draft,
)
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.features.watchlists.specs import canonical_json


CatalogPayloadProvider = Callable[[], Mapping[str, Any]]
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


def _prepared_payload(
    prepared: PreparedProjectAskDraft, *, dispatch_id: str
) -> dict[str, Any]:
    """Canonical stored facts that a later launch must reproduce exactly."""

    effects = sorted(
        effect
        for effect in prepared.catalog_entry.get("side_effects", [])
        if isinstance(effect, str)
    )
    authority = {
        "dispatch_id": dispatch_id,
        "draft": prepared.draft,
        "implementation_identity": _implementation_identity(prepared),
        "effects": effects,
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
) -> PreparedProjectAskAction:
    """Validate and persist a canonical action proposal on its Ask event."""

    prepared = prepare_validated_project_ask_draft(
        project,
        draft,
        catalog_payload=catalog_payload,
        scope=turn["scope"],
    )
    proposal = {"title": title, "spec": prepared.draft}
    dispatch_id = f"ask-action:{uuid4()}"
    stored = _prepared_payload(prepared, dispatch_id=dispatch_id)
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
    """Revalidate and dispatch prepared Ask actions without a new executor path."""

    def __init__(
        self,
        *,
        catalog_payload_provider: CatalogPayloadProvider,
        admit_operation: OperationAdmitter,
        run_action: ActionRunner,
    ) -> None:
        self._catalog_payload_provider = catalog_payload_provider
        self._admit_operation = admit_operation
        self._run_action = run_action

    def launch(
        self,
        project: Project,
        store: ProjectQAStore,
        reference: PreparedActionReference,
        *,
        project_id: str,
        research_id: str,
        estimate_micros: int | None,
        confirmation_hash: str | None = None,
    ) -> Any:
        """Revalidate, admit once, then invoke the ordinary action-run service.

        The caller owns budget and approval policy through ``admit_operation``.
        This method deliberately does not inspect or mint output grants.
        """

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
        current = _prepared_payload(prepared, dispatch_id=reference.dispatch_id)
        if current != stored:
            raise ValueError("prepared action changed; reprepare it before launch")
        if confirmation_hash is not None and (
            not isinstance(confirmation_hash, str) or not confirmation_hash
        ):
            raise ValueError("confirmation_hash must be a non-empty string")
        self._admit_operation(
            research_id,
            operation_id=reference.dispatch_id,
            payload_identity=current["payload_identity"],
            operation_kind="action",
            estimate_micros=estimate_micros,
            metadata={
                "prepared_event_seq": reference.event_seq,
                "prepared_turn_id": reference.turn_id,
                "action_id": prepared.request.action_id,
            },
        )
        body = prepared.request.model_dump(mode="json")
        body["idempotency_key"] = reference.dispatch_id
        if confirmation_hash is not None:
            body["confirmation"] = confirmation_hash
        return self._run_action(project_id, body)
