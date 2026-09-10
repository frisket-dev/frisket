"""The ``ExecutionAttempt`` commitment.

ADMISSION, SCOPE and the invocation remainder were three faces of one unnamed
thing: **the attempt** — a single authorized dispatch of a run or receipt over a
single row set under a single verified route head. Before this module the join
existed by hand (a ``bound_route_id`` threaded through three signatures purely
to reconcile two head reads); now it is an object.

Two halves live here:

- :class:`AttemptCommitment` — the frozen, immutable authorization core. It
  carries **no** ``state``: state lives in the ``execution_attempts`` row and
  is read there. ``ExecutionAttempt`` names the persisted row; this dataclass
  is a cache of it.
- the row's I/O, including the **owned claim transaction** (:func:`claim`),
  which is the linearization point between dispatch and reconsent.

**The opaque-handle rule.** Consent-critical consumption loads the
PERSISTED commitment. The dispatch decision in :func:`claim` is made against
the row inside the write lock, never against the passed object's fields. This
is construction discipline, not security — a single-process laptop app cannot
rest consent on the authenticity of a dataclass. Two things follow and only
two: *type, not shape* (consumption sites take an ``AttemptCommitment``; a
dict cannot pose as one) and the construction-closure test in
``tests/execution/test_execution_attempt.py``, which asserts the
``src`` modules naming ``AttemptCommitment(`` are exactly this one and
``frisket.execution.attempt_authority``.

``execution_attempts`` gets no mutable per-row progress or result columns:
completed rows stay owned by ``results``/``run_rows``.  The narrow
``attempt_row_authorizations`` child is different: it freezes a
consent-bound retail quote allocation, then records the one terminal billing
classification produced by that exact attempt.  It is append-only evidence,
not a second runner state machine.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Union

from frisket.execution.promise_compiler import (
    CostBasis,
    OperatorBorneZeroCost,
    PricedCostBasis,
    UnpriceableCost,
)
from frisket.execution.promises import Promise, SetEvaluation
from frisket.execution.resolver import CandidateBinding

# ---------------------------------------------------------------------------
# The value
# ---------------------------------------------------------------------------

#: ``OpContext.extras`` key carrying the attempt for the one invocation. It
#: Replaces the four route-binding extras (``resolved_route``,
#: ``route_binding``, ``execution_binding``, and the ``RouteBinding`` pair
#: type): the attempt IS the typed variant, so a routed
#: run's extras carry exactly one key and a route can never be in scope
#: without its binding.
ATTEMPT_EXTRA = "execution_attempt"

ATTEMPT_STATES = (
    "created",
    "admitted",
    "dispatching",
    "effected",
    "halted",
    "superseded",
    "abandoned",
)

#: The states in which an attempt authorizes nothing further. Reaching one is
#: what releases ``runs.current_attempt_id`` (:func:`set_attempt_state`) and
#: what frees the run for a next claim under the partial unique index.
TERMINAL_ATTEMPT_STATES = ("effected", "halted", "superseded", "abandoned")

#: A ``dispatching`` attempt whose output-claim lease has gone silent for this
#: long is presumed crashed and closed as ``abandoned`` (§1.5's bounded-age
#: detector). Recovery closes it explicitly BEFORE a resume is admitted, so a
#: crash cannot leave one live forever and block the run permanently.
#:
#: This is the one liveness bound. Output-producing work renews the existing
#: ``output_column_claims`` lease in each result transaction. A claimless
#: direct effect has no output lease to renew, so its existing age-only bound
#: remains the honest (and deliberately narrower) fallback.
STALE_DISPATCHING_AGE = timedelta(hours=6)


@dataclass(frozen=True)
class UnroutedAdmission:
    """The recipe does not consume resolution, so there is no head to pin.
    A sum-type member, never ``None``: "unrouted" is a decided answer."""

    reason: Literal["recipe_does_not_consume_resolution"] = (
        "recipe_does_not_consume_resolution"
    )
    # A legacy/unrouted action can still have crossed a real 402 confirmation
    # gate.  It has no route head, but the consent that authorized it remains
    # a first-class receipt fact rather than disappearing with that absence.
    admitted_by_consent_id: str | None = None


@dataclass(frozen=True)
class RoutedAdmission:
    """A verified route head, read ONCE and keyed by ID.

    The route chain and the promise-set chain are independent counters; no
    sequence number is ever compared across them, which is why both heads are
    pinned by id rather than by seq.

    ``admitted_by_consent_id`` names the consent row that authorized this
    dispatch when one exists — an exact match on (set hash, action identity,
    actor). It is ``None`` for the admissions that are not a single row:
    standing-consent and coverage-envelope admissions, and sets carrying no
    user claims at all. ``grant_basis`` on the named row is what the pointer
    MEANS (user-confirmed vs derived from an identical consented action) and
    is the receipt's first reader of that column (B6).

    ``promise_set`` is the exact persisted promise-set row that admission
    evaluated.
    """

    head_route_id: str
    head_promise_set_id: str
    route: Any  # RouteRow — store type, imported lazily to keep this leaf light
    promise_set: Any  # PromiseSetRow
    binding: CandidateBinding
    evaluation: SetEvaluation
    admitted_by_consent_id: str | None
    # Exact hashes/revision refs observed by the work-scope evaluation that
    # admitted this invocation. It is intentionally transient: receipts keep
    # the bounded aggregate identity, while dispatch needs the per-cell
    # material solely to prove that the value it is about to use is the value
    # admission checked.
    work_scope_snapshot: dict[str, Any] | None = None


Admission = Union[RoutedAdmission, UnroutedAdmission]


@dataclass(frozen=True)
class AttemptCommitment:
    """The immutable authorization core of one dispatch.

    Immutable after admission except for ``state``, which is not on this
    value at all. Anything that must change — head, consent, identity, scope,
    cost basis, pinned terms — means a NEW attempt or a reconsent. There is no
    component-level refresh and no staleness matrix.
    """

    attempt_id: str
    run_id: int | None
    seq: int
    identity: str
    scope: tuple[int, ...]
    admission: Admission
    cost_basis: CostBasis
    # Existing DB/wire spelling retained for compatibility.  It now carries
    # the cost basis's opaque commercial terms version, and is None for
    # provider-direct or free work.
    price_card_version: str | None
    receipt_id: str | None = None

    def __post_init__(self) -> None:
        _attempt_owner(self.run_id, self.receipt_id)

    @property
    def routed(self) -> RoutedAdmission | None:
        admission = self.admission
        return admission if isinstance(admission, RoutedAdmission) else None


class AttemptClaimRefused(RuntimeError):
    """The claim CAS lost: the row was not ``admitted`` when the write lock
    was taken, or a head moved under it (a reconsent landed in the window).
    Nothing dispatched. The caller maps this to the durable, reconsent-shaped
    refusal so the retry binds and verifies ONE head."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StaleAttemptWriter(RuntimeError):
    """A post-dispatch writer no longer owns the run it is trying to mutate.

    This is deliberately distinct from :class:`AttemptClaimRefused`: the
    provider effect may already have happened, so callers must stop only this
    invocation and must not advertise a safe retry or terminalize the shared
    run/claim now owned by a replacement attempt.
    """

    code = "stale_attempt_writer"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


