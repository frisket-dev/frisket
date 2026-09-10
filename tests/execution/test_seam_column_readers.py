"""The seam-column reader test.

**Every column of the six execution-seam tables names the code path that
reads it, by SYMBOL.** Keyed on symbols and not line numbers, so the registry
survives refactoring; closed over the live schema, so a new column is red
until someone says who reads it — or says, in writing, that nothing does.

Why the seam and not the whole database. These six tables are where consent,
money and data-egress are recorded. CLAUDE.md's rule is that *a column you
only read is cheap; a state you branch on is not* — and the way that rule
gets violated is a column that looks load-bearing (it is written on every
run, it is in the schema, it has a comment) while nothing anywhere reads it.
Two of the execution-route cutover deletions were exactly that shape. This registry makes the
question answerable in one place, and makes the honest answer — ``RECORD`` —
a first-class, stated ruling rather than an absence somebody has to
re-derive.

Three checks:

1. **Closure.** The declared keys equal the live ``PRAGMA table_info``
   columns. Adding a column without an entry is red.
2. **The symbol resolves.** Every named reader is importable and exists —
   this is the half that makes the registry survive a rename.
3. **The symbol plausibly reads the column** (the cheap mutation check): the
   named symbol's own source mentions the column, or the attribute a decoder
   maps it to. A reader that stops reading its column is red.
"""

from __future__ import annotations

import inspect
import pathlib
import re
import tempfile

import pytest

from frisket.engine.store import Project

SEAM_TABLES = (
    "consents",
    "promise_sets",
    "routes",
    "binding_epochs",
    "route_violations",
    "execution_attempts",
)


class RECORD:
    """A column nothing branches on, renders, or joins — deliberately.

    The ruling is stated here so a reader does not have to re-derive it, and
    so promoting a record to a behavioral state is a visible diff. CLAUDE.md:
    "A behavioral state ships in the same release as the operator who sets
    it, or not at all."
    """

    def __init__(self, why: str) -> None:
        self.why = why


