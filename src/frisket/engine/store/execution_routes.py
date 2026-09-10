"""Execution route data layer.

``RouteStore`` is the ONLY sanctioned access path to the consents /
promise_sets / routes / binding_epochs / route_violations tables. Subject
identity is typed at the boundary — ``RouteStore.for_run(project, run_id:
int)`` / ``RouteStore.for_receipt(project, receipt_id: str)`` — so no call
site ever builds a raw subject string or CASTs an id. Subject ids are TEXT:
``str(runs.id)`` / ``receipts.id``.

Chain-write protocol (the CAS is a protocol, not just the UNIQUE constraint):
every chain append runs ``BEGIN IMMEDIATE`` (writer lock acquired BEFORE the
head is read), re-reads ``MAX(seq)``, requires the caller's intended
predecessor to equal that head, inserts at ``seq = head + 1``, and commits.
Lock waits are bounded (``_CHAIN_BUSY_TIMEOUT_MS``) with whole-transaction
retry (``_CHAIN_MAX_RETRIES``); a loser whose predecessor is no longer the
head gets ``ChainConflictError`` (re-read the head and re-append), except for
successor routes, where an equivalent existing successor is REUSED when one
matches on ``(predecessor_id, route_fact_hash)`` instead of appending an
identical serial successor.

Every append method also accepts ``txn=`` (an active connection whose
transaction the caller owns) so a consent-bound successor update can write
consent + promise set + route atomically with its other project-store writes,
and ``observe_binding_divergence`` operates on the fact writer's existing
transaction by contract. In caller-owned mode this module
never commits, rolls back, or begins a transaction.

Connections come from ``Project.db`` (src/frisket/engine/store/project.py),
which sets ``PRAGMA foreign_keys=ON`` (and WAL + busy_timeout) per
connection at open — the pragma belongs at connection open because it is
per-connection state and a no-op mid-transaction. This module still verifies
the pragma defensively on every caller-supplied connection and refuses ones
that have it off (it cannot be enabled inside an open transaction).

Hash contract: every content
hash here is produced by ``frisket.execution.promises.content_hash`` — the
ONE family-registry implementation; the store has no parallel local copy.
Rules per family are declared in
``promises.HASH_FAMILIES`` and pinned by contract tests.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from filelock import FileLock

from frisket.execution.promises import (
    DOMAIN_EPOCH_PROVENANCE,
    DOMAIN_ROUTE_FACTS,
    DOMAIN_ROUTE_VIOLATION,
    DOMAIN_STANDING_POLICY,
    Promise,
    canonical_json as _canonical_json,
    content_hash,
    decimal_string,
    promise_row_fingerprint,
    promise_rows_hash,
)
from frisket.execution.targets import validated_target_snapshot


def _quote_json(quote: Mapping[str, Any]) -> str:
    """Encode a persisted ``ConsentQuote`` record.

    NOT ``canonical_json``, which refuses floats because the material it
    encodes is HASHED and a float there would make two quantities that print
    differently hash differently. This record is not hashed: the consent's
    digest was minted from the pydantic dump at 402 time and is stored
    separately in ``promise_set_hash``. What this column must do is preserve
    the quote as it was shown — and the estimate's ``cost`` is a float USD by
    contract (``ConsentQuote.cost``), so refusing floats here would refuse to
    record the very number the user approved.

    ``allow_nan=False`` still holds: a non-finite cost has already been
    refused by name at the projection, and a JSON ``NaN`` in a durable money
    record would be unreadable by anything else.
    """
    return json.dumps(
        dict(quote), sort_keys=True, separators=(",", ":"), allow_nan=False
    )


# §5.1: the sub-$X no-gate UX as a recorded, revocable standing consent.
STANDING_COST_POLICY = "cost_under_gate_v1"

# The env knob that MINTS standing-consent thresholds (F2). It is the mint
# input, never the live coverage width: on project open,
# ``ensure_standing_cost_consent`` reconciles the persisted head standing
# consent against this value (a change mints a SUCCESSOR consent row — an
# auditable trail of policy changes); coverage (validation AND worker) reads
# the persisted head's recorded threshold, never this env var directly.
COST_CONSENT_ENV = "FRISKET_COST_CONSENT_USD"
_DEFAULT_COST_CONSENT_USD = Decimal(2)

# Installation principal (F5): identity lives OUTSIDE portable project
# bundles, in a small ``installation_id`` file at the workspace data root
# (``FRISKET_DATA_DIR`` when set, else the directory containing the bundle),
# minted once with uuid4. The bundle meta under this key is only a CACHE and
# is IGNORED when it disagrees — restoring a bundle onto another install
# yields the NEW install's principal, so foreign consents fail closed
# (§5.3 actor check) and standing consent is re-minted for this principal
# at open.
INSTANCE_PRINCIPAL_META_KEY = "instance_principal"
INSTALLATION_ID_FILENAME = "installation_id"
INSTALLATION_ID_LOCK_FILENAME = ".installation_id.lock"

# Chain-write bounds. Each owned append waits at most this long for the
# writer lock at BEGIN IMMEDIATE (deliberately below the connection's 10s
# default so chain contention surfaces as bounded retries, not one long
# stall), and the whole transaction retries at most this many times.
_CHAIN_BUSY_TIMEOUT_MS = 5_000
_CHAIN_MAX_RETRIES = 5
_CHAIN_RETRY_BACKOFF_S = 0.02

# The resolved fact columns on a route row, in hash order. These ARE the
# promised config-phase facts (§1.3: the top-level columns are RESOLVED
# facts, the source for every promised receipt field).
_ROUTE_FACT_FIELDS = (
    "cost_posture",
    "credential_source",
    "egress_class",
    "operator",
    "region",
)

# ``consents.grant_basis`` records why a per-action consent row exists. Both
# rows are equally authoritative — the fence's three-way equality (identity,
# set hash, actor) is what authorizes, not the basis — but the ledger must be
# able to say which grants a human answered and which the gate derived.
CONSENT_BASIS_CONFIRMED = "user_confirmation"
CONSENT_BASIS_EXACT_MATCH = "exact_match_derived"
CONSENT_BASIS_PREAPPROVED = "cost_preapproved"


class RouteStoreError(RuntimeError):
    """Base error for the execution-route data layer."""


class ChainConflictError(RouteStoreError):
    """The stated predecessor is no longer the chain head.

    The caller must re-read the head and decide again (its resolution may be
    stale); this is never retried blindly inside the store.
    """


class SubjectIntegrityError(RouteStoreError):
    """A route and its promise set would disagree on subject (§1.1: an
    application invariant enforced by the writer)."""


_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
    """Crockford-base32 ULID (48-bit ms timestamp + 80 bits randomness)."""
    value = (int(time.time() * 1000) & ((1 << 48) - 1)) << 80
    value |= int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_ULID_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_or_none(text: str | None) -> Any:
    return None if text is None else json.loads(text)


@dataclass(frozen=True)
class ConsentRow:
    id: str
    subject_kind: str | None
    subject_id: str | None
    action_identity_hash: str | None
    promise_set_hash: str | None
    standing_policy: str | None
    actor: str
    granted_at: str
    # F2: persisted parameters for standing policies — the recorded artifact
    # that makes standing authority reproducible from persisted state alone
    # ({"currency": "USD", "threshold_usd": "<decimal string>",
    # "policy_content_id": <frisket.standing_policy.v1 hash>}); None for
    # per-action consents.
    policy_params: dict[str, Any] | None = None
    # Why this per-action row exists — :data:`CONSENT_BASIS_CONFIRMED` or
    # :data:`CONSENT_BASIS_EXACT_MATCH`. None for standing consents (and for
    # rows written before the column existed).
    grant_basis: str | None = None
    # The dumped ``ConsentQuote`` this consent approved — what the user was
    # SHOWN — as the RAW column text. None for standing consents and for
    # routed consents (whose authority is the promise-set chain, not a quote
    # hash). Dispatch reads only its RATED half; see ``schema.py``'s column
    # comment.
    #
    # Deliberately NOT decoded here. Reading a consent row must never raise on
    # the CONTENT of this column: ``consents()`` is called by the dispatch
    # fence before its normalizing try block, so a truncated or corrupt blob
    # decoded eagerly would escape as a raw ``JSONDecodeError`` and become a
    # generic job retry instead of the modeled ``consent_missing``. The one
    # reader (``AttemptAuthority._proving_consent``) decodes inside its own
    # per-candidate guard, where "this row proves nothing" is a decided
    # outcome rather than a crash — and where one corrupt row cannot stop a
    # sibling consent from proving the dispatch.
    quote_json: str | None = None


@dataclass(frozen=True)
class PromiseSetRow:
    id: str
    subject_kind: str
    subject_id: str
    seq: int
    consent_id: str | None
    promises: list[dict[str, Any]]
    promise_set_hash: str
    created_at: str


@dataclass(frozen=True)
class RouteRow:
    id: str
    subject_kind: str
    subject_id: str
    seq: int
    predecessor_id: str | None
    promise_set_id: str
    engine: str
    options: dict[str, Any]
    target_snapshot: dict[str, Any]
    route_fact_hash: str
    operator: str
    egress_class: str
    region: str | None
    credential_source: str
    cost_posture: str
    created_at: str


@dataclass(frozen=True)
class BindingEpochRow:
    id: str
    route_id: str
    provenance_hash: str
    observed: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class RouteViolationRow:
    id: str
    route_id: str
    promise_set_id: str
    promise_fingerprint: str
    observed: dict[str, Any]
    dedupe_key: str
    created_at: str


def _consent_row(row: sqlite3.Row) -> ConsentRow:
    return ConsentRow(
        id=row["id"],
        subject_kind=row["subject_kind"],
        subject_id=row["subject_id"],
        action_identity_hash=row["action_identity_hash"],
        promise_set_hash=row["promise_set_hash"],
        standing_policy=row["standing_policy"],
        actor=row["actor"],
        granted_at=row["granted_at"],
        policy_params=_json_or_none(row["policy_params_json"]),
        grant_basis=row["grant_basis"],
        quote_json=row["quote_json"],
    )


def _promise_set_row(row: sqlite3.Row) -> PromiseSetRow:
    promises = json.loads(row["promises_json"])
    if not isinstance(promises, list):
        raise RouteStoreError("persisted promise set must be a row list")
    try:
        for promise in promises:
            if not isinstance(promise, Mapping):
                raise ValueError("promise rows must be objects")
            Promise.from_row(promise)
    except (KeyError, TypeError, ValueError) as exc:
        raise RouteStoreError(
            "persisted promise set does not match the current row schema"
        ) from exc
    computed_hash = promise_rows_hash(promises)
    if computed_hash != row["promise_set_hash"]:
        raise RouteStoreError(
            "persisted promise-set hash does not match its row material"
        )
    return PromiseSetRow(
        id=row["id"],
        subject_kind=row["subject_kind"],
        subject_id=row["subject_id"],
        seq=int(row["seq"]),
        consent_id=row["consent_id"],
        promises=promises,
        promise_set_hash=row["promise_set_hash"],
        created_at=row["created_at"],
    )


def _route_row(row: sqlite3.Row) -> RouteRow:
    return RouteRow(
        id=row["id"],
        subject_kind=row["subject_kind"],
        subject_id=row["subject_id"],
        seq=int(row["seq"]),
        predecessor_id=row["predecessor_id"],
        promise_set_id=row["promise_set_id"],
        engine=row["engine"],
        options=json.loads(row["options_json"]),
        target_snapshot=validated_target_snapshot(
            json.loads(row["target_snapshot_json"])
        ),
        route_fact_hash=row["route_fact_hash"],
        operator=row["operator"],
        egress_class=row["egress_class"],
        region=row["region"],
        credential_source=row["credential_source"],
        cost_posture=row["cost_posture"],
        created_at=row["created_at"],
    )


def _epoch_row(row: sqlite3.Row) -> BindingEpochRow:
    return BindingEpochRow(
        id=row["id"],
        route_id=row["route_id"],
        provenance_hash=row["provenance_hash"],
        observed=json.loads(row["observed_json"]),
        created_at=row["created_at"],
    )


def _violation_row(row: sqlite3.Row) -> RouteViolationRow:
    return RouteViolationRow(
        id=row["id"],
        route_id=row["route_id"],
        promise_set_id=row["promise_set_id"],
        promise_fingerprint=row["promise_fingerprint"],
        observed=json.loads(row["observed_json"]),
        dedupe_key=row["dedupe_key"],
        created_at=row["created_at"],
    )


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message


def _require_foreign_keys(db: sqlite3.Connection) -> None:
    """Defensive per-connection check (the pragma itself belongs at
    connection open — Project.db sets it — because it is per-connection and
    silently a no-op inside an open transaction)."""
    (enabled,) = db.execute("PRAGMA foreign_keys").fetchone()
    if int(enabled) == 1:
        return
    if db.in_transaction:
        raise RouteStoreError(
            "connection has foreign_keys OFF and an open transaction; the "
            "pragma cannot be enabled mid-transaction — open connections "
            "through Project.db"
        )
    db.execute("PRAGMA foreign_keys=ON")


def _run_bounded_immediate_write(
    db: sqlite3.Connection,
    write: Callable[[sqlite3.Connection], Any],
    *,
    operation: str,
) -> Any:
    """Run ``write`` under the store's bounded owned-writer protocol.

    The writer lock is acquired before ``write`` performs any head read.
    Lock contention retries the whole transaction, never just the failed
    statement, so every retry re-evaluates its conditional mutation against
    the newly committed state.
    """
    (previous_timeout,) = db.execute("PRAGMA busy_timeout").fetchone()
    last_busy: sqlite3.OperationalError | None = None
    try:
        db.execute(f"PRAGMA busy_timeout={_CHAIN_BUSY_TIMEOUT_MS}")
        for attempt in range(_CHAIN_MAX_RETRIES):
            if attempt:
                time.sleep(_CHAIN_RETRY_BACKOFF_S * attempt)
            try:
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if _is_busy_error(exc):
                    last_busy = exc
                    continue
                raise
            try:
                result = write(db)
                db.commit()
                return result
            except sqlite3.OperationalError as exc:
                db.rollback()
                if _is_busy_error(exc):
                    last_busy = exc
                    continue
                raise
            except BaseException:
                db.rollback()
                raise
        raise RouteStoreError(
            f"{operation} still lock-contended after {_CHAIN_MAX_RETRIES} "
            f"attempts ({_CHAIN_BUSY_TIMEOUT_MS}ms busy timeout each)"
        ) from last_busy
    finally:
        db.execute(f"PRAGMA busy_timeout={int(previous_timeout)}")


def route_fact_hash(
    *,
    operator: str,
    egress_class: str,
    region: str | None,
    credential_source: str,
    cost_posture: str,
) -> str:
    """Canonical hash of the resolved route fact columns (§1.1)."""
    return content_hash(
        DOMAIN_ROUTE_FACTS,
        {
            "cost_posture": cost_posture,
            "credential_source": credential_source,
            "egress_class": egress_class,
            "operator": operator,
            "region": region,
        },
    )


def promise_set_hash(promises: Sequence[Mapping[str, Any]]) -> str:
    """Content hash of a compiled promise row list — delegates to the ONE
    implementation (F3: ``promises.promise_rows_hash``; the store, the 402
    echo, and ``PromiseSet.set_hash`` all produce this digest)."""
    return promise_rows_hash(list(promises))


def promise_fingerprint(promise: Mapping[str, Any]) -> str:
    """Hash of the full promise row (field+op+value+basis+
    order_ref) — the ONE fingerprint construction (F9:
    ``promises.promise_row_fingerprint``, domain ``frisket.promise_row.v1``)."""
    return promise_row_fingerprint(promise)


def provenance_hash(observed: Mapping[str, Any]) -> str:
    """Identity for "distinct observed provenance"."""
    return content_hash(DOMAIN_EPOCH_PROVENANCE, dict(observed))


class RouteStore:
    """Typed, subject-scoped access to the execution-route tables.

    Construct via :meth:`for_run` / :meth:`for_receipt` only.
    """

    def __init__(self, project: Any, *, subject_kind: str, subject_id: str) -> None:
        self._project = project
        self._subject_kind = subject_kind
        self._subject_id = subject_id

    # ---------- typed accessors ----------

    @classmethod
    def for_run(cls, project: Any, run_id: int) -> "RouteStore":
        if isinstance(run_id, bool) or not isinstance(run_id, int):
            raise TypeError(f"run_id must be int, got {type(run_id).__name__}")
        return cls(project, subject_kind="run", subject_id=str(run_id))

    @classmethod
    def for_receipt(cls, project: Any, receipt_id: str) -> "RouteStore":
        if not isinstance(receipt_id, str) or not receipt_id:
            raise TypeError("receipt_id must be a non-empty str")
        return cls(project, subject_kind="receipt", subject_id=receipt_id)

    @property
    def subject(self) -> tuple[str, str]:
        return (self._subject_kind, self._subject_id)

    # ---------- connection / transaction plumbing ----------

    def _db(self) -> sqlite3.Connection:
        db = self._project.db
        _require_foreign_keys(db)
        return db

    def _run_chain_write(self, write, txn: sqlite3.Connection | None):
        """Run ``write(db)`` under the chain-write protocol.

        Caller-owned mode (``txn`` given, or the store connection already has
        an open transaction): run inside that transaction; the caller owns
        atomicity and commit — this is what lets a consent-bound update write
        consent, successor set, and successor route atomically.

        Owned mode: BEGIN IMMEDIATE (writer lock before the head read, so the
        in-transaction MAX(seq) can never be raced by another committer),
        bounded busy timeout, whole-transaction retry on lock contention.
        """
        if txn is not None:
            _require_foreign_keys(txn)
            return write(txn)
        db = self._db()
        if db.in_transaction:
            return write(db)
        return _run_bounded_immediate_write(
            db,
            write,
            operation="chain write",
        )

    # ---------- chain heads ----------

    def _head_promise_set(self, db: sqlite3.Connection) -> sqlite3.Row | None:
        return db.execute(
            "SELECT * FROM promise_sets WHERE subject_kind=? AND subject_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (self._subject_kind, self._subject_id),
        ).fetchone()

    def _head_route(self, db: sqlite3.Connection) -> sqlite3.Row | None:
        return db.execute(
            "SELECT * FROM routes WHERE subject_kind=? AND subject_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (self._subject_kind, self._subject_id),
        ).fetchone()

    def head(self) -> tuple[RouteRow, PromiseSetRow] | None:
        """The head (route, promise set) pair, always coherent (the
        successor set and successor route land in one transaction, so the
        head route's referenced set IS the head set)."""
        db = self._db()
        route = self._head_route(db)
        if route is None:
            return None
        promise_set = db.execute(
            "SELECT * FROM promise_sets WHERE id=?", (route["promise_set_id"],)
        ).fetchone()
        if promise_set is None:  # FK-impossible; decode defensively anyway
            raise RouteStoreError(
                f"route {route['id']} references missing promise set "
                f"{route['promise_set_id']}"
            )
        return _route_row(route), _promise_set_row(promise_set)

    # ---------- writers ----------

    def record_consent(
        self,
        *,
        action_identity_hash: str,
        promise_set_hash: str,
        actor: str,
        grant_basis: str = CONSENT_BASIS_CONFIRMED,
        granted_at: str | None = None,
        quote: Mapping[str, Any] | None = None,
        txn: sqlite3.Connection | None = None,
    ) -> ConsentRow:
        """Persist a per-action consent artifact for this subject.

        ``grant_basis`` records WHY (see the module constants): a user who
        was shown uncovered claims and confirmed them, or the validation
        gate's exact-match derivation from an identical already-consented
        action. Every subject that dispatches under a consent gets its OWN
        row, so the subject-scoped worker fence never has to reach across
        subjects to find one.

        ``quote`` is the dumped ``ConsentQuote`` the 402 hashed — what the
        user actually saw. The unrouted confirmation path supplies it and
        dispatch reads its rated half back; the routed/claims path passes
        ``None`` because its authority is the promise-set chain, which
        carries its own compiled cost claim.
        """

        def write(db: sqlite3.Connection) -> ConsentRow:
            consent = ConsentRow(
                id="consent_" + _ulid(),
                subject_kind=self._subject_kind,
                subject_id=self._subject_id,
                action_identity_hash=action_identity_hash,
                promise_set_hash=promise_set_hash,
                standing_policy=None,
                actor=actor,
                granted_at=granted_at or _now(),
                grant_basis=grant_basis,
                quote_json=None if quote is None else _quote_json(quote),
            )
            db.execute(
                "INSERT INTO consents (id, subject_kind, subject_id, "
                "action_identity_hash, promise_set_hash, standing_policy, "
                "actor, granted_at, grant_basis, quote_json) "
                "VALUES (?,?,?,?,?,NULL,?,?,?,?)",
                (
                    consent.id,
                    consent.subject_kind,
                    consent.subject_id,
                    consent.action_identity_hash,
                    consent.promise_set_hash,
                    consent.actor,
                    consent.granted_at,
                    consent.grant_basis,
                    consent.quote_json,
                ),
            )
            return consent

        return self._run_chain_write(write, txn)

    def append_promise_set(
        self,
        *,
        promises: Sequence[Mapping[str, Any]],
        predecessor_id: str | None,
        consent_id: str | None = None,
        txn: sqlite3.Connection | None = None,
    ) -> PromiseSetRow:
        """Append a promise set at the chain head.

        ``predecessor_id`` states the caller's intended head (``None`` = the
        chain must be empty); a mismatch is ``ChainConflictError`` — the
        caller re-reads the head and decides again.
        """
        promises_list = [dict(p) for p in promises]
        for promise in promises_list:
            Promise.from_row(promise)
        content_hash = promise_set_hash(promises_list)

        def write(db: sqlite3.Connection) -> PromiseSetRow:
            head = self._head_promise_set(db)
            head_id = head["id"] if head is not None else None
            if head_id != predecessor_id:
                raise ChainConflictError(
                    f"promise-set head for {self.subject} is {head_id!r}, "
                    f"not the stated predecessor {predecessor_id!r}"
                )
            row = PromiseSetRow(
                id="pset_" + _ulid(),
                subject_kind=self._subject_kind,
                subject_id=self._subject_id,
                seq=(int(head["seq"]) + 1) if head is not None else 1,
                consent_id=consent_id,
                promises=promises_list,
                promise_set_hash=content_hash,
                created_at=_now(),
            )
            try:
                db.execute(
                    "INSERT INTO promise_sets (id, subject_kind, subject_id, "
                    "seq, consent_id, promises_json, promise_set_hash, "
                    "created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        row.id,
                        row.subject_kind,
                        row.subject_id,
                        row.seq,
                        row.consent_id,
                        _canonical_json(row.promises),
                        row.promise_set_hash,
                        row.created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # seq UNIQUE: a caller-owned txn without the write lock lost
                # the CAS to a concurrent committer (owned mode holds the
                # IMMEDIATE lock, so this cannot happen there).
                raise ChainConflictError(
                    f"lost promise-set seq CAS for {self.subject} at seq {row.seq}"
                ) from exc
            return row

        return self._run_chain_write(write, txn)

    def append_route(
        self,
        *,
        promise_set_id: str,
        engine: str,
        options: Mapping[str, Any],
        target_snapshot: Mapping[str, Any],
        operator: str,
        egress_class: str,
        region: str | None,
        credential_source: str,
        cost_posture: str,
        predecessor_id: str | None,
        txn: sqlite3.Connection | None = None,
    ) -> RouteRow:
        """Append a route at the chain head (or reuse an equivalent successor).

        ``predecessor_id`` states the intended head route (``None`` = first
        route). Delegates to the ONE transaction-local CAS implementation
        (:func:`_append_route_cas`, F10) shared with the observation path.
        Successor reuse applies only when the
        equivalent existing successor references the SAME promise set —
        never across operations with different promise-set validity.
        """

        def write(db: sqlite3.Connection) -> RouteRow:
            return _append_route_cas(
                db,
                subject_kind=self._subject_kind,
                subject_id=self._subject_id,
                predecessor_id=predecessor_id,
                promise_set_id=promise_set_id,
                engine=engine,
                options=options,
                target_snapshot=target_snapshot,
                operator=operator,
                egress_class=egress_class,
                region=region,
                credential_source=credential_source,
                cost_posture=cost_posture,
            )

        return self._run_chain_write(write, txn)

    def record_violation(
        self,
        *,
        route_id: str,
        promise_set_id: str,
        promise: Mapping[str, Any],
        observed: Mapping[str, Any],
        txn: sqlite3.Connection | None = None,
    ) -> RouteViolationRow:
        """Record a promise violation, deduped on (subject, promise
        fingerprint, observed hash); an already-recorded violation is
        returned as-is (its ``status`` progression is never reset). The
        subject is in the key: a structurally identical violation on a
        different run is a different event and belongs in that run's
        ledger."""

        def write(db: sqlite3.Connection) -> RouteViolationRow:
            return _record_violation(
                db,
                subject_kind=self._subject_kind,
                subject_id=self._subject_id,
                route_id=route_id,
                promise_set_id=promise_set_id,
                promise=promise,
                observed=observed,
            )

        return self._run_chain_write(write, txn)

    # ---------- reads ----------

    def promise_sets(self) -> list[PromiseSetRow]:
        rows = (
            self._db()
            .execute(
                "SELECT * FROM promise_sets WHERE subject_kind=? AND subject_id=? "
                "ORDER BY seq",
                (self._subject_kind, self._subject_id),
            )
            .fetchall()
        )
        return [_promise_set_row(r) for r in rows]

    def consents(self) -> list[ConsentRow]:
        rows = (
            self._db()
            .execute(
                "SELECT * FROM consents WHERE subject_kind=? AND subject_id=? "
                "ORDER BY granted_at, id",
                (self._subject_kind, self._subject_id),
            )
            .fetchall()
        )
        return [_consent_row(r) for r in rows]

    def violations(self) -> list[RouteViolationRow]:
        """This subject's violation ledger, oldest first.

        Subject-scoped by the join through ``routes`` (a violation belongs to
        the subject whose route it was recorded against). A per-``route_id``
        filter lived here with zero callers — production and test — and was
        deleted; a caller wanting one route's rows filters the list."""
        db = self._db()
        rows = db.execute(
            "SELECT v.* FROM route_violations v JOIN routes r "
            "ON r.id = v.route_id WHERE r.subject_kind=? AND r.subject_id=? "
            "ORDER BY v.created_at, v.id",
            (self._subject_kind, self._subject_id),
        ).fetchall()
        return [_violation_row(r) for r in rows]


class ConsentRegistry:
    """INSTALLATION-scoped consent reads — the queries that have no subject.

    Both queries here ignore the subject entirely: their SQL never mentions
    ``subject_kind``/``subject_id``, because both ask an installation-wide
    question. They lived on :class:`RouteStore` anyway, so every caller had to
    invent a subject to reach them, and all three src seats invented the same
    one: ``RouteStore.for_run(project, 0)`` — a run that does not exist,
    written to satisfy a constructor whose scoping the method then discarded.
    A reader could not tell from the call whether run 0 mattered.

    So the two queries got the scope they actually have. ``RouteStore`` stays
    subject-scoped and every one of its methods now genuinely reads the
    subject; ``for_run(project, 0)`` has zero seats in ``src``.
    """

    def __init__(self, project: Any) -> None:
        self._project = project

    def _db(self) -> sqlite3.Connection:
        db = self._project.db
        _require_foreign_keys(db)
        return db

    def action_consents(self, action_identity_hash: str) -> list[ConsentRow]:
        """Per-action consents bound to ONE canonical action identity, across
        EVERY subject.

        The socket the exact-match gate plugs into ("socket
        restoration"): the worker's §5.3 exact match is subject-scoped
        because a resuming worker asks about ITS run, but the VALIDATION-time
        gate asks a different question — "has this installation already
        consented to this exact action with this exact claim set?" — whose
        answer necessarily spans subjects, since a re-run is a NEW run id.
        Actor filtering stays the caller's (F5: the coverage evaluator holds
        the current principal), exactly as with standing consents.
        """
        rows = (
            self._db()
            .execute(
                "SELECT * FROM consents WHERE action_identity_hash=? "
                "AND standing_policy IS NULL ORDER BY granted_at, id",
                (action_identity_hash,),
            )
            .fetchall()
        )
        return [_consent_row(r) for r in rows]

    def standing_consents(self) -> list[ConsentRow]:
        """Standing consents are installation-scoped (subject NULL), not
        subject-scoped; the one store coverage evaluation reads them from."""
        rows = (
            self._db()
            .execute(
                "SELECT * FROM consents WHERE standing_policy IS NOT NULL "
                "ORDER BY granted_at, id"
            )
            .fetchall()
        )
        return [_consent_row(r) for r in rows]


def route_row_by_id(db: sqlite3.Connection, route_id: str) -> RouteRow | None:
    """Load one route row by id on the CALLER's connection (lane F: the fact
    writer resolves the route a bound fact references inside its own store
    transaction before calling :func:`observe_binding_divergence`)."""
    row = db.execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
    return None if row is None else _route_row(row)


# ---------- shared low-level writers (usable under any transaction) ----------


def _append_route_cas(
    db: sqlite3.Connection,
    *,
    subject_kind: str,
    subject_id: str,
    predecessor_id: str | None,
    promise_set_id: str,
    engine: str,
    options: Mapping[str, Any],
    target_snapshot: Mapping[str, Any],
    operator: str,
    egress_class: str,
    region: str | None,
    credential_source: str,
    cost_posture: str,
    operation: str = "route",
) -> RouteRow:
    """THE transaction-local route append (F1/F7/F10): one CAS
    implementation for ``RouteStore.append_route``, both successor APIs, and
    ``observe_binding_divergence``. Runs entirely on the caller's
    connection/transaction; never commits.

    Protocol: same-subject promise-set validation → operation-specific
    promise-set validity → successor reuse ONLY within the same promise set
    → predecessor-is-current-head CAS → insert at head+1 → on a lost UNIQUE,
    reuse the equivalent same-set successor or surface ``ChainConflictError``.
    """
    target_snapshot = validated_target_snapshot(target_snapshot)
    subject = (subject_kind, subject_id)
    promise_subject = db.execute(
        "SELECT subject_kind, subject_id FROM promise_sets WHERE id=?",
        (promise_set_id,),
    ).fetchone()
    if promise_subject is None:
        raise SubjectIntegrityError(f"unknown promise set {promise_set_id!r}")
    if (promise_subject["subject_kind"], promise_subject["subject_id"]) != subject:
        raise SubjectIntegrityError(
            f"promise set {promise_set_id!r} belongs to "
            f"({promise_subject['subject_kind']!r}, "
            f"{promise_subject['subject_id']!r}), not {subject}"
        )
    predecessor_row = None
    if predecessor_id is not None:
        predecessor_row = db.execute(
            "SELECT * FROM routes WHERE id=?", (predecessor_id,)
        ).fetchone()
        if predecessor_row is None:
            raise SubjectIntegrityError(f"unknown predecessor route {predecessor_id!r}")
        if (
            predecessor_row["subject_kind"],
            predecessor_row["subject_id"],
        ) != subject:
            raise SubjectIntegrityError(
                f"predecessor route {predecessor_id!r} does not belong to {subject}"
            )
    # Operation-specific promise-set validity (F7): observation successors
    # keep the predecessor's set.
    if operation == "observation":
        if predecessor_row is None:
            raise RouteStoreError("observation successors require a predecessor")
        if predecessor_row["promise_set_id"] != promise_set_id:
            raise SubjectIntegrityError(
                "observation successor must keep the predecessor's promise "
                f"set ({predecessor_row['promise_set_id']!r}), got "
                f"{promise_set_id!r}"
            )

    fact_hash = route_fact_hash(
        operator=operator,
        egress_class=egress_class,
        region=region,
        credential_source=credential_source,
        cost_posture=cost_posture,
    )

    def _reusable_successor() -> RouteRow | None:
        """An equivalent existing successor is reused ONLY when it points at
        the SAME promise set (F7) — a same-facts successor under a different
        set is a conflict the caller must re-evaluate, never a silent
        substitution of consent."""
        if predecessor_id is None:
            return None
        existing = db.execute(
            "SELECT * FROM routes WHERE predecessor_id=? AND route_fact_hash=?",
            (predecessor_id, fact_hash),
        ).fetchone()
        if existing is None:
            return None
        if existing["promise_set_id"] != promise_set_id:
            raise ChainConflictError(
                f"an equivalent successor of {predecessor_id!r} already "
                f"exists under promise set {existing['promise_set_id']!r}, "
                f"not {promise_set_id!r}; re-read the head and re-evaluate"
            )
        return _route_row(existing)

    reusable = _reusable_successor()
    if reusable is not None:
        return reusable
    head = db.execute(
        "SELECT * FROM routes WHERE subject_kind=? AND subject_id=? "
        "ORDER BY seq DESC LIMIT 1",
        (subject_kind, subject_id),
    ).fetchone()
    head_id = head["id"] if head is not None else None
    if head_id != predecessor_id:
        raise ChainConflictError(
            f"route head for {subject} is {head_id!r}, not the stated "
            f"predecessor {predecessor_id!r}"
        )
    row = RouteRow(
        id="route_" + _ulid(),
        subject_kind=subject_kind,
        subject_id=subject_id,
        seq=(int(head["seq"]) + 1) if head is not None else 1,
        predecessor_id=predecessor_id,
        promise_set_id=promise_set_id,
        engine=engine,
        options=dict(options),
        target_snapshot=validated_target_snapshot(target_snapshot),
        route_fact_hash=fact_hash,
        operator=operator,
        egress_class=egress_class,
        region=region,
        credential_source=credential_source,
        cost_posture=cost_posture,
        created_at=_now(),
    )
    try:
        db.execute(
            "INSERT INTO routes (id, subject_kind, subject_id, seq, "
            "predecessor_id, promise_set_id, engine, options_json, "
            "target_snapshot_json, route_fact_hash, operator, "
            "egress_class, region, credential_source, cost_posture, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.id,
                row.subject_kind,
                row.subject_id,
                row.seq,
                row.predecessor_id,
                row.promise_set_id,
                row.engine,
                _canonical_json(row.options),
                _canonical_json(row.target_snapshot),
                row.route_fact_hash,
                row.operator,
                row.egress_class,
                row.region,
                row.credential_source,
                row.cost_posture,
                row.created_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        # (predecessor_id, route_fact_hash) or seq UNIQUE lost in a
        # caller-owned txn: reuse the equivalent same-set successor when one
        # exists, else surface the CAS loss.
        reusable = _reusable_successor()
        if reusable is not None:
            return reusable
        raise ChainConflictError(
            f"lost route seq CAS for {subject} at seq {row.seq}"
        ) from exc
    return row


def _open_epoch(
    db: sqlite3.Connection, *, route_id: str, observed: Mapping[str, Any]
) -> BindingEpochRow:
    observed_dict = dict(observed)
    content_hash = provenance_hash(observed_dict)
    existing = db.execute(
        "SELECT * FROM binding_epochs WHERE route_id=? AND provenance_hash=?",
        (route_id, content_hash),
    ).fetchone()
    if existing is not None:
        return _epoch_row(existing)
    row = BindingEpochRow(
        id="epoch_" + _ulid(),
        route_id=route_id,
        provenance_hash=content_hash,
        observed=observed_dict,
        created_at=_now(),
    )
    try:
        db.execute(
            "INSERT INTO binding_epochs (id, route_id, provenance_hash, "
            "observed_json, created_at) VALUES (?,?,?,?,?)",
            (
                row.id,
                row.route_id,
                row.provenance_hash,
                _canonical_json(row.observed),
                row.created_at,
            ),
        )
    except sqlite3.IntegrityError:
        # Lazy epoch creation raced; the winner's row is ours.
        existing = db.execute(
            "SELECT * FROM binding_epochs WHERE route_id=? AND provenance_hash=?",
            (route_id, content_hash),
        ).fetchone()
        if existing is None:
            raise
        return _epoch_row(existing)
    return row


def _record_violation(
    db: sqlite3.Connection,
    *,
    subject_kind: str,
    subject_id: str,
    route_id: str,
    promise_set_id: str,
    promise: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> RouteViolationRow:
    """Record one violation, deduped WITHIN its subject.

    The dedupe key used to be (promise fingerprint, observed
    hash) alone, and ``dedupe_key`` is UNIQUE across the whole table — so
    run B's structurally identical violation (same promise, same observed
    facts, a genuinely separate event on a separate run) silently returned
    run A's row and NEVER LANDED IN B's LEDGER. ``violations()`` joins
    through ``routes``, so B's ledger simply read empty: a truth-recording
    surface that lost truth. The subject is part of the identity of the
    event, so it is part of the key.
    """
    observed_dict = dict(observed)
    fingerprint = promise_fingerprint(promise)
    dedupe_key = content_hash(
        DOMAIN_ROUTE_VIOLATION,
        {
            "promise_fingerprint": fingerprint,
            "observed_hash": provenance_hash(observed_dict),
            "subject_kind": subject_kind,
            "subject_id": subject_id,
        },
    )
    existing = db.execute(
        "SELECT * FROM route_violations WHERE dedupe_key=?", (dedupe_key,)
    ).fetchone()
    if existing is not None:
        return _violation_row(existing)
    row = RouteViolationRow(
        id="viol_" + _ulid(),
        route_id=route_id,
        promise_set_id=promise_set_id,
        promise_fingerprint=fingerprint,
        observed=observed_dict,
        dedupe_key=dedupe_key,
        created_at=_now(),
    )
    try:
        db.execute(
            "INSERT INTO route_violations (id, route_id, promise_set_id, "
            "promise_fingerprint, observed_json, dedupe_key, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (
                row.id,
                row.route_id,
                row.promise_set_id,
                row.promise_fingerprint,
                _canonical_json(row.observed),
                row.dedupe_key,
                row.created_at,
            ),
        )
    except sqlite3.IntegrityError:
        existing = db.execute(
            "SELECT * FROM route_violations WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        if existing is None:
            raise
        return _violation_row(existing)
    return row


# ---------- observation phase (§6) ----------


def observe_binding_divergence(
    txn: sqlite3.Connection, route: RouteRow, observed: Mapping[str, Any]
) -> tuple[str, str]:
    """Record observed binding truth; return ``(route_id, epoch_id)``.

    Operates on the caller's transaction/connection; it never opens
    its own connection or commits — no lock contention with the run's own
    writes, no partial commits. With credentials pinned, divergence
    here is a recorded defect, not sanctioned behavior.

    Stale-observer protocol (F1): the subject's route head is RE-READ under
    the caller's transaction; ``route`` merely names the chain, it is never
    trusted as the head. Divergence is evaluated against the CURRENT head:

    - Observed config facts (the ``_ROUTE_FACT_FIELDS`` subset of
      ``observed``) match the current head: reuse — the epoch opens under
      the head, no append. A newer consent-bound head cannot be displaced by
      a delayed observer still holding its predecessor.
    - They diverge from the current head: append a SUCCESSOR of the CURRENT
      head via the shared CAS (:func:`_append_route_cas`,
      ``operation="observation"`` — same promise set, F7/F10), open an
      epoch under it, and record a dedupe-keyed violation for every matching
      compiled promise. This post-effect observation writer never halts;
      dispatch authorization belongs to the earlier admission evaluation.

    Observation-only facts (revision/device/dtype/timings) in ``observed``
    never diverge a route — they are recorded on the epoch, not promised.
    """
    _require_foreign_keys(txn)
    observed_dict = dict(observed)
    head_row = txn.execute(
        "SELECT * FROM routes WHERE subject_kind=? AND subject_id=? "
        "ORDER BY seq DESC LIMIT 1",
        (route.subject_kind, route.subject_id),
    ).fetchone()
    if head_row is None:  # the caller's route row implies a chain exists
        raise RouteStoreError(
            f"no route chain for subject ({route.subject_kind!r}, "
            f"{route.subject_id!r}); cannot record binding observation"
        )
    head = _route_row(head_row)
    diverged = {
        field: observed_dict[field]
        for field in _ROUTE_FACT_FIELDS
        if field in observed_dict and observed_dict[field] != getattr(head, field)
    }
    if not diverged:
        epoch = _open_epoch(txn, route_id=head.id, observed=observed_dict)
        return head.id, epoch.id

    successor_facts = {
        field: observed_dict.get(field, getattr(head, field))
        for field in _ROUTE_FACT_FIELDS
    }
    successor = _append_route_cas(
        txn,
        subject_kind=head.subject_kind,
        subject_id=head.subject_id,
        predecessor_id=head.id,
        promise_set_id=head.promise_set_id,
        engine=head.engine,
        options=head.options,
        target_snapshot=head.target_snapshot,
        operator=successor_facts["operator"],
        egress_class=successor_facts["egress_class"],
        region=successor_facts["region"],
        credential_source=successor_facts["credential_source"],
        cost_posture=successor_facts["cost_posture"],
        operation="observation",
    )
    epoch = _open_epoch(txn, route_id=successor.id, observed=observed_dict)

    promise_set = txn.execute(
        "SELECT promises_json FROM promise_sets WHERE id=?",
        (head.promise_set_id,),
    ).fetchone()
    promises = json.loads(promise_set["promises_json"]) if promise_set else []
    for field in sorted(diverged):
        # ONLY a compiled promise row can be violated. A diverged fact that
        # no promise covers is still recorded — the successor route above
        # carries the actual value and the epoch carries the whole observed
        # payload — but it does not get a ``route_violations`` row, because
        # there is no claim to have broken. This branch used to SYNTHESIZE an
        # implicit eq-promise from the route row's own fact column;
        # deleted it with the ``credential_source`` promise it existed for
        # (§3.5). A fabricated promise row fingerprints against nothing a
        # reader can find in ``promise_sets``, which is a ledger entry naming
        # a claim the user never made.
        matching = [
            p for p in promises if isinstance(p, Mapping) and p.get("field") == field
        ]
        for promise in matching:
            _record_violation(
                txn,
                subject_kind=head.subject_kind,
                subject_id=head.subject_id,
                route_id=head.id,
                promise_set_id=head.promise_set_id,
                promise=promise,
                observed=observed_dict,
            )
    return successor.id, epoch.id


# ---------- installation principal (F5) + standing consent (§5.1/F2) ----------


def _installation_root(project: Any) -> Path:
    """Where installation-level app state lives: ``FRISKET_DATA_DIR`` when
    set (the hosted/team data root), else the directory containing the
    project bundle (the workspace root in the flat layouts). Deliberately
    OUTSIDE the portable bundle: a restored bundle lands under the restoring
    install's root and therefore under ITS principal."""
    data_dir = os.environ.get("FRISKET_DATA_DIR")
    if data_dir and Path(data_dir).is_dir():
        # Only an EXISTING data root counts: a configured-but-absent path is
        # not this process's app-state root, and identity minting must never
        # conjure directories out of ambient env (the queue-posture tests pin
        # that no code path materializes a decoy FRISKET_DATA_DIR).
        return Path(data_dir)
    return Path(project.path).parent


def _read_or_mint_installation_id(root: Path) -> str:
    """Read (or mint once, uuid4) the ``installation_id`` file at ``root``.
    Mint is crash/race-safe across processes: one FileLock holder re-checks
    the final path, writes a unique sibling scratch file, then atomically
    renames it into place. A killed holder can leave only an ignored scratch
    file, never an empty final identity; the next holder mints normally."""
    path = root / INSTALLATION_ID_FILENAME
    try:
        text = path.read_text().strip()
        if text:
            return text
    except OSError:
        pass
    lock = FileLock(str(root / INSTALLATION_ID_LOCK_FILENAME), timeout=-1)
    with lock:
        # The first read is only a fast path. Re-read while holding the
        # cross-process lock so every loser observes the published winner.
        try:
            text = path.read_text().strip()
            if text:
                return text
        except FileNotFoundError:
            pass
        minted = str(uuid.uuid4())
        scratch = root / f".{INSTALLATION_ID_FILENAME}.{uuid.uuid4().hex}.tmp"
        try:
            scratch.write_text(minted + "\n", encoding="utf-8")
            os.replace(scratch, path)
        finally:
            # Normal failures do not accumulate scratch files. SIGKILL can
            # leave one behind, but unique names make it inert to successors.
            scratch.unlink(missing_ok=True)
        return minted


def instance_principal(project: Any) -> str:
    """The stable installation principal (``deployment:<installation-id>``).

    F5: identity is minted OUTSIDE portable bundles (the workspace data
    root's ``installation_id`` file) and merely CACHED in bundle meta — the
    cache is ignored (and refreshed) when it disagrees, so a bundle restored
    onto another install yields the NEW install's principal, foreign
    consents fail closed, and standing consent is re-minted at open.
    """
    principal = (
        f"deployment:{_read_or_mint_installation_id(_installation_root(project))}"
    )
    cached = project.get_meta(INSTANCE_PRINCIPAL_META_KEY)
    if cached != principal:
        project.set_meta(
            INSTANCE_PRINCIPAL_META_KEY,
            principal,
            commit=not project.db.in_transaction,
        )
    return principal


def standing_cost_threshold_env() -> Decimal:
    """The MINT input for the standing cost consent (F2): the
    ``FRISKET_COST_CONSENT_USD`` knob (default 1; 0 = always-confirm).
    Coverage NEVER reads this directly — it reads the persisted head
    standing consent's recorded threshold; this value only feeds
    :func:`ensure_standing_cost_consent`'s reconcile-at-open mint and the
    legacy (non-resolution) cost gates.

    Fail-closed cases: ``NaN`` / ``Infinity`` / ``-Infinity`` parse
    cleanly as Decimals but state no threshold, and the old code handed
    them the $1 DEFAULT — i.e. a garbage (or hostile) env value silently
    minted a dollar of standing coverage. A non-finite knob now mints
    ``0`` (always-confirm), as does a negative one. Only an ABSENT or
    unparseable knob falls back to the default: absence means "the operator
    did not configure this", which is what the default is for.
    """
    raw = os.environ.get(COST_CONSENT_ENV)
    if raw is None:
        return _DEFAULT_COST_CONSENT_USD
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return _DEFAULT_COST_CONSENT_USD
    if not value.is_finite() or value < 0:
        return Decimal(0)
    return value


def _standing_policy_params(threshold: Decimal) -> dict[str, Any]:
    """The persisted policy parameters for one standing-consent generation:
    threshold + currency plus the policy content identity (F2)."""
    params: dict[str, Any] = {
        "currency": "USD",
        "threshold_usd": decimal_string(threshold),
    }
    params["policy_content_id"] = content_hash(
        DOMAIN_STANDING_POLICY, {**params, "policy": STANDING_COST_POLICY}
    )
    return params


def head_standing_cost_consent(
    consents: Sequence[ConsentRow], *, principal: str
) -> ConsentRow | None:
    """The HEAD standing cost consent for this installation principal: the
    most recently granted ``cost_under_gate_v1`` row whose actor is the
    CURRENT principal (F5: foreign consents from a restored bundle never
    count — fail closed → re-consent/re-mint)."""
    matching = [
        consent
        for consent in consents
        if consent.standing_policy == STANDING_COST_POLICY
        and consent.actor == principal
    ]
    if not matching:
        return None
    return max(matching, key=lambda consent: (consent.granted_at, consent.id))


def standing_cost_threshold(
    consents: Sequence[ConsentRow], *, principal: str
) -> Decimal | None:
    """The PERSISTED standing cost threshold for this principal, from the
    head standing consent's recorded parameters (F2: coverage — validation
    AND worker — reads this artifact, never the env knob). ``None`` (no
    row for this principal, or unreproducible parameters) = no standing
    coverage, fail closed."""
    head = head_standing_cost_consent(consents, principal=principal)
    if head is None or not isinstance(head.policy_params, Mapping):
        return None
    raw = head.policy_params.get("threshold_usd")
    if not isinstance(raw, str):
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    return value if value.is_finite() and value >= 0 else None


def ensure_standing_cost_consent(project: Any) -> ConsentRow:
    """Mint/reconcile the ``cost_under_gate_v1`` standing consent (§5.1/F2).

    Called at project open (the route-table migration path). The env knob is
    the MINT input: when no standing consent exists for the CURRENT
    installation principal, or the persisted head's recorded threshold
    differs from the knob, a SUCCESSOR standing consent is minted (actor =
    the installation principal, parameters persisted in
    ``policy_params_json``). Prior rows are never touched — they remain the
    auditable trail of policy changes. Coverage reads the persisted head
    only. The head read and conditional insert share the store's bounded
    ``BEGIN IMMEDIATE`` envelope, so independent project-open processes
    serialize before either can decide whether a successor is needed.

    Refuses inside a caller-owned transaction: this function owns and
    commits its transaction, which inside someone else's open transaction
    would commit THEIR partial writes as a side effect of reconciling a
    policy row. It runs at project open, where the connection is idle;
    anywhere else that is a programming error, not a thing to paper over
    with a savepoint.
    """
    db = project.db
    if db.in_transaction:
        raise RouteStoreError(
            "ensure_standing_cost_consent runs at project open on an idle "
            "connection; it commits, and committing inside a caller-owned "
            "transaction would durably commit that caller's partial writes"
        )
    principal = instance_principal(project)
    threshold = standing_cost_threshold_env()

    def reconcile(txn: sqlite3.Connection) -> ConsentRow:
        rows = txn.execute(
            "SELECT * FROM consents WHERE standing_policy=? ORDER BY granted_at, id",
            (STANDING_COST_POLICY,),
        ).fetchall()
        consents = [_consent_row(row) for row in rows]
        head = head_standing_cost_consent(consents, principal=principal)
        if (
            head is not None
            and standing_cost_threshold(consents, principal=principal) == threshold
        ):
            return head
        minted = ConsentRow(
            id="consent_" + _ulid(),
            subject_kind=None,
            subject_id=None,
            action_identity_hash=None,
            promise_set_hash=None,
            standing_policy=STANDING_COST_POLICY,
            actor=principal,
            granted_at=_now(),
            policy_params=_standing_policy_params(threshold),
        )
        txn.execute(
            "INSERT INTO consents (id, subject_kind, subject_id, "
            "action_identity_hash, promise_set_hash, standing_policy, actor, "
            "granted_at, policy_params_json) "
            "VALUES (?,NULL,NULL,NULL,NULL,?,?,?,?)",
            (
                minted.id,
                STANDING_COST_POLICY,
                minted.actor,
                minted.granted_at,
                _canonical_json(minted.policy_params),
            ),
        )
        return minted

    return _run_bounded_immediate_write(
        db,
        reconcile,
        operation="standing consent reconciliation",
    )