# ---------------------------------------------------------------------------
# extras accessors — typed replacement for ``require_route_binding``
# ---------------------------------------------------------------------------


def attempt_in_scope(extras: Any) -> AttemptCommitment | None:
    """The attempt authorizing this invocation, or ``None`` for an
    unaccounted invocation. Type, not shape: a dict cannot pose as a commitment."""
    value = (extras or {}).get(ATTEMPT_EXTRA)
    return value if isinstance(value, AttemptCommitment) else None


def routed_admission_in_scope(extras: Any) -> RoutedAdmission | None:
    """The routed admission in scope, or ``None``.

    ``require_route_binding`` existed to make
    "a route present without its binding" a loud error, and the sum type
    makes that state unrepresentable — a ``RoutedAdmission`` carries both or
    does not exist. Callers that need the connection material read
    ``.binding.connection``; callers that need the pinned facts read
    ``.route``.
    """
    attempt = attempt_in_scope(extras)
    return None if attempt is None else attempt.routed


# ---------------------------------------------------------------------------
# cost basis, derived from the pin (never re-minted)
# ---------------------------------------------------------------------------


def cost_basis_from_promises(promises: Any) -> CostBasis:
    """The consented cost basis, read back off the promise row the user
    actually confirmed.

    Not a second mint (§4 scorecard: one authority per semantic fact): the
    compiled ``cost`` promise's ``basis`` IS the cost basis the consent bound,
    so the attempt pins the same object the consent did rather than
    recomputing one from live rates. No cost row means genuinely zero
    operator-borne cost (absence of a claim, not a claim of zero); an
    unbounded row means the cost could not be estimated.
    """
    rows = (
        promise.to_row() if isinstance(promise, Promise) else promise
        for promise in (promises or [])
    )
    row = next(
        (
            value
            for value in rows
            if isinstance(value, Mapping) and str(value.get("field")) == "cost"
        ),
        None,
    )
    if row is None:
        return OperatorBorneZeroCost()
    basis = row.get("basis")
    if not isinstance(basis, dict) or "unit_rate" not in basis:
        return UnpriceableCost()
    if "kind" in basis and basis.get("kind") != "priced":
        return UnpriceableCost()
    try:
        raw_row_quotes = basis.get("row_quote_quantities")
        row_quote_quantities = None
        if raw_row_quotes is not None:
            if not isinstance(raw_row_quotes, list):
                raise ValueError("row_quote_quantities must be a list")
            parsed_row_quotes: list[tuple[int, str]] = []
            for item in raw_row_quotes:
                if not isinstance(item, dict):
                    raise ValueError("row_quote_quantities contains a malformed row")
                row_id = item.get("row_id")
                quantity = item.get("quantity")
                if (
                    isinstance(row_id, bool)
                    or not isinstance(row_id, int)
                    or not isinstance(quantity, str)
                ):
                    raise ValueError("row_quote_quantities contains a malformed row")
                parsed_row_quotes.append((row_id, quantity))
            row_quote_quantities = tuple(parsed_row_quotes)
        return PricedCostBasis(
            pricing_key=basis["pricing_key"],
            unit_rate=basis["unit_rate"],
            estimated_quantity=basis["estimated_quantity"],
            quantity_unit=basis["quantity_unit"],
            terms_version=basis["terms_version"],
            quantity_rounding_mode=basis["quantity_rounding_mode"],
            quantity_rounding_decimal_places=basis["quantity_rounding_decimal_places"],
            meter_key=basis["meter_key"],
            meter_units_per_quantity_unit=basis["meter_units_per_quantity_unit"],
            ceiling_mode=basis["ceiling_mode"],
            row_settlement_mode=basis["row_settlement_mode"],
            charge_authority=basis["charge_authority"],
            hardware_class=basis.get("hardware_class"),
            throughput_ref=basis.get("throughput_ref"),
            row_quote_quantities=row_quote_quantities,
        )
    except (KeyError, TypeError, ValueError):
        # A basis this code cannot reconstruct is honestly unpriceable here;
        # it is never fabricated into a rate the user did not consent to.
        return UnpriceableCost()


