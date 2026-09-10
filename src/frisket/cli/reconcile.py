"""`frisket reconcile` — the operator lever for stuck paid-effect checkpoints.

A `reserved` effect checkpoint whose process died after a provider MAY have
been reached refuses automatic retry forever, by design: buying again on a
guess is the unforgivable failure. Unknown provider outcomes therefore
require an operator. This command is that operator path:

  frisket reconcile list [--project PATH]
      every stuck effect across the unified store, and what it blocks
  frisket reconcile discard <checkpoint-id> [--project PATH]
      the operator asserts the effect did NOT happen (or eats the cost):
      the refusal is cleared and the unit can be bought fresh
  frisket reconcile accept-charged <checkpoint-id> [--project PATH]
      the operator asserts the effect DID happen: the decision is recorded
      and the unit is never called again

No auto-resolution: this process cannot know what the provider did — only
the operator can find out, out of band.  Payload bodies (provider responses,
recovery context) may hold provider data or secrets and are NEVER printed;
only safe metadata (ids, family, keys, state, age) appears.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Human names for each family's caller-owned group/unit keys (the shared
# store's docstring is the source of the mapping).
_FAMILY_KEY_LABELS = {
    "row_effect": ("run", "row"),
    "model_call": ("group", "call"),
    "plugin_effect": ("effect", "unit"),
    "reduce_group_summary": ("group", "unit"),
    "embedding_index_refresh": ("index", "unit"),
    "export_google_sheets_egress": ("idempotency_key", "egress"),
    "embedding_dimension_probe_egress": ("idempotency_key", "attempt"),
}

# Defect 4: embedding.index_create's dimension probe (embeddings.py's
# bespoke reservation-as-receipt lifecycle, NOT the shared EffectCheckpointStore)
# marks its pre-egress reservation as a RECEIPT EVIDENCE item, not an
# effect_checkpoints row. A crash between that marker and the provider's
# response leaves the receipt 'running' forever with no checkpoint --
# exactly as ambiguous as a 'reserved' effect_checkpoint, but invisible to
# `store.list_all()`, so reconcile must enumerate and resolve it separately.
_PROBE_EGRESS_FAMILY = "embedding_dimension_probe_egress"
_PROBE_EGRESS_EVIDENCE_KIND = "embedding_dimension_probe_egress_reservation"
_PROBE_CHECKPOINT_EVIDENCE_KIND = "embedding_dimension_probe_checkpoint"
_PROBE_OPERATOR_EVIDENCE_KIND = "embedding_dimension_probe_operator_decision"


def _probe_receipt_rows(project) -> list[dict[str, Any]]:
    """Every receipt stuck in the dimension-probe's ambiguous shape:
    'running', an egress reservation recorded, but no returned checkpoint.

    Normalized to the same row shape `_stuck_reason`/`_referent_description`
    already read (family/group_key/unit_key/state/payload/created_at) so it
    merges into one enumeration with the shared EffectCheckpointStore rows —
    an operator gets ONE list of everything stuck, not two.
    """

    rows = project.db.execute(
        "SELECT id, action_kind, idempotency_key, body, created_at "
        "FROM receipts WHERE status='running'"
    ).fetchall()
    stuck: list[dict[str, Any]] = []
    for row in rows:
        try:
            body = json.loads(row["body"] or "{}")
        except (TypeError, ValueError):
            continue
        evidence = body.get("evidence")
        if not isinstance(evidence, list):
            continue
        refs = [
            item["ref"]
            for item in evidence
            if isinstance(item, dict) and isinstance(item.get("ref"), dict)
        ]
        egress = next(
            (ref for ref in refs if ref.get("kind") == _PROBE_EGRESS_EVIDENCE_KIND),
            None,
        )
        if egress is None:
            continue
        checkpoint = next(
            (ref for ref in refs if ref.get("kind") == _PROBE_CHECKPOINT_EVIDENCE_KIND),
            None,
        )
        if checkpoint is not None:
            continue  # a durable checkpoint exists -- not stuck, just mid-lifecycle
        stuck.append(
            {
                "id": row["id"],
                "family": _PROBE_EGRESS_FAMILY,
                "action_kind": row["action_kind"],
                "group_key": row["idempotency_key"] or "",
                "unit_key": str(egress.get("attempt_id") or ""),
                "state": "reserved",
                "payload": None,
                "accounting_persisted": 0,
                "created_at": row["created_at"],
            }
        )
    return stuck


def _open_project(path_arg: str):
    from frisket.engine.store import Project

    path = Path(path_arg).expanduser()
    if not (path / "project.db").exists():
        print(
            f"frisket reconcile: not a frisket bundle (no project.db under "
            f"{path}); point --project at a .frisket bundle directory",
            file=sys.stderr,
        )
        return None
    return Project(path)


def _age(created_at: str) -> str:
    """Compact age from the store's UTC ``datetime('now')`` text."""

    try:
        created = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return "?"
    seconds = max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _referent_exists(db, family: str, group_key: str) -> bool | None:
    """Whether the family's group key still names a live referent.

    Only derivable where the group key IS a table key: ``row_effect`` groups
    by run id and ``embedding_index_refresh`` by index id.  ``model_call``
    groups by action kind, ``plugin_effect`` by a plugin-declared effect
    namespace, and ``reduce_group_summary`` by a params digest — none of
    those name a row that can be deleted, so orphanhood is not derivable
    (``None``) rather than guessed.
    """

    if family == "row_effect":
        try:
            run_id = int(group_key)
        except ValueError:
            return None
        return (
            db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone()
            is not None
        )
    if family == "embedding_index_refresh":
        return (
            db.execute(
                "SELECT 1 FROM embedding_indexes WHERE id=?", (group_key,)
            ).fetchone()
            is not None
        )
    return None