#: "<table>.<column>" -> (module, symbol) | RECORD(why)
#: ``symbol`` is a module-level name, or ``Class.method``.
SEAM_COLUMN_READERS: dict[str, object] = {
    "consents.id": (
        "frisket.execution.attempt_authority",
        "admit_routed",
    ),
    "consents.subject_kind": (
        "frisket.engine.store.execution_routes",
        "RouteStore.consents",
    ),
    "consents.subject_id": (
        "frisket.engine.store.execution_routes",
        "RouteStore.consents",
    ),
    "consents.action_identity_hash": (
        "frisket.engine.store.execution_routes",
        "ConsentRegistry.action_consents",
    ),
    "consents.promise_set_hash": (
        "frisket.execution.attempt_authority",
        "admit_routed",
    ),
    "consents.standing_policy": (
        "frisket.engine.store.execution_routes",
        "head_standing_cost_consent",
    ),
    # F5: a restored bundle's foreign consents fail closed.
    "consents.actor": (
        "frisket.execution.attempt_authority",
        "admit_routed",
    ),
    "consents.granted_at": (
        "frisket.engine.store.execution_routes",
        "head_standing_cost_consent",
    ),
    "consents.policy_params_json": (
        "frisket.engine.store.execution_routes",
        "standing_cost_threshold",
    ),
    "consents.grant_basis": ("frisket.execution.attempt", "attempt_receipt"),
    # The quote the user approved. Its reader is the dispatch fence, which
    # reconstructs the confirmation hash from live neutral facts plus THIS
    # record's rated projection — the pricing policy that produced the billed
    # figure belongs to the deployment and is deliberately not wired into
    # dispatch, so the figure has to be persisted rather than re-derived.
    "consents.quote_json": (
        "frisket.execution.attempt_authority",
        "AttemptAuthority._proving_consent",
    ),
    "promise_sets.id": (
        "frisket.execution.resolve_for_action",
        "confirm_changed_claims",
    ),
    "promise_sets.subject_kind": (
        "frisket.engine.store.execution_routes",
        "_append_route_cas",
    ),
    "promise_sets.subject_id": (
        "frisket.engine.store.execution_routes",
        "_append_route_cas",
    ),
    "promise_sets.seq": (
        "frisket.engine.store.execution_routes",
        "RouteStore.append_promise_set",
    ),
    "promise_sets.consent_id": RECORD(
        "the set -> consent backlink. Admission goes the other way (consent -> "
        "set hash), so no src path branches on it; it exists so an auditor "
        "reading a promise set can find the confirmation that authorized it."
    ),
    "promise_sets.promises_json": (
        "frisket.execution.attempt_authority",
        "admit_routed",
    ),
    "promise_sets.promise_set_hash": (
        "frisket.execution.attempt_authority",
        "admit_routed",
    ),
    "promise_sets.created_at": RECORD(
        "append timestamp. Chain order is `seq` everywhere, so nothing reads "
        "it; a forensic reader of the chain would."
    ),
    "routes.id": ("frisket.execution.attempt_authority", "admit_routed"),
    "routes.subject_kind": (
        "frisket.engine.store.execution_routes",
        "_append_route_cas",
    ),
    "routes.subject_id": (
        "frisket.engine.store.execution_routes",
        "_append_route_cas",
    ),
    "routes.seq": ("frisket.engine.store.execution_routes", "_append_route_cas"),
    "routes.predecessor_id": (
        "frisket.engine.store.execution_routes",
        "_append_route_cas",
    ),
    "routes.promise_set_id": (
        "frisket.engine.store.execution_routes",
        "RouteStore.head",
    ),
    "routes.engine": ("frisket.execution.resolver", "route_row_facts_from_row"),
    "routes.options_json": RECORD(
        "the authored options snapshot. Its only non-decoder use is being "
        "copied verbatim onto a successor route by observe_binding_divergence; "
        "nothing branches on or renders it."
    ),
    "routes.target_snapshot_json": (
        "frisket.execution.runtime_binding",
        "bind_fact_to_route",
    ),
    "routes.route_fact_hash": (
        "frisket.engine.store.execution_routes",
        "_append_route_cas",
    ),
    "routes.operator": ("frisket.execution.attempt", "attempt_receipt"),
    "routes.egress_class": ("frisket.execution.attempt", "attempt_receipt"),
    "routes.region": ("frisket.execution.attempt", "attempt_receipt"),
    "routes.credential_source": (
        "frisket.execution.runtime_binding",
        "bind_fact_to_route",
    ),
    # The pre-effect credential-USE fence reads the posture as the
    # consented credential CLASS and refuses before the external call.
    "routes.cost_posture": (
        "frisket.sdk.ops.transcription.hosted",
        "_halt_unless_consented_credential",
    ),
    "routes.created_at": RECORD(
        "append timestamp. Chain order is `seq`; only a forensic reader would."
    ),
    "binding_epochs.id": (
        "frisket.engine.store.execution_routes",
        "_open_epoch",
    ),
    "binding_epochs.route_id": (
        "frisket.engine.store.execution_routes",
        "_open_epoch",
    ),
    "binding_epochs.provenance_hash": (
        "frisket.engine.store.execution_routes",
        "_open_epoch",
    ),
    "binding_epochs.observed_json": RECORD(
        "the observation-only payload (revision/device/dtype + provenance). "
        "Divergence is decided from the in-memory observed dict at the "
        "observation point, never re-read from this column; nothing filters "
        "or renders it. It is the evidence a human or a future forensic view "
        "reads to say what was actually bound; observed provenance remains "
        "post-effect evidence."
    ),
    "binding_epochs.created_at": RECORD(
        "epoch open time; nothing orders or filters on it."
    ),
    "route_violations.id": (
        "frisket.engine.store.execution_routes",
        "RouteStore.violations",
    ),
    "route_violations.route_id": (
        "frisket.engine.store.execution_routes",
        "RouteStore.violations",
    ),
    "route_violations.promise_set_id": RECORD(
        "names which compiled set the broken claim came from. The ledger read "
        "scopes through route_id, so nothing joins or branches on it."
    ),
    "route_violations.promise_fingerprint": (
        "frisket.engine.store.execution_routes",
        "_record_violation",
    ),
    "route_violations.observed_json": RECORD(
        "the observed facts at violation time. Hashed into dedupe_key at "
        "WRITE; the stored column itself is only decoded, never branched on."
    ),
    "route_violations.dedupe_key": (
        "frisket.engine.store.execution_routes",
        "_record_violation",
    ),
    "route_violations.created_at": (
        "frisket.engine.store.execution_routes",
        "RouteStore.violations",
    ),
    "execution_attempts.id": ("frisket.execution.attempt", "claim"),
    "execution_attempts.run_id": ("frisket.execution.attempt", "claim"),
    "execution_attempts.receipt_id": (
        "frisket.execution.attempt",
        "require_receipt_attempt_writer",
    ),
    "execution_attempts.seq": ("frisket.execution.attempt", "open_attempt"),
    "execution_attempts.state": ("frisket.execution.attempt", "claim"),
    "execution_attempts.action_identity_hash": (
        "frisket.execution.attempt",
        "attempt_receipt",
    ),
    "execution_attempts.scope_json": (
        "frisket.execution.attempt",
        "attempt_receipt",
    ),
    "execution_attempts.head_route_id": ("frisket.execution.attempt", "claim"),
    "execution_attempts.head_promise_set_id": (
        "frisket.execution.attempt",
        "claim",
    ),
    "execution_attempts.admitted_by_consent_id": (
        "frisket.execution.attempt",
        "attempt_receipt",
    ),
    "execution_attempts.cost_basis_json": (
        "frisket.execution.attempt",
        "attempt_settlement",
    ),
    "execution_attempts.price_card_version": (
        "frisket.execution.attempt",
        "attempt_settlement",
    ),
    "execution_attempts.evaluation_json": (
        "frisket.execution.attempt",
        "attempt_receipt",
    ),
    "execution_attempts.created_at": (
        "frisket.engine.store.output_claims",
        "OutputColumnClaimStore.abandon_stale_dispatching_attempts",
    ),
}