def _cost_basis_json(cost_basis: CostBasis) -> str:
    if isinstance(cost_basis, PricedCostBasis):
        payload: dict[str, Any] = {
            "kind": "priced",
            "pricing_key": cost_basis.pricing_key,
            "unit_rate": cost_basis.unit_rate,
            "estimated_quantity": cost_basis.estimated_quantity,
            "quantity_unit": cost_basis.quantity_unit,
            "terms_version": cost_basis.terms_version,
            "quantity_rounding_mode": cost_basis.quantity_rounding_mode,
            "quantity_rounding_decimal_places": (
                cost_basis.quantity_rounding_decimal_places
            ),
            "meter_key": cost_basis.meter_key,
            "meter_units_per_quantity_unit": (cost_basis.meter_units_per_quantity_unit),
            "ceiling_mode": cost_basis.ceiling_mode,
            "row_settlement_mode": cost_basis.row_settlement_mode,
            "charge_authority": cost_basis.charge_authority,
        }
        if cost_basis.hardware_class is not None:
            payload["hardware_class"] = cost_basis.hardware_class
            payload["throughput_ref"] = cost_basis.throughput_ref
        if cost_basis.row_quote_quantities is not None:
            payload["row_quote_quantities"] = [
                {"row_id": row_id, "quantity": quantity}
                for row_id, quantity in cost_basis.row_quote_quantities
            ]
        return json.dumps(payload, sort_keys=True)
    if isinstance(cost_basis, OperatorBorneZeroCost):
        return json.dumps({"kind": "operator_borne_zero"})
    return json.dumps({"kind": "unpriceable"})


def _evaluation_json(evaluation: SetEvaluation | None) -> str | None:
    if evaluation is None:
        return None
    return json.dumps(
        {
            "overall": evaluation.overall,
            "results": [
                {
                    "field": promise.field,
                    "status": result.status,
                    "reason": result.reason,
                }
                for promise, result in evaluation.results
            ],
        },
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# row I/O
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ulid() -> str:
    from frisket.engine.store.execution_routes import _ulid as mint

    return mint()


def _attempt_owner(run_id: int | None, receipt_id: str | None) -> tuple[str, int | str]:
    """An invocation has one concrete durable owner; reclaimed rows cannot dispatch."""
    if receipt_id is not None:
        if run_id is not None or not isinstance(receipt_id, str) or not receipt_id:
            raise ValueError("an attempt requires exactly one run or receipt owner")
        return "receipt_id", receipt_id
    if type(run_id) is not int:
        raise ValueError("an attempt requires exactly one run or receipt owner")
    return "run_id", run_id


def open_attempt(
    project: Any,
    *,
    run_id: int | None = None,
    receipt_id: str | None = None,
    identity: str,
    scope: tuple[int, ...],
    commit: bool = True,
) -> tuple[str, int]:
    """The ``created`` transition (§1.5): owner, identity and **scope**
    freeze. Returns ``(attempt_id, seq)``.

    ``seq`` is per-owner monotone with a database uniqueness backstop, so a
    retry mints a NEW attempt (same owner, ``seq+1``, a new scope) rather than
    mutating the one that already ran.
    """
    db = project.db
    owner_column, owner_id = _attempt_owner(run_id, receipt_id)
    row = db.execute(
        f"SELECT COALESCE(MAX(seq), -1) AS top FROM execution_attempts WHERE {owner_column}=?",
        (owner_id,),
    ).fetchone()
    seq = int(row["top"]) + 1
    attempt_id = "attempt_" + _ulid()
    db.execute(
        "INSERT INTO execution_attempts (id, run_id, receipt_id, seq, state, "
        "action_identity_hash, scope_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            attempt_id,
            run_id,
            receipt_id,
            seq,
            "created",
            identity,
            json.dumps(list(scope)),
            _now(),
        ),
    )
    if commit:
        db.commit()
    return attempt_id, seq


def admit_attempt(
    project: Any,
    commitment: AttemptCommitment,
    *,
    commit: bool = True,
) -> None:
    """The ``admitted`` transition (§1.5): both head ids, the binding, the
    evaluation, the consent id, the cost basis and its terms version freeze."""
    routed = commitment.routed
    admitted_by_consent_id = commitment.admission.admitted_by_consent_id
    row_quotes = (
        commitment.cost_basis.row_quote_quantities
        if isinstance(commitment.cost_basis, PricedCostBasis)
        else None
    )
    if row_quotes is not None:
        quoted_scope = tuple(row_id for row_id, _quantity in row_quotes)
        if len(set(commitment.scope)) != len(commitment.scope) or set(
            quoted_scope
        ) != set(commitment.scope):
            raise RuntimeError(
                "row quote allocation must cover the attempt scope exactly"
            )
    project.db.execute(
        "UPDATE execution_attempts SET state='admitted', head_route_id=?, "
        "head_promise_set_id=?, admitted_by_consent_id=?, cost_basis_json=?, "
        "price_card_version=?, evaluation_json=? WHERE id=? AND state='created'",
        (
            routed.head_route_id if routed else None,
            routed.head_promise_set_id if routed else None,
            admitted_by_consent_id,
            _cost_basis_json(commitment.cost_basis),
            commitment.price_card_version,
            _evaluation_json(routed.evaluation if routed else None),
            commitment.attempt_id,
        ),
    )
    if row_quotes is not None:
        project.db.executemany(
            "INSERT INTO attempt_row_authorizations "
            "(attempt_id, row_id, quoted_quantity) VALUES (?,?,?)",
            [
                (commitment.attempt_id, row_id, quantity)
                for row_id, quantity in row_quotes
            ],
        )
    if commit:
        project.db.commit()


def set_attempt_state(
    project: Any,
    attempt_id: str,
    state: str,
    *,
    commit: bool = True,
) -> None:
    """A terminal transition (``effected``/``halted``). Never moves a row that
    already left ``dispatching`` — terminal states are final.

    A terminal transition also CLEARS ``runs.current_attempt_id`` when it names
    this attempt. Without that the pointer keeps naming a finished attempt
    forever, which is how it came to lie: readers asking "is a dispatch live on
    this run" through the pointer got a stale name (and, once two attempts
    could dispatch at all, the WRONG name). The authoritative answer is the
    ``execution_attempts`` row's state — see
    ``resolve_for_action.confirm_changed_claims``, which now asks the table
    directly — and the pointer's remaining job (stamping
    ``model_calls.attempt_id`` during dispatch) is served exactly as well by a
    pointer that goes NULL the moment the dispatch ends.
    """
    if state not in ATTEMPT_STATES:  # pragma: no cover - typed call sites
        raise ValueError(f"unknown attempt state {state!r}")
    project.db.execute(
        "UPDATE execution_attempts SET state=? WHERE id=? AND state IN "
        "('created','admitted','dispatching')",
        (state, attempt_id),
    )
    if state in TERMINAL_ATTEMPT_STATES:
        project.db.execute(
            "UPDATE runs SET current_attempt_id=NULL WHERE current_attempt_id=?",
            (attempt_id,),
        )
    if commit:
        project.db.commit()