def _referent_description(row: dict[str, Any]) -> str:
    """What the user-visible blocked thing is, per family — never payload."""

    family = row["family"]
    group, unit = row["group_key"], row["unit_key"]
    if family == "row_effect":
        return f"run {group} row {unit}"
    if family == "model_call":
        return f"'{row['action_kind']}' call {unit}"
    if family == "plugin_effect":
        return f"plugin effect {group} unit {unit}"
    if family == "reduce_group_summary":
        return f"reduce group summary {unit}"
    if family == "embedding_index_refresh":
        return f"embedding index {group}"
    if family == "export_google_sheets_egress":
        return f"google_sheets export {group}"
    if family == _PROBE_EGRESS_FAMILY:
        return f"embedding dimension probe (receipt {row['id']})"
    return f"{family} {group}/{unit}"


def _row_effect_error(payload: Any) -> bool:
    """A row_effect replay payload recording a durable provider error
    (``{field: {..., "error": ...}}`` — the MapRunner results shape)."""

    return isinstance(payload, dict) and any(
        isinstance(field, dict) and field.get("error") for field in payload.values()
    )


def _stuck_reason(row: dict[str, Any], orphaned: bool | None) -> str | None:
    """Why this checkpoint needs (or records) an operator decision, and what
    refusing costs the user.  ``None`` means not stuck: a replayable
    returned success is consumed free by automatic resume, and a consumed
    provider-audit row is deliberately retained forever.
    """

    from frisket.engine.store.effect_checkpoints import is_operator_attestation

    referent = _referent_description(row)
    prefix = f"ORPHANED ({referent} no longer exists) " if orphaned else ""
    state = row["state"]
    if state == "reserved":
        return (
            f"{prefix}ambiguous: the provider may have been reached but no "
            f"response is durable; {referent} refuses retry until you decide "
            "(discard = retryable, accept-charged = keep the charge)"
        )
    if state == "returned":
        if row["payload"] is None:
            return (
                f"{prefix}returned payload is corrupt; replay refuses forever "
                f"and {referent} is blocked (discard to retry fresh)"
            )
        if row["family"] == "row_effect" and _row_effect_error(row["payload"]):
            return (
                f"{prefix}returned a durable provider error; resume replays "
                f"that error into {referent} (discard to retry fresh — a "
                "retry re-buys the FULL row, including any fields that "
                "succeeded)"
            )
        if orphaned:
            return (
                f"ORPHANED: {referent} no longer exists, so this paid "
                "response can never be consumed (discard to remove the dead "
                "record)"
            )
        return None
    if state == "consumed" and is_operator_attestation(row["payload"]):
        return (
            f"decided accept-charged: never re-called; {referent} stays "
            "blocked by that decision (discard to reverse it and retry)"
        )
    return None


