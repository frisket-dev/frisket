"""THE durable reserve→returned→consume/refuse lifecycle for paid effects.

The lifecycle is shared by row/model checkpoints in ``runs.py`` and the
remaining paid-effect families, instead of letting callers copy the state
machine and diverge in error vocabulary or JSON canonicalization. This module
owns the shared parts:

- the state machine.  Stored states are ``reserved`` (durable BEFORE possible
  egress; deliberately ambiguous, refuses automatic retry), ``returned`` (the
  effect's response is durable; safe to replay without another egress), and
  ``consumed`` (a terminal caller-owned state). ``discard`` and ``retire``
  are terminal deletions:
  discarding is allowed only for a ``reserved`` row whose caller proves
  egress did not start, and retiring happens in the same transaction that
  commits the effect's facts (the ``runs.py`` shape).  The one other exit
  is the OPERATOR pair (``operator_discard`` / ``operator_accept_charged``):
  a human, via ``frisket reconcile``, decides a stuck row the
  automatic paths must refuse forever.
- the transaction discipline: ``BEGIN IMMEDIATE``; complete and
  consume+retire are each ONE transaction, with caller work (fact accrual,
  result insertion) threaded through in-transaction hooks.
- JSON canonicalization (:func:`canonical_json`).
- one refusal vocabulary (:class:`EffectCheckpointRefused` and subclasses).

Payload semantics stay caller-owned: ``identity`` is an opaque caller string
compared verbatim (drift refuses), ``payload`` is a caller dict stored
canonically and decoded leniently (corruption is visible as ``None``, never
mistaken for permission to egress again).

Live families: ``row_effect`` and ``model_call`` (thin wrappers in
``runs.py``), ``reduce_group_summary`` (``reduces.py``), and
``embedding_index_refresh`` (``embeddings.py``). The bespoke research-answer
reservation-as-receipt stays outside on purpose (its reservation IS the
receipt). The fence-closure test (``tests/engine/test_paid_effect_fence_closure.py``)
keeps this the complete list of paid producers.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, ClassVar

# The retained payload written when an OPERATOR (frisket reconcile) decides a
# reserved unit's unknown provider effect really happened.  It is an
# attestation of a human decision — never a provider response, result, or
# cost — and is the one consumed payload the operator may later reverse.
OPERATOR_RESOLUTION_SCHEMA_VERSION = "frisket.effect_checkpoint_operator_resolution.v1"


def is_operator_attestation(payload: Any) -> bool:
    """Whether a consumed payload is the operator's own reconcile decision
    (reversible by the operator) rather than retained provider audit
    evidence (immutable historical truth)."""

    return (
        isinstance(payload, dict)
        and payload.get("schema_version") == OPERATOR_RESOLUTION_SCHEMA_VERSION
    )


def canonical_json(value: Any) -> str:
    """The one JSON spelling for checkpoint identities and payloads.

    ``sort_keys`` + compact separators make the encoding deterministic so
    stored strings can be compared verbatim; ``allow_nan=False`` refuses
    values SQLite/JSON round-trips cannot preserve; ``ensure_ascii=False``
    keeps non-ASCII text byte-stable with the stored UTF-8 rather than
    escape-encoded."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


class EffectCheckpointRefused(RuntimeError):
    """Base of the shared refusal vocabulary; ``code`` is the stable name."""

    code: ClassVar[str] = "effect_checkpoint_refused"


class CheckpointLost(EffectCheckpointRefused):
    """The expected state row vanished or changed under this operation."""

    code = "checkpoint_lost"


class IdentityMismatch(EffectCheckpointRefused):
    """The checkpoint was minted for a different request or source value."""

    code = "identity_mismatch"


class AmbiguousReserved(EffectCheckpointRefused):
    """A provider may have been reached but no response is durable."""

    code = "ambiguous_reserved"


class InvalidCheckpointState(EffectCheckpointRefused):
    """The checkpoint is in a state this operation must not act on."""

    code = "invalid_checkpoint_state"


class ModelCallIdMissing(EffectCheckpointRefused):
    """A checkpoint batch carried a ``model_calls`` entry without an ``id``.

    Replay dedup on the checkpoint paths rests entirely on caller-minted call
    ids (``INSERT OR IGNORE`` by id): an id-less entry would mint a fresh
    uuid on every replay and accrue project-key cap spend once per replay.
    The checkpoint path refuses it before anything is written."""

    code = "model_call_id_missing"