def load_attempt(project: Any, attempt_id: str) -> Any:
    return project.db.execute(
        "SELECT * FROM execution_attempts WHERE id=?", (attempt_id,)
    ).fetchone()


def require_receipt_attempt_writer(
    project: Any, receipt_id: str, attempt_id: str, *, state: str = "dispatching"
) -> Any:
    """Fence receipt-owned accounting by its persisted invocation, not columns."""
    row = project.db.execute(
        "SELECT a.* FROM execution_attempts a JOIN receipts r ON r.id=a.receipt_id "
        "WHERE a.id=? AND a.receipt_id=? AND a.run_id IS NULL "
        "AND a.state=? AND r.run_id IS NULL AND r.status='running'",
        (attempt_id, receipt_id, state),
    ).fetchone()
    if row is None:
        raise StaleAttemptWriter(
            "receipt accounting requires its active admitted writer"
        )
    return row


def abandon_stale_dispatching_attempts(
    project: Any,
    run_id: int,
    *,
    max_age: timedelta = STALE_DISPATCHING_AGE,
    now: datetime | None = None,
    commit: bool = True,
) -> int:
    """Close a silent dispatch under the existing output-claim lease.

    A claimed output run is stale only after its attempt is old enough *and*
    every active claim row bound to the run has an expired (or malformed
    missing) lease. A claimless direct effect retains the same age-only bound.
    The caller may join an existing project transaction with ``commit=False``;
    initial-attempt mint does exactly that.
    """

    observed_now = now or datetime.now(timezone.utc)
    cutoff = (observed_now - max_age).isoformat(timespec="seconds")
    lease_cutoff = observed_now.isoformat(timespec="microseconds")
    from frisket.engine.store.output_claims import OutputColumnClaimStore

    return OutputColumnClaimStore(project).abandon_stale_dispatching_attempts(
        run_id=run_id,
        cutoff=cutoff,
        lease_cutoff=lease_cutoff,
        commit=commit,
    )


# ---------------------------------------------------------------------------
# The owned claim transaction (§1.3)
# ---------------------------------------------------------------------------


def _one_dispatch_refusal(db: Any, run_id: int) -> AttemptClaimRefused:
    """The typed refusal for a claim that lost the one-dispatch-per-run fence.

    Named separately because the loser must never see a bare
    ``sqlite3.IntegrityError``: the caller maps ``AttemptClaimRefused`` to the
    durable, reconsent-shaped failure, and an untyped exception here would
    surface as a generic job retry that claims again.
    """
    live = db.execute(
        "SELECT id FROM execution_attempts WHERE run_id=? AND state='dispatching' "
        "ORDER BY seq LIMIT 1",
        (run_id,),
    ).fetchone()
    if live is not None:
        return AttemptClaimRefused(
            "stale_head",
            f"execution attempt {live['id']} is already dispatching for this "
            "run; a run authorizes ONE dispatch at a time, so nothing was "
            "dispatched here and nothing was spent — wait for it to finish "
            "(or for recovery to close it) and resume again",
        )
    return AttemptClaimRefused(
        "stale_head",
        "the attempt left 'admitted' under the claim lock; nothing was dispatched",
    )