def _list(project) -> int:
    from frisket.engine.store.effect_checkpoints import EffectCheckpointStore

    store = EffectCheckpointStore(project.db)
    lines: list[str] = []
    all_rows = [*store.list_all(), *_probe_receipt_rows(project)]
    for row in all_rows:
        referent_alive = _referent_exists(project.db, row["family"], row["group_key"])
        reason = _stuck_reason(row, referent_alive is False)
        if reason is None:
            continue
        group_label, unit_label = _FAMILY_KEY_LABELS.get(
            row["family"], ("group", "unit")
        )
        lines.append(
            f"  {row['id']}  {row['family']} kind={row['action_kind']} "
            f"{group_label}={row['group_key']} {unit_label}={row['unit_key']} "
            f"state={row['state']} age={_age(row['created_at'])} — {reason}"
        )
    if not lines:
        print(f"no stuck paid effects in {project.path}")
        return 0
    print(f"{len(lines)} stuck paid effect(s) in {project.path}:")
    for line in lines:
        print(line)
    print(
        "\nDecide each out of band (check the provider dashboard/bill), then:\n"
        "  frisket reconcile discard <checkpoint-id>        it did NOT happen "
        "(or eat the cost): unit becomes retryable\n"
        "  frisket reconcile accept-charged <checkpoint-id> it DID happen: "
        "record it, never call again\n"
        "Only decide a checkpoint whose owning process you have confirmed "
        "dead: a live in-flight reserved row looks identical here, and "
        "deciding it out from under that process makes its completion fail "
        "and roll back."
    )
    return 0


def _refused(exc) -> int:
    print(f"frisket reconcile: refused ({exc.code}): {exc}", file=sys.stderr)
    return 1


def _spend_facts_note(row: dict[str, Any], referent_alive: bool | None) -> str:
    """State-accurate money truth for a discarded checkpoint — never a blanket
    "facts stay booked" claim (review A3: a reserved or unaccounted row never
    booked one, and a row_effect orphan's facts were owned by the deleted
    run)."""

    state = row["state"]
    if state == "reserved":
        return (
            "No spend was booked through this reservation; if the provider "
            "did charge, that cost stays untracked (your decision)."
        )
    if state == "consumed":
        return (
            "Your accept-charged attestation is withdrawn; the unknown "
            "charge stays untracked."
        )
    if not row["accounting_persisted"]:
        return "No spend facts were booked through this checkpoint."
    if referent_alive is False:
        return (
            "Any spend facts it booked were owned by the deleted referent "
            "and may already be gone with it."
        )
    return "Its already-booked spend facts stay booked."


def _find_stuck_probe_receipt(project, receipt_id: str) -> dict[str, Any] | None:
    for row in _probe_receipt_rows(project):
        if row["id"] == receipt_id:
            return row
    return None