def require_model_call_ids(batch: list[dict[str, Any]]) -> None:
    """Refuse a checkpoint batch whose ``model_calls`` lack durable ids.

    Non-dict entries are skipped, mirroring the fact writers (they silently
    drop them, so they can never accrue).  Everything else must carry a
    non-blank string ``id`` minted at call time."""

    for result in batch:
        if not isinstance(result, dict):
            continue
        for call in result.get("model_calls") or []:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id.strip():
                raise ModelCallIdMissing(
                    "a checkpoint model_calls entry carries no durable id; "
                    "replay dedup requires the id minted at call time, so "
                    "this batch is refused before anything is written"
                )


class EffectCheckpointStore:
    """One durable lifecycle over the shared ``effect_checkpoints`` table.

    A logical unit is ``(family, group_key, unit_key)`` — caller-owned
    strings: the family names the consumer, the group key its recovery scope,
    and the unit key the unit within it. ``checkpoint_id`` is the caller-computed
    durable id for the unit.
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    # -- reads ------------------------------------------------------------

    def get(self, checkpoint_id: str) -> dict[str, Any] | None:
        row = self.db.execute(f"{_SELECT} WHERE id=?", (checkpoint_id,)).fetchone()
        return _decoded(row)

    def find_unit(
        self, *, family: str, group_key: str, unit_key: str
    ) -> dict[str, Any] | None:
        row = self.db.execute(
            f"{_SELECT} WHERE family=? AND group_key=? AND unit_key=?",
            (family, group_key, unit_key),
        ).fetchone()
        return _decoded(row)

    def list_group(self, *, family: str, group_key: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            f"{_SELECT} WHERE family=? AND group_key=? ORDER BY unit_key, id",
            (family, group_key),
        ).fetchall()
        return [decoded for row in rows if (decoded := _decoded(row)) is not None]

    def find_recovery_candidates(
        self,
        *,
        family: str,
        action_kind: str,
        unit_key: str,
        canonical_group_key: str,
        legacy_group_key: str,
        legacy_identity: str,
        payload_schema_version: str,
        receipt_id: str,
    ) -> list[dict[str, Any]]:
        """At most two exact candidates for one caller-owned recovery unit.

        Exact receipt association wins, followed by the canonical group, the
        legacy same-key group, and finally a legacy identity match. Returning
        two rows is enough to detect a corrupt duplicate receipt association;
        unrelated family history is filtered in SQL and never decoded.
        """

        receipt_sql = (
            "json_valid(payload) "
            "AND json_extract(payload, '$.schema_version')=? "
            "AND json_extract(payload, '$.receipt_id')=?"
        )
        rows = self.db.execute(
            f"{_SELECT} WHERE family=? AND action_kind=? AND unit_key=? AND ("
            f"({receipt_sql}) OR group_key IN (?, ?) OR identity=?"
            ") ORDER BY CASE "
            f"WHEN ({receipt_sql}) THEN 0 "
            "WHEN group_key=? THEN 1 "
            "WHEN group_key=? THEN 2 "
            "ELSE 3 END, id LIMIT 2",
            (
                family,
                action_kind,
                unit_key,
                payload_schema_version,
                receipt_id,
                canonical_group_key,
                legacy_group_key,
                legacy_identity,
                payload_schema_version,
                receipt_id,
                canonical_group_key,
                legacy_group_key,
            ),
        ).fetchall()
        return [decoded for row in rows if (decoded := _decoded(row)) is not None]

    def list_all(self) -> list[dict[str, Any]]:
        """Every checkpoint in the bundle, all families — the operator
        reconciliation read (`frisket reconcile list`)."""

        rows = self.db.execute(
            f"{_SELECT} ORDER BY family, group_key, unit_key, id"
        ).fetchall()
        return [decoded for row in rows if (decoded := _decoded(row)) is not None]

    # -- lifecycle --------------------------------------------------------

    def reserve(
        self,
        checkpoint_id: str,
        *,
        family: str,
        group_key: str,
        unit_key: str,
        action_kind: str,
        identity: str,
        authorized_attempt_id: str | None = None,
        payload: dict[str, Any] | None = None,
        run_id: int | None = None,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
    ) -> bool:
        """Durably reserve one logical unit immediately before possible egress.

        Returns ``True`` only to the invocation that created the reservation.
        A concurrent/exact retry gets ``False`` and must inspect the existing
        state (:meth:`returned_for_replay`); it may replay ``returned`` but
        must never call through an ambiguous ``reserved`` row.  ``payload``
        is optional caller recovery context (the model-call shape stores its
        reconstruction payload at reserve time)."""

        encoded = None if payload is None else canonical_json(payload)
        with self._txn():
            self._fence_writer(
                run_id=run_id,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=claimless_direct_effect,
                authorized_attempt_id=authorized_attempt_id,
            )
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO effect_checkpoints "
                "(id, family, group_key, unit_key, action_kind, identity, "
                "authorized_attempt_id, state, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?)",
                (
                    checkpoint_id,
                    family,
                    group_key,
                    unit_key,
                    action_kind,
                    identity,
                    authorized_attempt_id,
                    encoded,
                ),
            ).rowcount
        return inserted == 1

    def returned_for_replay(
        self,
        *,
        family: str,
        group_key: str,
        unit_key: str,
        action_kind: str,
        identity: str,
    ) -> dict[str, Any]:
        """The unit's ``returned`` checkpoint, or the named refusal.

        This is the recovery decision tree every copy re-implements today:
        missing → :class:`CheckpointLost`; a different request or source
        value → :class:`IdentityMismatch`; still ``reserved`` →
        :class:`AmbiguousReserved` (a provider may have been reached);
        anything else non-returned → :class:`InvalidCheckpointState`."""

        row = self.db.execute(
            f"{_SELECT} WHERE family=? AND group_key=? AND unit_key=?",
            (family, group_key, unit_key),
        ).fetchone()
        return self._verified(
            row,
            action_kind=action_kind,
            identity=identity,
            expected_state="returned",
        )

    def complete(
        self,
        checkpoint_id: str,
        *,
        family: str,
        group_key: str,
        unit_key: str,
        action_kind: str,
        identity: str,
        payload: dict[str, Any],
        accrue: Callable[[dict[str, Any]], float] | None = None,
        run_id: int | None = None,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
    ) -> float:
        """Atomically move ``reserved`` → ``returned`` with the response durable.

        ``accrue`` is the caller's in-transaction accounting hook (fact +
        spend writes, ``commit=False`` style); it receives the verified
        checkpoint — including ``authorized_attempt_id``, which permanently
        owns the facts — and returns the newly-incurred cost.  When given,
        ``accounting_persisted`` is set so a later idempotent
        :meth:`account_returned` replays free."""

        encoded = canonical_json(payload)
        with self._txn():
            checkpoint = self._verified_unit(
                checkpoint_id,
                family=family,
                group_key=group_key,
                unit_key=unit_key,
                action_kind=action_kind,
                identity=identity,
                expected_state="reserved",
            )
            self._fence_writer(
                run_id=run_id,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=claimless_direct_effect,
                authorized_attempt_id=checkpoint["authorized_attempt_id"],
            )
            cost = 0.0 if accrue is None else float(accrue(checkpoint))
            updated = self.db.execute(
                "UPDATE effect_checkpoints SET state='returned', payload=?, "
                "accounting_persisted=? WHERE id=? AND state='reserved'",
                (encoded, 1 if accrue is not None else 0, checkpoint_id),
            ).rowcount
            if updated != 1:
                raise CheckpointLost(
                    "the effect checkpoint reservation was lost mid-complete"
                )
            return cost

    def account_returned(
        self,
        checkpoint_id: str,
        *,
        family: str,
        group_key: str,
        unit_key: str,
        action_kind: str,
        identity: str,
        payload: dict[str, Any],
        accrue: Callable[[dict[str, Any]], float],
        run_id: int | None = None,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
    ) -> float:
        """Idempotently persist a ``returned`` unit's accounting without consuming.

        The cancelled-after-return shape: facts and spend are historical truth
        even though no result may commit.  The first call accrues and rewrites
        the payload (the caller passes its zero-cost replay); replays return
        ``0.0`` without re-running ``accrue``."""

        encoded = canonical_json(payload)
        with self._txn():
            checkpoint = self._verified_unit(
                checkpoint_id,
                family=family,
                group_key=group_key,
                unit_key=unit_key,
                action_kind=action_kind,
                identity=identity,
                expected_state="returned",
            )
            self._fence_writer(
                run_id=run_id,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=claimless_direct_effect,
                authorized_attempt_id=checkpoint["authorized_attempt_id"],
            )
            if checkpoint["accounting_persisted"]:
                return 0.0
            cost = float(accrue(checkpoint))
            updated = self.db.execute(
                "UPDATE effect_checkpoints SET payload=?, accounting_persisted=1 "
                "WHERE id=? AND state='returned' AND accounting_persisted=0",
                (encoded, checkpoint_id),
            ).rowcount
            if updated != 1:
                raise CheckpointLost(
                    "the returned effect checkpoint was lost mid-accounting"
                )
            return cost

    def consume_and_retire(
        self,
        checkpoint_id: str,
        *,
        family: str,
        group_key: str,
        unit_key: str,
        action_kind: str,
        identity: str,
        finalize: Callable[[dict[str, Any]], None] | None = None,
        run_id: int | None = None,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
        commit: bool = True,
    ) -> None:
        """Atomically commit a ``returned`` unit's facts and delete its row.

        ``finalize`` is the caller's in-transaction consumption hook (result
        insertion under the checkpoint's ``authorized_attempt_id``).  The
        retire is the ``runs.py`` shape; the retained-forever plugin shape is
        :meth:`consume_group_retained` — which one a migrating family uses is
        an explicit review decision, not an implementing default.

        With ``commit=False`` the verify+finalize+delete joins the caller's
        surrounding transaction (a family whose result materialization is one
        ``BEGIN IMMEDIATE`` retires its checkpoints inside that same
        transaction, so the result can never commit while the replay
        authority survives, or vice versa).  The caller must already hold an
        open transaction."""

        if not commit:
            if not self.db.in_transaction:
                raise InvalidCheckpointState(
                    "consume_and_retire(commit=False) joins the caller's "
                    "transaction, but no transaction is open"
                )
            self._consume_and_retire_in_txn(
                checkpoint_id,
                family=family,
                group_key=group_key,
                unit_key=unit_key,
                action_kind=action_kind,
                identity=identity,
                finalize=finalize,
                run_id=run_id,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=claimless_direct_effect,
            )
            return
        with self._txn():
            self._consume_and_retire_in_txn(
                checkpoint_id,
                family=family,
                group_key=group_key,
                unit_key=unit_key,
                action_kind=action_kind,
                identity=identity,
                finalize=finalize,
                run_id=run_id,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=claimless_direct_effect,
            )

    def _consume_and_retire_in_txn(
        self,
        checkpoint_id: str,
        *,
        family: str,
        group_key: str,
        unit_key: str,
        action_kind: str,
        identity: str,
        finalize: Callable[[dict[str, Any]], None] | None,
        run_id: int | None,
        writer_attempt_id: str | None,
        claim_token: str | None,
        claimless_direct_effect: bool,
    ) -> None:
        checkpoint = self._verified_unit(
            checkpoint_id,
            family=family,
            group_key=group_key,
            unit_key=unit_key,
            action_kind=action_kind,
            identity=identity,
            expected_state="returned",
        )
        self._fence_writer(
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            claimless_direct_effect=claimless_direct_effect,
            authorized_attempt_id=checkpoint["authorized_attempt_id"],
        )
        if finalize is not None:
            finalize(checkpoint)
        deleted = self.db.execute(
            "DELETE FROM effect_checkpoints WHERE id=? AND state='returned'",
            (checkpoint_id,),
        ).rowcount
        if deleted != 1:
            raise CheckpointLost("the returned effect checkpoint was not retired")

    def consume_group_retained(
        self,
        *,
        family: str,
        group_key: str,
        action_kind: str,
        audit: Callable[[str | None], dict[str, Any]],
        run_id: int | None = None,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
        commit: bool = True,
    ) -> int:
        """Move a group's ``returned`` units to retained ``consumed`` rows.

        ``audit`` maps each unit's raw stored payload text to the bounded
        audit payload that replaces it (the plugin shape hashes the exact
        envelope bytes, so it receives the raw string).  ``reserved`` rows
        are deliberately untouched: they remain an explicit possible-effect
        reconciliation record.  With ``commit=False`` this joins the caller's
        surrounding transaction (e.g. a terminal receipt write)."""

        try:
            if commit:
                self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT id, payload, authorized_attempt_id FROM effect_checkpoints "
                "WHERE family=? AND group_key=? AND action_kind=? "
                "AND state='returned' ORDER BY unit_key, id",
                (family, group_key, action_kind),
            ).fetchall()
            for row in rows:
                self._fence_writer(
                    run_id=run_id,
                    writer_attempt_id=writer_attempt_id,
                    claim_token=claim_token,
                    claimless_direct_effect=claimless_direct_effect,
                    authorized_attempt_id=row["authorized_attempt_id"],
                )
            consumed = 0
            for row in rows:
                replacement = canonical_json(audit(row["payload"]))
                consumed += self.db.execute(
                    "UPDATE effect_checkpoints SET state='consumed', payload=? "
                    "WHERE id=? AND state='returned'",
                    (replacement, str(row["id"])),
                ).rowcount
            if commit:
                self.db.commit()
            return consumed
        except BaseException:
            if commit:
                self.db.rollback()
            raise

    def discard_reserved(
        self,
        checkpoint_id: str,
        *,
        family: str,
        group_key: str,
        unit_key: str,
        action_kind: str,
        identity: str,
        run_id: int | None = None,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
    ) -> bool:
        """Delete a reservation ONLY when the caller proves egress did not start.

        Never touches ``returned``/``consumed`` rows — a durable response or
        audit record is historical truth and cannot be discarded."""

        with self._txn():
            checkpoint = self.get(checkpoint_id)
            if (
                checkpoint is None
                or checkpoint["family"] != family
                or checkpoint["group_key"] != group_key
                or checkpoint["unit_key"] != unit_key
                or checkpoint["action_kind"] != action_kind
                or checkpoint["identity"] != identity
                or checkpoint["state"] != "reserved"
            ):
                # This operation was historically a conditional delete:
                # returned/consumed evidence and mismatched/missing rows are
                # deliberately untouched and report False. Preserve that
                # no-op contract while fencing only the row we may mutate.
                return False
            self._fence_writer(
                run_id=run_id,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=claimless_direct_effect,
                authorized_attempt_id=checkpoint["authorized_attempt_id"],
            )
            deleted = self.db.execute(
                "DELETE FROM effect_checkpoints WHERE id=? AND family=? "
                "AND group_key=? AND unit_key=? AND action_kind=? AND identity=? "
                "AND state='reserved'",
                (
                    checkpoint_id,
                    family,
                    group_key,
                    unit_key,
                    action_kind,
                    identity,
                ),
            ).rowcount
        return deleted == 1

    # -- operator decisions (frisket reconcile) ----------------------------

    def operator_discard(
        self, checkpoint_id: str, *, expected_state: str
    ) -> dict[str, Any]:
        """Operator decision: delete a stuck checkpoint so its unit is retryable.

        Unlike :meth:`discard_reserved` — an effect-site path that must prove
        egress never started — this is the named operator lever: a human has
        decided the provider effect did not happen, or accepts eating an
        unknowable cost, and clears the refusal so the unit can be bought
        fresh.  Discarding removes replay/refusal authority, never money
        truth (booked provider facts live in ``model_calls``/spend, not
        here).

        ``expected_state`` is REQUIRED and pins the DELETE: it is the state
        the operator's policy decision was based on, and the DELETE runs
        ``WHERE id=? AND state=?`` so a checkpoint that changed between the
        caller's read and this transaction (e.g. an in-flight ``complete()``
        landing a paid response) raises :class:`CheckpointLost` instead of
        silently deleting a row the operator never saw.

        ``consumed`` rows refuse (:class:`InvalidCheckpointState`): a retained
        audit payload is historical truth and cannot be discarded — EXCEPT
        this store's own operator attestation, which records a reversible
        human decision, not provider evidence.  Payload-level policy (e.g.
        refusing to discard a replayable returned success) belongs to the
        calling surface, which owns the family's payload semantics.

        Returns the deleted row so the caller can report what was decided.
        """

        with self._txn():
            row = self.db.execute(f"{_SELECT} WHERE id=?", (checkpoint_id,)).fetchone()
            if row is None:
                raise CheckpointLost(
                    "no effect checkpoint has this id; nothing to discard "
                    "(re-run `frisket reconcile list` for current ids)"
                )
            decoded = _decoded(row)
            assert decoded is not None
            if decoded["state"] == "consumed" and not is_operator_attestation(
                decoded["payload"]
            ):
                raise InvalidCheckpointState(
                    "this checkpoint is 'consumed' with a retained audit "
                    "payload; it is historical truth and cannot be discarded"
                )
            deleted = self.db.execute(
                "DELETE FROM effect_checkpoints WHERE id=? AND state=?",
                (checkpoint_id, expected_state),
            ).rowcount
            if deleted != 1:
                raise CheckpointLost(
                    "the checkpoint changed under this discard; re-run "
                    "`frisket reconcile list` and decide again"
                )
        return decoded

    def operator_accept_charged(self, checkpoint_id: str) -> dict[str, Any]:
        """Operator decision: a reserved unit's unknown provider effect DID
        happen and stays spent.

        ``reserved`` → ``consumed`` with an operator attestation payload.
        Nothing is fabricated: no provider response (so the unit can never
        replay as if a response existed), no result, and no cost figure (the
        true meter is unknown, so caps and receipts deliberately do not move).
        The consumed row is retained and every effect site refuses to call
        the provider again for this unit — the reservation's refusal becomes
        a recorded decision instead of an open question.

        Only ``reserved`` rows are decidable: a ``returned`` row's response
        is already durable (automatic resume consumes it without another
        call — there is no unknown charge to accept), and a ``consumed`` row
        is already decided or retained.
        """

        with self._txn():
            row = self.db.execute(f"{_SELECT} WHERE id=?", (checkpoint_id,)).fetchone()
            if row is None:
                raise CheckpointLost(
                    "no effect checkpoint has this id; nothing to accept "
                    "(re-run `frisket reconcile list` for current ids)"
                )
            state = str(row["state"])
            if state == "returned":
                raise InvalidCheckpointState(
                    "this effect's response is already durable; resuming the "
                    "run consumes it without another provider call, so there "
                    "is no unknown charge to accept"
                )
            if state != "reserved":
                raise InvalidCheckpointState(
                    f"this checkpoint is '{state}'; only an ambiguous "
                    "'reserved' unit has an unknown charge to accept"
                )
            attestation = canonical_json(
                {
                    "schema_version": OPERATOR_RESOLUTION_SCHEMA_VERSION,
                    "resolution": "accepted_charged",
                    "decided_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                }
            )
            updated = self.db.execute(
                "UPDATE effect_checkpoints SET state='consumed', payload=?, "
                "accounting_persisted=0 WHERE id=? AND state='reserved'",
                (attestation, checkpoint_id),
            ).rowcount
            if updated != 1:
                raise CheckpointLost(
                    "the reservation changed under this decision; re-run "
                    "`frisket reconcile list` and decide again"
                )
            after = self.db.execute(
                f"{_SELECT} WHERE id=?", (checkpoint_id,)
            ).fetchone()
        decoded = _decoded(after)
        assert decoded is not None
        return decoded

    # -- shared verification ----------------------------------------------

    def _fence_writer(
        self,
        *,
        run_id: int | None,
        writer_attempt_id: str | None,
        claim_token: str | None,
        claimless_direct_effect: bool,
        authorized_attempt_id: str | None,
    ) -> None:
        """Fence run-scoped checkpoint mutations in this transaction."""

        if authorized_attempt_id is None and run_id is None:
            return
        from frisket.execution.attempt import StaleAttemptWriter

        if authorized_attempt_id is None:
            raise StaleAttemptWriter(
                "a run-scoped checkpoint supplied no immutable authorized attempt"
            )
        if run_id is None:
            raise StaleAttemptWriter(
                "an attempt-owned checkpoint mutation supplied no run id"
            )
        owner = self.db.execute(
            "SELECT run_id FROM execution_attempts WHERE id=?",
            (authorized_attempt_id,),
        ).fetchone()
        if (
            owner is None
            or owner["run_id"] is None
            or int(owner["run_id"]) != int(run_id)
        ):
            owner_run = None if owner is None else owner["run_id"]
            raise StaleAttemptWriter(
                f"checkpoint fact owner {authorized_attempt_id!r} belongs to "
                f"run {owner_run!r}, not run {run_id}"
            )
        if writer_attempt_id is None:
            raise StaleAttemptWriter(
                "a run-scoped checkpoint mutation supplied no writer attempt"
            )
        from frisket.engine.store.output_claims import OutputColumnClaimStore

        OutputColumnClaimStore.require_current_writer(
            self.db,
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            claimless_direct_effect=claimless_direct_effect,
        )
        if claim_token is None:
            return
        from frisket.execution.attempt import STALE_DISPATCHING_AGE

        try:
            renewed = OutputColumnClaimStore.renew_in_transaction(
                self.db,
                claim_token=claim_token,
                run_id=run_id,
                lease_seconds=int(STALE_DISPATCHING_AGE.total_seconds()),
            )
        except sqlite3.OperationalError as exc:
            from frisket.engine.store.output_claims import (
                ClaimLeaseRenewalFailed,
            )

            raise ClaimLeaseRenewalFailed(str(exc)) from exc
        if renewed == 0:
            raise StaleAttemptWriter(
                f"claim token {claim_token!r} stopped being active during renewal"
            )

    def _verified_unit(
        self,
        checkpoint_id: str,
        *,
        family: str,
        group_key: str,
        unit_key: str,
        action_kind: str,
        identity: str,
        expected_state: str,
    ) -> dict[str, Any]:
        row = self.db.execute(
            f"{_SELECT} WHERE id=? AND family=? AND group_key=? AND unit_key=?",
            (checkpoint_id, family, group_key, unit_key),
        ).fetchone()
        return self._verified(
            row,
            action_kind=action_kind,
            identity=identity,
            expected_state=expected_state,
        )

    @staticmethod
    def _verified(
        row: Any,
        *,
        action_kind: str,
        identity: str,
        expected_state: str,
    ) -> dict[str, Any]:
        if row is None:
            raise CheckpointLost(
                "the effect checkpoint raced or disappeared; refusing another "
                "provider call until it is reconciled"
            )
        if str(row["action_kind"]) != action_kind or str(row["identity"]) != identity:
            raise IdentityMismatch(
                "the effect checkpoint was reserved for a different request or "
                "source value; refusing to substitute the current unit or call "
                "the provider again"
            )
        state = str(row["state"])
        if state != expected_state:
            if state == "reserved":
                raise AmbiguousReserved(
                    "a prior process may have reached the provider for this "
                    "unit, but no response was durably recorded; refusing "
                    "another call until the effect is reconciled"
                )
            raise InvalidCheckpointState(
                f"the effect checkpoint is '{state}' where this operation "
                f"requires '{expected_state}'; refusing another provider call "
                "until it is reconciled"
            )
        decoded = _decoded(row)
        assert decoded is not None
        return decoded

    @contextmanager
    def _txn(self):
        """BEGIN IMMEDIATE; commit on success, rollback on ANY exit."""

        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise


_SELECT = (
    "SELECT id, family, group_key, unit_key, action_kind, identity, "
    "authorized_attempt_id, state, payload, accounting_persisted, created_at "
    "FROM effect_checkpoints"
)


def _decoded(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    payload: Any = None
    if row["payload"] is not None:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            # Corruption stays visible as None, never as permission to
            # egress again — the effect-site consumer fails closed on it.
            payload = None
    return {
        "id": str(row["id"]),
        "family": str(row["family"]),
        "group_key": str(row["group_key"]),
        "unit_key": str(row["unit_key"]),
        "action_kind": str(row["action_kind"]),
        "identity": str(row["identity"]),
        "authorized_attempt_id": (
            None
            if row["authorized_attempt_id"] is None
            else str(row["authorized_attempt_id"])
        ),
        "state": str(row["state"]),
        "payload": payload,
        "accounting_persisted": bool(row["accounting_persisted"]),
        "created_at": str(row["created_at"]),
    }


__all__ = [
    "AmbiguousReserved",
    "CheckpointLost",
    "EffectCheckpointRefused",
    "EffectCheckpointStore",
    "IdentityMismatch",
    "InvalidCheckpointState",
    "ModelCallIdMissing",
    "OPERATOR_RESOLUTION_SCHEMA_VERSION",
    "canonical_json",
    "is_operator_attestation",
    "require_model_call_ids",
]