def claim(
    project: Any,
    commitment: AttemptCommitment,
    *,
    claimless_direct_effect: bool,
    claim_token: str | None = None,
    output_column_ids: set[int] | frozenset[int] | None = None,
) -> None:
    """Compare-and-swap the attempt to ``dispatching`` — the linearization
    point, taken before the first effect.

    ONE owned ``BEGIN IMMEDIATE``. It must not accept a ``txn=`` parameter and
    must not run inside a caller's transaction: the chain writer's
    join-fallback degrades to a DEFERRED transaction, which voids the write
    lock and turns this CAS back into a read-and-compare.

    Opaque handle (§1.2): every field consulted below comes from the PERSISTED
    row, not from ``commitment``. The passed value supplies only the id.

    **One dispatch per run.** The CAS is ``AND NOT EXISTS (a dispatching
    attempt on this run)``, and ``uq_execution_attempts_one_dispatching``
    (schema.py) is the same fence as a partial unique index — the pair mirrors
    ``uq_jobs_active_dedupe``, which backstops the analogous enqueue race. The
    CAS alone is not enough on its own history: it protects only writers that
    take this lock, and only the index makes "two live dispatches on one run"
    unrepresentable rather than merely unlikely. Without it, a run whose lease
    expired mid-dispatch could be resumed by a second worker while the first
    was still transcribing — two workers, one paid route, the user billed
    twice with nothing refusing.

    Reconsent is the serialized counterparty (see
    the backfill-confirm writer): it supersedes ``created``/``admitted``
    attempts inside its own ``BEGIN IMMEDIATE`` and refuses outright while a
    ``dispatching`` attempt is live. SQLite serializes the two writers, so
    there is a defined winner and no window.
    """
    db = project.db
    if db.in_transaction:
        raise AttemptClaimRefused(
            "consent_missing",
            "the attempt claim requires its OWN write transaction; joining a "
            "caller's transaction would degrade the BEGIN IMMEDIATE write "
            "lock to a deferred read and turn the compare-and-swap into a "
            "non-atomic read-and-compare of the persisted attempt commitment",
        )
    try:
        db.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        # Losing the write lock is a race outcome, not an infrastructure
        # fault: another writer (a claim on a sibling run, a reconsent) holds
        # it. Say so in the vocabulary the caller already handles, so it
        # retries the whole bind-and-verify rather than surfacing a bare
        # sqlite3 error as an untyped job failure.
        raise AttemptClaimRefused(
            "stale_head",
            "the attempt claim could not take its write transaction "
            f"({exc}); another writer holds this bundle's lock and nothing "
            "was dispatched — resume again to bind and verify one head",
        ) from exc
    try:
        row = load_attempt(project, commitment.attempt_id)
        if row is None:
            raise AttemptClaimRefused(
                "consent_missing",
                f"attempt {commitment.attempt_id} is not persisted; nothing "
                "authorizes this dispatch",
            )
        state = str(row["state"])
        if state != "admitted":
            # Zero rows == refusal. 'superseded' is the reconsent winning the
            # race; anything else is a lifecycle bug that must not dispatch.
            raise AttemptClaimRefused(
                "stale_head",
                f"this run's attempt is '{state}', not 'admitted' — a "
                "concurrent reconsent superseded it before dispatch could "
                "claim it; nothing was dispatched, so resume again to bind "
                "and verify one head",
            )
        if row["run_id"] is None and row["receipt_id"] is None:
            # The run was reclaimed by `compact()` between admission and this
            # claim (its branch was undone and discarded in the window). The
            # attempt row survives as the consent record, but it
            # authorizes nothing: there is no run left to dispatch against.
            # Before the row survived at all this was the `row is None`
            # refusal above; it stays a TYPED refusal rather than becoming a
            # TypeError on `int(None)` two lines down.
            raise AttemptClaimRefused(
                "consent_missing",
                f"attempt {commitment.attempt_id} no longer names a run — the "
                "run was reclaimed by compaction after its branch was "
                "discarded; nothing was dispatched",
            )
        head_route_id = row["head_route_id"]
        if head_route_id is not None:
            # Re-read BOTH chain heads INSIDE this lock and refuse unless they
            # still equal the ids the admission pinned.
            from frisket.engine.store.execution_routes import RouteStore

            store = (
                RouteStore.for_receipt(project, row["receipt_id"])
                if row["receipt_id"] is not None
                else RouteStore.for_run(project, int(row["run_id"]))
            )
            head = store.head()
            live_route = None if head is None else head[0].id
            live_set = None if head is None else head[1].id
            if live_route != head_route_id or live_set != row["head_promise_set_id"]:
                raise AttemptClaimRefused(
                    "stale_head",
                    "this run's execution route advanced from "
                    f"{head_route_id} to {live_route} between admission and "
                    "the dispatch claim (a concurrent reconsent); nothing was "
                    "dispatched — resume again so dispatch and verification "
                    "agree on one route head",
                )
        run_id = row["run_id"]
        receipt_id = row["receipt_id"]
        owner_column, owner_id = _attempt_owner(run_id, receipt_id)
        from frisket.engine.store.output_claims import OutputColumnClaimStore

        output_claims = OutputColumnClaimStore(project)
        if receipt_id is not None:
            if (
                not claimless_direct_effect
                or claim_token is not None
                or output_column_ids
            ):
                raise AttemptClaimRefused(
                    "stale_head", "receipt attempts cannot authorize output columns"
                )
            try:
                require_receipt_attempt_writer(
                    project, receipt_id, commitment.attempt_id, state="admitted"
                )
            except StaleAttemptWriter as exc:
                raise AttemptClaimRefused("stale_head", str(exc)) from exc
        elif claimless_direct_effect:
            if output_column_ids:
                raise AttemptClaimRefused(
                    "stale_head",
                    "a claimless direct effect cannot authorize output columns",
                )
            if output_claims.has_active_claims(run_id=run_id):
                raise AttemptClaimRefused(
                    "stale_head",
                    "this output-producing run has an active claim group "
                    "and cannot dispatch as a claimless direct effect",
                )
        else:
            if not isinstance(claim_token, str) or not claim_token:
                raise AttemptClaimRefused(
                    "stale_head",
                    "the prepared dispatch carries no output claim token",
                )
            claimed = output_claims.active_column_ids(
                claim_token=claim_token,
                run_id=run_id,
            )
            if not claimed:
                raise AttemptClaimRefused(
                    "stale_head",
                    "the prepared dispatch's output claim group is not "
                    "active on this run",
                )
            missing = set(output_column_ids or ()) - claimed
            if missing:
                raise AttemptClaimRefused(
                    "stale_head",
                    "the prepared dispatch's output claim group does not "
                    f"cover columns {sorted(missing)}",
                )
        try:
            updated = db.execute(
                "UPDATE execution_attempts SET state='dispatching' "
                "WHERE id=? AND state='admitted' AND NOT EXISTS ("
                "SELECT 1 FROM execution_attempts live "
                f"WHERE live.{owner_column}=? AND live.state='dispatching')",
                (commitment.attempt_id, owner_id),
            )
        except sqlite3.IntegrityError as exc:
            # uq_execution_attempts_one_dispatching. The CAS above should have
            # refused first; if the index is what caught it, the answer the
            # caller gets is still the typed one, never a bare IntegrityError
            # escaping as an untyped job failure.
            if receipt_id is not None:
                raise AttemptClaimRefused(
                    "stale_head", "this receipt already has a dispatching attempt"
                ) from exc
            raise _one_dispatch_refusal(db, run_id) from exc
        if updated.rowcount != 1:
            if receipt_id is not None:
                raise AttemptClaimRefused(
                    "stale_head", "this receipt already has a dispatching attempt"
                )
            raise _one_dispatch_refusal(db, run_id)
        if run_id is not None:
            db.execute(
                "UPDATE runs SET current_attempt_id=? WHERE id=?",
                (commitment.attempt_id, run_id),
            )
    except BaseException:
        db.rollback()
        raise
    db.commit()


# ---------------------------------------------------------------------------
# The attempt receipt (§1.8) — an acceptance criterion, not an extra
# ---------------------------------------------------------------------------