def _discard_probe_receipt(project, row: dict[str, Any]) -> int:
    """The probe-receipt twin of ``store.operator_discard``: the operator
    asserts the provider was NOT reached (or eats the cost) -- delete the
    running receipt (the exact mechanism ``_delete_embedding_probe_reservation``
    already uses for every OTHER refusal on this path) so the idempotency_key
    is retryable again."""

    from frisket.engine.store.receipts import ReceiptStore

    deleted = ReceiptStore(project).delete_running(row["id"])
    if not deleted:
        print(
            f"frisket reconcile: refused (probe_receipt_lost): receipt "
            f"{row['id']} is no longer 'running' -- it may have completed "
            "or already been discarded; re-run `frisket reconcile list`",
            file=sys.stderr,
        )
        return 1
    print(
        f"discarded {row['id']} (embedding_dimension_probe_egress, was "
        "reserved): the dimension probe reservation is removed -- the "
        "idempotency_key is retryable again. No spend was booked through "
        "this reservation; if the provider did charge, that cost stays "
        "untracked (your decision)."
    )
    return 0


def _accept_charged_probe_receipt(project, row: dict[str, Any]) -> int:
    """The probe-receipt twin of ``store.operator_accept_charged``: record
    that the provider WAS reached and never call it again, without inventing
    a response, result, or cost this process never observed."""

    from frisket.contracts.action import ActionError, ReceiptEvidence
    from frisket.engine.store.receipts import ReceiptStore

    store = ReceiptStore(project)
    stored = store.find_by_id(row["id"])
    if stored is None or stored.status != "running":
        print(
            f"frisket reconcile: refused (probe_receipt_lost): receipt "
            f"{row['id']} is no longer 'running' -- it may have completed "
            "or already been decided; re-run `frisket reconcile list`",
            file=sys.stderr,
        )
        return 1
    receipt = stored.parsed()
    evidence = [
        item
        for item in receipt.evidence
        if item.ref.get("kind") != _PROBE_OPERATOR_EVIDENCE_KIND
    ]
    evidence.append(
        ReceiptEvidence(
            ref={
                "kind": _PROBE_OPERATOR_EVIDENCE_KIND,
                "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            retention="pinned",
        )
    )
    decided = receipt.model_copy(
        update={
            "status": "failed",
            "evidence": evidence,
            "errors": [
                ActionError(
                    code="external_effect_reconciliation_required",
                    message=(
                        "an operator confirmed via `frisket reconcile "
                        "accept-charged` that this dimension probe reached "
                        "the provider; it is never retried automatically"
                    ),
                    action_kind=receipt.action_kind,
                    details={"reconciliation_required": True, "retryable": False},
                )
            ],
        }
    )
    landed = store.update_body_status(decided, require_status="running")
    if not landed:
        print(
            f"frisket reconcile: refused (probe_receipt_lost): receipt "
            f"{row['id']} changed before this decision landed; re-run "
            "`frisket reconcile list`",
            file=sys.stderr,
        )
        return 1
    referent = _referent_description(row)
    print(
        f"accepted as charged: {row['id']} (embedding_dimension_probe_egress, "
        f"{referent}). Recorded your decision; this probe will never be "
        "called again automatically.\n"
        "Honest limits: no provider response, result, or cost was invented — "
        "the true meter is unknown, so caps and receipts do not include this "
        f"charge, and {referent} stays blocked. If you later want a retry "
        "instead, `frisket reconcile discard` reverses this decision."
    )
    return 0


def _discard(project, checkpoint_id: str) -> int:
    from frisket.engine.store.effect_checkpoints import (
        CheckpointLost,
        EffectCheckpointRefused,
        EffectCheckpointStore,
        InvalidCheckpointState,
    )

    store = EffectCheckpointStore(project.db)
    row = store.get(checkpoint_id)
    if row is None:
        probe_row = _find_stuck_probe_receipt(project, checkpoint_id)
        if probe_row is not None:
            return _discard_probe_receipt(project, probe_row)
        return _refused(
            CheckpointLost(
                "no effect checkpoint has this id; nothing to discard "
                "(re-run `frisket reconcile list` for current ids)"
            )
        )
    referent_alive = _referent_exists(project.db, row["family"], row["group_key"])
    if row["state"] == "returned":
        # Payload policy lives here, where family payload semantics are
        # known: a replayable returned success is a PAID response automatic
        # resume consumes without another call — discarding it would buy the
        # unit again for nothing.  Discardable returned shapes: a corrupt
        # payload (refuses replay forever), a durable row_effect error (the
        # operator clears it to retry fresh), or an orphaned row whose
        # referent is gone (it can never be consumed).
        discardable = (
            row["payload"] is None
            or (row["family"] == "row_effect" and _row_effect_error(row["payload"]))
            or referent_alive is False
        )
        if not discardable:
            return _refused(
                InvalidCheckpointState(
                    "this returned checkpoint holds the provider's durable "
                    "successful response; resuming the run consumes it "
                    "WITHOUT another paid call, so nothing is stuck — "
                    "discarding would only buy the unit again"
                )
            )
    try:
        # expected_state pins the store's DELETE to the exact state this
        # policy decision was based on: if the checkpoint changes in the gap
        # (an in-flight complete() landing a paid response), the store
        # refuses instead of deleting a row the operator never saw (F1).
        discarded = store.operator_discard(checkpoint_id, expected_state=row["state"])
    except EffectCheckpointRefused as exc:
        return _refused(exc)
    referent = _referent_description(discarded)
    if referent_alive is False:
        outcome = f"{referent} no longer exists; the dead record is removed."
    else:
        outcome = f"{referent} is retryable again — resume or backfill to buy it fresh."
    print(
        f"discarded {discarded['id']} ({discarded['family']}, was "
        f"{discarded['state']}): {outcome} "
        f"{_spend_facts_note(discarded, referent_alive)}"
    )
    return 0


def _accept_charged(project, checkpoint_id: str) -> int:
    from frisket.engine.store.effect_checkpoints import (
        CheckpointLost,
        EffectCheckpointRefused,
        EffectCheckpointStore,
    )

    store = EffectCheckpointStore(project.db)
    if store.get(checkpoint_id) is None:
        probe_row = _find_stuck_probe_receipt(project, checkpoint_id)
        if probe_row is not None:
            return _accept_charged_probe_receipt(project, probe_row)
        return _refused(
            CheckpointLost(
                "no effect checkpoint has this id; nothing to accept "
                "(re-run `frisket reconcile list` for current ids)"
            )
        )
    try:
        decided = store.operator_accept_charged(checkpoint_id)
    except EffectCheckpointRefused as exc:
        return _refused(exc)
    referent = _referent_description(decided)
    print(
        f"accepted as charged: {decided['id']} ({decided['family']}, "
        f"{referent}). Recorded your decision; this unit will never be "
        "called again automatically.\n"
        "Honest limits: no provider response, result, or cost was invented — "
        "the true meter is unknown, so caps and receipts do not include this "
        f"charge, and {referent} stays blocked. If you later want a retry "
        "instead, `frisket reconcile discard` reverses this decision."
    )
    return 0


def reconcile(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="frisket reconcile",
        description=(
            "decide stuck paid-effect checkpoints (an operator lever: "
            "list what is stuck, then discard or accept-charged each)"
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    def _with_project(sub) -> None:
        sub.add_argument(
            "--project",
            default=".",
            help="path to the .frisket project bundle (default: current dir)",
        )

    list_parser = subcommands.add_parser(
        "list", help="list stuck paid effects and what each one blocks"
    )
    _with_project(list_parser)
    discard_parser = subcommands.add_parser(
        "discard",
        help="the effect did NOT happen (or eat the cost): make the unit retryable",
    )
    discard_parser.add_argument("checkpoint_id")
    _with_project(discard_parser)
    accept_parser = subcommands.add_parser(
        "accept-charged",
        help="the effect DID happen: record the charge, never re-call",
    )
    accept_parser.add_argument("checkpoint_id")
    _with_project(accept_parser)
    args = parser.parse_args(argv)

    project = _open_project(args.project)
    if project is None:
        return 2
    try:
        if args.command == "list":
            return _list(project)
        if args.command == "discard":
            return _discard(project, args.checkpoint_id)
        return _accept_charged(project, args.checkpoint_id)
    finally:
        project.close()