def _live_columns() -> set[str]:
    with tempfile.TemporaryDirectory() as tmp:
        project = Project.create(pathlib.Path(tmp) / "schema.frisket")
        try:
            return {
                f"{table}.{row[1]}"
                for table in SEAM_TABLES
                for row in project.db.execute(f"PRAGMA table_info({table})")
            }
        finally:
            project.close()


def _resolve(module_path: str, symbol: str):
    module = __import__(module_path, fromlist=[symbol.split(".")[0]])
    target = module
    for part in symbol.split("."):
        target = getattr(target, part)
    return target


def test_every_seam_column_is_declared_and_no_declaration_is_stale():
    live = _live_columns()
    declared = set(SEAM_COLUMN_READERS)
    assert declared == live, (
        "seam-column registry drifted from the schema. "
        f"undeclared columns: {sorted(live - declared)}; "
        f"stale entries: {sorted(declared - live)}"
    )


@pytest.mark.parametrize("column", sorted(SEAM_COLUMN_READERS))
def test_each_column_names_a_reader_that_exists_and_reads_it(column: str):
    entry = SEAM_COLUMN_READERS[column]
    if isinstance(entry, RECORD):
        assert entry.why.strip(), f"{column}: a RECORD ruling must say why"
        return
    module_path, symbol = entry  # type: ignore[misc]
    reader = _resolve(module_path, symbol)
    source = inspect.getsource(reader)
    name = column.split(".", 1)[1]
    # A decoder maps `<x>_json` onto the attribute `<x>`; either spelling in
    # the reader's own source counts.
    candidates = {name}
    if name.endswith("_json"):
        candidates.add(name[: -len("_json")])
    assert any(
        re.search(rf"\b{re.escape(candidate)}\b", source) for candidate in candidates
    ), (
        f"{column}: the declared reader {module_path}.{symbol} no longer "
        f"mentions {sorted(candidates)} — either it stopped reading the "
        "column (repoint the entry, or rule it a RECORD) or the column is "
        "genuinely unread now and should be deleted"
    )


def test_the_record_columns_are_a_stated_minority():
    """A registry where everything is a RECORD proves nothing. This asserts
    the seam is mostly load-bearing and names the exceptions, so the ruling
    set is a reviewed diff rather than a growing shrug."""
    records = {
        column
        for column, entry in SEAM_COLUMN_READERS.items()
        if isinstance(entry, RECORD)
    }
    assert records == {
        "promise_sets.consent_id",
        "promise_sets.created_at",
        "routes.options_json",
        "routes.created_at",
        "binding_epochs.observed_json",
        "binding_epochs.created_at",
        "route_violations.promise_set_id",
        "route_violations.observed_json",
    }, sorted(records)
    assert len(records) * 3 < len(SEAM_COLUMN_READERS)