def attempt_receipt(project: Any, attempt_id: str) -> dict[str, Any] | None:
    """The six facts a user must be able to read off one attempt (§1.8):

    1. **which rows it covered** — ``scope``;
    2. **which target/provider the data went to** — ``target``;
    3. **which consent authorized it** — ``consent.id``;
    4. **whether that consent was direct or derived** — ``consent.grant_basis``
       (this is ``consents.grant_basis``'s first reader, which is why B6
       resolved as an unconditional KEEP);
    5. **which cost basis and opaque terms version were authorized** —
       ``cost_basis`` and the compatibility-spelled ``price_card_version``;
    6. **its terminal outcome** — ``state``.

    The same view joins settlement on ``attempt_id``. Without it,
    ``execution_attempts`` is an internal forensic table and three of the four
    product sentences go unanswered.
    """
    row = load_attempt(project, attempt_id)
    if row is None:
        return None
    keys = row.keys()

    def _json(name: str) -> Any:
        raw = row[name] if name in keys else None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):  # pragma: no cover - written by us
            return None

    consent: dict[str, Any] | None = None
    consent_id = row["admitted_by_consent_id"]
    if consent_id is not None:
        consent_row = project.db.execute(
            "SELECT id, grant_basis, actor, granted_at, promise_set_hash "
            "FROM consents WHERE id=?",
            (consent_id,),
        ).fetchone()
        if consent_row is not None:
            consent = {
                "id": consent_row["id"],
                # 'user_confirmation' vs 'exact_match_derived': what
                # admitted_by_consent_id MEANS.
                "grant_basis": consent_row["grant_basis"],
                "actor": consent_row["actor"],
                "granted_at": consent_row["granted_at"],
                "promise_set_hash": consent_row["promise_set_hash"],
            }

    target: dict[str, Any] | None = None
    route_id = row["head_route_id"]
    if route_id is not None:
        route_row = project.db.execute(
            "SELECT target_snapshot_json, operator, egress_class, region, "
            "credential_source, engine FROM routes WHERE id=?",
            (route_id,),
        ).fetchone()
        if route_row is not None:
            from frisket.execution.targets import validated_target_snapshot

            snapshot = validated_target_snapshot(
                json.loads(route_row["target_snapshot_json"])
            )
            target = {
                "target_id": snapshot["target_id"],
                "transport": snapshot["transport"],
                "engine": route_row["engine"],
                "operator": route_row["operator"],
                "egress_class": route_row["egress_class"],
                "region": route_row["region"],
                "credential_source": route_row["credential_source"],
            }

    # NULL once `compact()` has reclaimed the run this attempt authorized.
    # The consent and charge record outlives
    # the data it describes, and says plainly that the run is gone rather
    # than naming an id nothing resolves.
    run_id = row["run_id"]
    receipt_id = row["receipt_id"]
    run_status_row = (
        None
        if run_id is None
        else project.db.execute(
            "SELECT status FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
    )
    terminal_status = None if run_status_row is None else str(run_status_row["status"])
    if receipt_id is not None:
        owner = project.db.execute(
            "SELECT status FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()
        terminal_status = str(owner["status"]) if owner is not None else None
    reclaimed = run_id is None and receipt_id is None
    return {
        "attempt_id": row["id"],
        "run_id": None if run_id is None else int(run_id),
        "receipt_id": receipt_id,
        "seq": int(row["seq"]),
        "state": row["state"],
        "action_identity_hash": row["action_identity_hash"],
        "scope": _json("scope_json") or [],
        "target": target,
        "consent": consent,
        "cost_basis": _json("cost_basis_json"),
        "price_card_version": row["price_card_version"],
        "settlement": attempt_settlement(
            project,
            attempt_id=str(row["id"]),
            cost_basis=_json("cost_basis_json"),
            price_card_version=row["price_card_version"],
            run_reclaimed=reclaimed,
            terminal_status=terminal_status,
        ),
        "evaluation": _json("evaluation_json"),
        "borne_by": attempt_borne_by(
            project,
            attempt_id=str(row["id"]),
            run_reclaimed=reclaimed,
        ),
        "created_at": row["created_at"],
    }


def attempt_borne_by(
    project: Any,
    *,
    attempt_id: str,
    run_reclaimed: bool,
) -> dict[str, Any] | None:
    """Whose credential actually bore this attempt's work.

    ``cost_basis`` answers what the platform was authorized to charge; for an
    ``operator_borne_zero`` attempt that answer is "nothing". It does NOT
    follow that the run was free — a run billed to the user's own OpenAI key
    is operator-borne AND cost the user money, and the Charges panel used to
    call both of them "no cost to you".

    The distinguishing fact exists one grain down, on the calls the attempt
    authorized: ``credential_source`` is the OBSERVED per-call provenance
    (``runtime_binding`` reconciles the route pin against what the adapter
    actually used, and keeps the observed value on tariffed transports), and
    ``cost_source`` separates a local model call — which also pins
    ``credential_source='local'`` — from a real provider call on a key the
    operator holds.

    Derived here rather than in the client because the client has neither
    column, and a UI that guessed from ``cost_basis`` alone is exactly the
    bug (pattern 2: one computation, not two).

    Returns ``None`` when nothing can be said — the metering is gone, or no
    call was ever recorded — which is not the same as "nothing external ran"
    and must not render as a claim of free.
    """
    from frisket.execution.credential_use import (
        ENV_KEY,
        ORG_BYOK_KEY,
        PROJECT_KEY,
        credential_class_of,
    )

    if run_reclaimed:
        return None
    rows = project.db.execute(
        "SELECT provider, credential_source, cost_source FROM model_calls "
        "WHERE attempt_id=?",
        (attempt_id,),
    ).fetchall()
    if not rows:
        return None
    operator_held = {PROJECT_KEY, ORG_BYOK_KEY, ENV_KEY}
    providers = {
        str(row["provider"])
        for row in rows
        # `free_local` is the local/sidecar constructors' own cost source, and
        # they pin `credential_source='local'` too — without this the in-process
        # engines would be reported as a key the user is being billed against.
        if row["cost_source"] != "free_local"
        and credential_class_of(row["credential_source"]) in operator_held
    }
    return {"credentialed_providers": sorted(providers)}


def attempt_settlement(
    project: Any,
    *,
    attempt_id: str,
    cost_basis: Any,
    price_card_version: str | None,
    run_reclaimed: bool,
    terminal_status: str | None,
) -> dict[str, Any] | None:
    """THE settlement join: "what did it cost", answered by
    summing the metering rows that carry this attempt's id and rating them
    under the complete terms the attempt PINNED.

    Authorization (``execution_attempts``: identity, scope, consent, cost
    basis, ``price_card_version``) joined to settlement (``model_calls``:
    the measured quantity) on ``attempt_id`` — one column per side, no
    settlement table, no new write path. The measured quantity exists only at
    the per-model-call grain, so the attempt-level answer is a JOIN.

    The rate-change rule made concrete: no live rate, SKU table, meter table,
    or deployment offering is consulted here.

    ``run_reclaimed`` is required, not defaulted: it is the one input a
    caller must not be able to forget. ``model_calls.run_id`` cascades, so
    once ``compact()`` has deleted the run, THIS JOIN'S ROWS ARE GONE — and
    summing what is left returns a confident ``charge_usd`` of "0" for a run
    that really was billed. That is the fabricated total ``settle`` refuses
    to produce everywhere else, and it is worse than the missing record
    settlement must avoid, because it reads as an answer.

    ``terminal_status`` is equally required.  A cancelled run is a finished
    claim-layer outcome, but only its completed rows are eligible for money
    settlement; the row authorization join below proves and excludes the
    cancelled remainder before any fact reaches the pure price-book rater.
    """
    from frisket.execution.price_book import settle

    if run_reclaimed:
        # The AUTHORIZED side survives on the attempt and the receipt still
        # reports it (cost basis, pinned card, consent): "did I approve it,
        # and at what quoted price" stays answerable. What it actually
        # metered does not, and this says so rather than guessing.
        return {
            "price_card_version": price_card_version,
            "terminal_status": None,
            "charge_usd": None,
            "unsettleable": "metering_reclaimed",
        }
    if terminal_status not in {"running", "completed", "failed", "cancelled"}:
        return {
            "price_card_version": price_card_version,
            "terminal_status": terminal_status,
            "charge_usd": None,
            "unsettleable": "run_terminal_status_missing",
        }
    calls = project.db.execute(
        "SELECT row_id, units FROM model_calls WHERE attempt_id=? "
        "ORDER BY created_at, id",
        (attempt_id,),
    ).fetchall()

    row_mode = (
        cost_basis.get("row_settlement_mode") if isinstance(cost_basis, dict) else None
    )
    allocation: dict[int, tuple[str, Decimal]] | None = None
    outcomes: dict[int, str] | None = None
    inspection_refusal: str | None = None
    settlement_calls = calls
    all_rows_cancelled = False
    if row_mode == "quoted_successful_rows":
        allocation, outcomes, inspection_refusal = _inspect_quoted_row_settlement(
            project,
            attempt_id=attempt_id,
            cost_basis=cost_basis,
            calls=calls,
        )
        if inspection_refusal is None:
            assert allocation is not None and outcomes is not None
            if "cancelled" in outcomes.values() and terminal_status != "cancelled":
                inspection_refusal = "row_cancelled_run_status_mismatch"
            else:
                # Cancelled rows are individually outside settlement.  In
                # particular, an incomplete fact from a cancelled-after-return
                # row cannot poison completed siblings into one false partial
                # total.  The raw fact remains durable evidence on the attempt.
                settlement_calls = [
                    call
                    for call in calls
                    if outcomes[int(call["row_id"])] != "cancelled"
                ]
                all_rows_cancelled = bool(outcomes) and all(
                    outcome == "cancelled" for outcome in outcomes.values()
                )

    metered: list[dict[str, Any]] = []
    for call in settlement_calls:
        try:
            units = json.loads(call["units"] or "{}")
        except (TypeError, ValueError):  # pragma: no cover - written by us
            units = {}
        metered.append(units if isinstance(units, dict) else {})
    receipt = settle(
        cost_basis=cost_basis,
        metered_units=metered,
        price_card_version=price_card_version,
        terminal_status=terminal_status,
        all_rows_cancelled=all_rows_cancelled,
    )
    if row_mode != "quoted_successful_rows":
        return receipt
    # Inspection runs before rating.  When either side refuses, preserve the
    # meter's more specific incomplete-fact diagnosis; a plausible complete
    # number is never allowed through a failed ownership/outcome join.
    if receipt.get("charge_usd") is None:
        return receipt
    if inspection_refusal is not None:
        return _refuse_quoted_rows(receipt, inspection_refusal)
    assert allocation is not None and outcomes is not None
    return _apply_quoted_row_settlement(
        allocation=allocation,
        outcomes=outcomes,
        receipt=receipt,
    )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _refuse_quoted_rows(receipt: dict[str, Any], reason: str) -> dict[str, Any]:
    receipt["charge_usd"] = None
    receipt["charged_quantity"] = None
    receipt["absorbed_overage_usd"] = None
    receipt["unsettleable"] = reason
    return receipt


def _inspect_quoted_row_settlement(
    project: Any,
    *,
    attempt_id: str,
    cost_basis: dict[str, Any],
    calls: list[Any],
) -> tuple[
    dict[int, tuple[str, Decimal]] | None,
    dict[int, str] | None,
    str | None,
]:
    """Inspect the durable row join before any fact is handed to ``settle``.

    Three independently durable sets must agree: the allocation inside the
    consented cost basis, the attempt-owned authorization rows minted from it,
    and the attempt-owned terminal outcomes written with results.  No equal
    share or actual-meter-derived substitute is safe when one is absent.
    """
    raw_allocation = cost_basis.get("row_quote_quantities")
    if not isinstance(raw_allocation, list):
        return None, None, "row_quote_allocation_missing"

    allocation: dict[int, tuple[str, Decimal]] = {}
    previous_row_id = -1
    try:
        for item in raw_allocation:
            if not isinstance(item, dict):
                raise ValueError
            row_id = item.get("row_id")
            raw_quantity = item.get("quantity")
            if (
                isinstance(row_id, bool)
                or not isinstance(row_id, int)
                or row_id <= previous_row_id
                or row_id in allocation
                or not isinstance(raw_quantity, str)
            ):
                raise ValueError
            quantity = Decimal(raw_quantity)
            if not quantity.is_finite() or quantity < 0:
                raise ValueError
            allocation[row_id] = (raw_quantity, quantity)
            previous_row_id = row_id
        estimated = Decimal(str(cost_basis["estimated_quantity"]))
        if (
            not estimated.is_finite()
            or estimated < 0
            or sum((quantity for _raw, quantity in allocation.values()), Decimal(0))
            != estimated
        ):
            raise ValueError
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return None, None, "row_quote_allocation_invalid"

    attempt = project.db.execute(
        "SELECT scope_json FROM execution_attempts WHERE id=?", (attempt_id,)
    ).fetchone()
    try:
        raw_scope = json.loads(attempt["scope_json"] if attempt is not None else "")
        if not isinstance(raw_scope, list):
            raise ValueError
        scope = []
        for raw_row_id in raw_scope:
            if isinstance(raw_row_id, bool) or not isinstance(raw_row_id, int):
                raise ValueError
            scope.append(raw_row_id)
        if len(scope) != len(set(scope)) or set(scope) != set(allocation):
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, None, "row_quote_scope_mismatch"

    durable_rows = project.db.execute(
        "SELECT row_id, quoted_quantity, terminal_outcome "
        "FROM attempt_row_authorizations WHERE attempt_id=? ORDER BY row_id",
        (attempt_id,),
    ).fetchall()
    if len(durable_rows) != len(allocation):
        return None, None, "row_quote_allocation_missing"

    outcomes: dict[int, str] = {}
    for row in durable_rows:
        row_id = int(row["row_id"])
        expected = allocation.get(row_id)
        if expected is None or row["quoted_quantity"] != expected[0]:
            return None, None, "row_quote_allocation_mismatch"
        outcome = row["terminal_outcome"]
        if outcome not in {"succeeded", "failed", "cancelled"}:
            return None, None, "row_terminal_outcome_missing"
        outcomes[row_id] = str(outcome)

    fact_row_ids: set[int] = set()
    for call in calls:
        row_id = call["row_id"]
        if isinstance(row_id, bool) or not isinstance(row_id, int):
            return None, None, "row_fact_ownership_missing"
        if row_id not in allocation:
            return None, None, "row_fact_ownership_mismatch"
        fact_row_ids.add(row_id)
    succeeded = {
        row_id for row_id, outcome in outcomes.items() if outcome == "succeeded"
    }
    if not succeeded.issubset(fact_row_ids):
        return None, None, "row_success_meter_missing"

    return allocation, outcomes, None


def _apply_quoted_row_settlement(
    *,
    allocation: dict[int, tuple[str, Decimal]],
    outcomes: dict[int, str],
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Charge each successful completed row from its consented allocation."""
    succeeded = {
        row_id for row_id, outcome in outcomes.items() if outcome == "succeeded"
    }
    charged_quantity = sum((allocation[row_id][1] for row_id in succeeded), Decimal(0))
    try:
        rate = Decimal(str(receipt["unit_rate"]))
        rated_charge = Decimal(str(receipt["rated_charge_usd"]))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return _refuse_quoted_rows(receipt, "row_rating_invalid")
    charge = rate * charged_quantity
    receipt["charged_quantity"] = _decimal_text(charged_quantity)
    receipt["charge_usd"] = _decimal_text(charge)
    receipt["absorbed_overage_usd"] = _decimal_text(
        max(rated_charge - charge, Decimal(0))
    )
    return receipt


def run_attempt_receipts(project: Any, run_id: int) -> list[dict[str, Any]]:
    """Every attempt on one run, newest first. A retry mints a new attempt
    rather than mutating the old one, so this is also how a user sees two
    attempts covering a row that was paid for twice (§1.5).

    ``WHERE run_id=?`` by construction, which is why it cannot reach a
    COMPACTION-ORPHANED attempt: see ``project_attempt_receipts``."""
    rows = project.db.execute(
        "SELECT id FROM execution_attempts WHERE run_id=? ORDER BY seq DESC",
        (run_id,),
    ).fetchall()
    receipts = [attempt_receipt(project, str(row["id"])) for row in rows]
    return [receipt for receipt in receipts if receipt is not None]


def project_attempt_receipts(
    project: Any,
    *,
    run_id: int | None = None,
    offset: int = 0,
    limit: int = 25,
) -> tuple[int, list[dict[str, Any]]]:
    """Every attempt receipt in one PROJECT, newest first — the reader that
    can open a compaction-orphaned attempt. Returns ``(total, page)``.

    ``execution_attempts.run_id`` is nullable with ON DELETE SET
    NULL so the record that a user consented and was charged outlives the
    data it describes. But every reader keyed on ``run_id=?``, so the record
    it preserved was one nothing could open: after ``compact()`` the orphan
    joins no run, and a run-keyed list correctly never returns it. Keying by
    PROJECT makes the orphan an ordinary row (``run_id`` null, which the
    receipt already reports honestly), so the surface that preserves the
    record is also the surface that can show it.

    The optional ``run_id`` filter is the run-keyed question asked of this
    same query, so a run detail view and a project-level list cannot disagree
    about one attempt: both render ``attempt_receipt``.
    """
    where = "" if run_id is None else "WHERE run_id=?"
    args: tuple[Any, ...] = () if run_id is None else (run_id,)
    total = int(
        project.db.execute(
            f"SELECT COUNT(*) AS n FROM execution_attempts {where}", args
        ).fetchone()["n"]
    )
    rows = project.db.execute(
        # created_at is the project-wide order; seq breaks the tie WITHIN a
        # run (two attempts on one run can share a whole-second timestamp),
        # which keeps a run-filtered page in the same newest-first order
        # ``run_attempt_receipts`` returns.
        f"SELECT id FROM execution_attempts {where} "
        "ORDER BY created_at DESC, seq DESC, id DESC LIMIT ? OFFSET ?",
        (*args, limit, offset),
    ).fetchall()
    receipts = [attempt_receipt(project, str(row["id"])) for row in rows]
    return total, [receipt for receipt in receipts if receipt is not None]
