"""Execution route data layer.

Covers: idempotent marker-pattern migrations (fresh + legacy bundle), the
chain-write CAS protocol (thread race: one winner per seq, losers reuse an
equivalent successor or get ChainConflictError and re-append — linear chain
either way), same-subject route<->promise-set integrity, typed subject
accessors, epoch dedupe by (route_id, provenance_hash) under concurrency,
violation dedupe, once-only standing cost consent, atomic consent-bound
successor coherence, observe_binding_divergence on a caller-owned transaction, and strict
refusal of retired snapshot/promise row shapes.
"""

from __future__ import annotations

import sqlite3
import threading
from functools import partial

import pytest

from frisket.engine.store import Project
from frisket.engine.store.execution_routes import (
    STANDING_COST_POLICY,
    ChainConflictError,
    ConsentRegistry,
    RouteStore,
    RouteStoreError,
    SubjectIntegrityError,
    ensure_standing_cost_consent,
    instance_principal,
    observe_binding_divergence,
    standing_cost_threshold,
)
from frisket.execution.promises import content_hash
from tests.deterministic_time import controlled_time

ROUTE_TABLES = {
    "consents",
    "promise_sets",
    "routes",
    "binding_epochs",
    "route_violations",
}

ROUTE_INDEXES = {
    "idx_consents_subject",
    "idx_consents_subject_hash",
    "idx_route_violations_route",
    "idx_model_calls_epoch",
}

PROMISES = [
    {
        "field": "egress_class",
        "op": "eq",
        "value": "none",
        "basis": None,
        "order_ref": None,
        "audience": "user_claim",
    }
]

SNAPSHOT = {
    "target_id": "local",
    "capability": "transcribe",
    "transport": "local",
    "run_scoped": False,
}


def _tables(db) -> set[str]:
    return {
        r["name"]
        for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _indexes(db) -> set[str]:
    return {
        r["name"]
        for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }


def _cols(db, table: str) -> set[str]:
    return {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "t.frisket", name="t")
    yield p
    p.close()


def _seed_head(store: RouteStore):
    promise_set = store.append_promise_set(promises=PROMISES, predecessor_id=None)
    route = store.append_route(
        promise_set_id=promise_set.id,
        engine="faster_whisper",
        options={"size": "base"},
        target_snapshot=SNAPSHOT,
        operator="self",
        egress_class="none",
        region=None,
        credential_source="none",
        cost_posture="operator_borne",
        predecessor_id=None,
    )
    return promise_set, route


def _routes(project, store: RouteStore) -> list[sqlite3.Row]:
    """The subject's route chain, oldest first.

    ``RouteStore.routes()`` was deleted because production reads the head,
    never the whole chain, so the chain-SHAPE
    assertions below read the table directly.
    """
    kind, ident = store.subject
    return project.db.execute(
        "SELECT * FROM routes WHERE subject_kind=? AND subject_id=? ORDER BY seq",
        (kind, ident),
    ).fetchall()


class TestMigrations:
    def test_fresh_bundle_has_tables_indexes_and_epoch_column(self, project):
        assert ROUTE_TABLES <= _tables(project.db)
        assert ROUTE_INDEXES <= _indexes(project.db)
        assert "epoch_id" in _cols(project.db, "model_calls")

    def test_reopen_twice_is_idempotent(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        p.close()
        for _ in range(2):
            p = Project(tmp_path / "t.frisket")
            p.close()
        p = Project(tmp_path / "t.frisket")
        try:
            # Tables exist exactly once (sqlite_master would reject dupes,
            # but assert the migration never tries a non-IF-NOT-EXISTS path).
            for table in sorted(ROUTE_TABLES):
                count = p.db.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()[0]
                assert count == 1, table
            assert ROUTE_INDEXES <= _indexes(p.db)
        finally:
            p.close()

    def test_consents_standing_per_action_xor_check(self, project):
        # Both NULL and both set violate the CHECK.
        for standing, action_hash in ((None, None), ("p", "h")):
            with pytest.raises(sqlite3.IntegrityError):
                project.db.execute(
                    "INSERT INTO consents (id, subject_kind, subject_id, "
                    "action_identity_hash, promise_set_hash, standing_policy, "
                    "actor, granted_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        "consent_x" + str(standing),
                        None,
                        None,
                        action_hash,
                        None,
                        standing,
                        "deployment:test",
                        "2026-07-24T00:00:00+00:00",
                    ),
                )
            project.db.rollback()


class TestTypedAccessors:
    def test_for_run_requires_int(self, project):
        with pytest.raises(TypeError):
            RouteStore.for_run(project, "7")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            RouteStore.for_run(project, True)  # type: ignore[arg-type]
        assert RouteStore.for_run(project, 7).subject == ("run", "7")

    def test_for_receipt_requires_nonempty_str(self, project):
        with pytest.raises(TypeError):
            RouteStore.for_receipt(project, 7)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            RouteStore.for_receipt(project, "")
        assert RouteStore.for_receipt(project, "rcpt_1").subject == (
            "receipt",
            "rcpt_1",
        )

    def test_subjects_are_isolated(self, project):
        run_store = RouteStore.for_run(project, 7)
        _seed_head(run_store)
        assert RouteStore.for_receipt(project, "rcpt_1").head() is None
        assert RouteStore.for_run(project, 8).head() is None
        assert run_store.head() is not None


class TestChainCAS:
    def test_concurrent_equivalent_successors_dedupe(self, project):
        store = RouteStore.for_run(project, 1)
        promise_set, head_route = _seed_head(store)
        barrier = threading.Barrier(2)
        results: list = [None, None]

        def append(slot: int) -> None:
            barrier.wait()
            results[slot] = store.append_route(
                promise_set_id=promise_set.id,
                engine="faster_whisper",
                options={"size": "base"},
                target_snapshot=SNAPSHOT,
                operator="self",
                egress_class="none",
                region=None,
                credential_source="platform_metered_key",  # same facts both
                cost_posture="platform_metered",
                predecessor_id=head_route.id,
            )

        with controlled_time() as clock:
            threads = [clock.background(partial(append, i)) for i in range(2)]
            for thread in threads:
                thread.join(timeout=30)
        assert results[0] is not None and results[1] is not None
        # One row wins seq 2; the loser REUSES it (successor dedupe).
        assert results[0].id == results[1].id
        seqs = [r["seq"] for r in _routes(project, store)]
        assert seqs == [1, 2]

    def test_concurrent_distinct_successors_one_wins_loser_retries(self, project):
        store = RouteStore.for_run(project, 1)
        promise_set, head_route = _seed_head(store)
        barrier = threading.Barrier(2)
        outcomes: list = [None, None]

        def append(slot: int, credential: str) -> None:
            barrier.wait()
            predecessor = head_route.id
            while True:
                try:
                    outcomes[slot] = store.append_route(
                        promise_set_id=promise_set.id,
                        engine="faster_whisper",
                        options={"size": "base"},
                        target_snapshot=SNAPSHOT,
                        operator="self",
                        egress_class="none",
                        region=None,
                        credential_source=credential,
                        cost_posture="platform_metered",
                        predecessor_id=predecessor,
                    )
                    return
                except ChainConflictError:
                    # Loser: re-read the head and re-append behind it.
                    head = store.head()
                    assert head is not None
                    predecessor = head[0].id

        with controlled_time() as clock:
            threads = [
                clock.background(partial(append, 0, "org_key")),
                clock.background(partial(append, 1, "platform_metered_key")),
            ]
            for thread in threads:
                thread.join(timeout=30)
        routes = _routes(project, store)
        # Linear chain: contiguous seqs, each row's predecessor is the
        # previous row, no forks.
        assert [r["seq"] for r in routes] == [1, 2, 3]
        assert routes[1]["predecessor_id"] == routes[0]["id"]
        assert routes[2]["predecessor_id"] in {routes[0]["id"], routes[1]["id"]}
        assert {routes[1]["credential_source"], routes[2]["credential_source"]} == {
            "org_key",
            "platform_metered_key",
        }

    def test_stale_predecessor_is_chain_conflict(self, project):
        store = RouteStore.for_run(project, 1)
        set_one = store.append_promise_set(promises=PROMISES, predecessor_id=None)
        store.append_promise_set(promises=PROMISES, predecessor_id=set_one.id)
        with pytest.raises(ChainConflictError):
            store.append_promise_set(promises=PROMISES, predecessor_id=set_one.id)
        with pytest.raises(ChainConflictError):
            store.append_promise_set(promises=PROMISES, predecessor_id=None)

    def test_concurrent_promise_set_appends_stay_linear(self, project):
        store = RouteStore.for_run(project, 1)
        base = store.append_promise_set(promises=PROMISES, predecessor_id=None)
        barrier = threading.Barrier(2)

        def append() -> None:
            barrier.wait()
            predecessor = base.id
            while True:
                try:
                    store.append_promise_set(
                        promises=PROMISES, predecessor_id=predecessor
                    )
                    return
                except ChainConflictError:
                    head = store._head_promise_set(project.db)
                    predecessor = head["id"]

        with controlled_time() as clock:
            threads = [clock.background(append) for _ in range(2)]
            for thread in threads:
                thread.join(timeout=30)
        assert [s.seq for s in store.promise_sets()] == [1, 2, 3]


class TestSubjectIntegrity:
    def test_route_refuses_other_subjects_promise_set(self, project):
        run_store = RouteStore.for_run(project, 1)
        promise_set, _ = _seed_head(run_store)
        other = RouteStore.for_run(project, 2)
        with pytest.raises(SubjectIntegrityError):
            other.append_route(
                promise_set_id=promise_set.id,
                engine="faster_whisper",
                options={},
                target_snapshot=SNAPSHOT,
                operator="self",
                egress_class="none",
                region=None,
                credential_source="none",
                cost_posture="operator_borne",
                predecessor_id=None,
            )
        assert _routes(project, other) == []

    def test_route_refuses_unknown_promise_set(self, project):
        with pytest.raises(SubjectIntegrityError):
            RouteStore.for_run(project, 1).append_route(
                promise_set_id="pset_missing",
                engine="faster_whisper",
                options={},
                target_snapshot=SNAPSHOT,
                operator="self",
                egress_class="none",
                region=None,
                credential_source="none",
                cost_posture="operator_borne",
                predecessor_id=None,
            )


class TestEpochsAndViolations:
    def test_epoch_dedupe_by_provenance_hash_under_concurrency(self, project):
        """Dedupe on ``(route_id, provenance_hash)``, driven through the ONE
        production writer (``observe_binding_divergence`` on a caller-owned
        transaction) now that ``RouteStore.open_epoch`` — a wrapper with zero
        src callers — is gone."""
        store = RouteStore.for_run(project, 1)
        _, route = _seed_head(store)
        barrier = threading.Barrier(4)
        results: list = [None] * 4
        # Observation-only facts never diverge the route, so all four writers
        # open an epoch under the SAME head.
        observed = {"revision": "abc", "device": "cpu", "dtype": "int8"}

        def open_epoch(slot: int) -> None:
            db = sqlite3.connect(project.db_path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                barrier.wait()
                db.execute("BEGIN IMMEDIATE")
                results[slot] = observe_binding_divergence(db, route, observed)
                db.commit()
            finally:
                db.close()

        with controlled_time() as clock:
            threads = [clock.background(partial(open_epoch, i)) for i in range(4)]
            for thread in threads:
                thread.join(timeout=30)
        assert len({r[1] for r in results}) == 1
        count = project.db.execute(
            "SELECT COUNT(*) FROM binding_epochs WHERE route_id=?", (route.id,)
        ).fetchone()[0]
        assert count == 1
        # Distinct provenance opens a distinct epoch.
        db = project.db
        db.execute("BEGIN IMMEDIATE")
        _, other_epoch = observe_binding_divergence(
            db, route, {"revision": "def", "device": "cpu"}
        )
        db.commit()
        assert other_epoch != results[0][1]

    def test_violation_dedupe(self, project):
        store = RouteStore.for_run(project, 1)
        promise_set, route = _seed_head(store)
        observed = {"egress_class": "third_party_api"}
        first = store.record_violation(
            route_id=route.id,
            promise_set_id=promise_set.id,
            promise=PROMISES[0],
            observed=observed,
        )
        second = store.record_violation(
            route_id=route.id,
            promise_set_id=promise_set.id,
            promise=PROMISES[0],
            observed=observed,
        )
        assert first.id == second.id
        assert len(store.violations()) == 1
        # Different observed truth is a distinct violation.
        third = store.record_violation(
            route_id=route.id,
            promise_set_id=promise_set.id,
            promise=PROMISES[0],
            observed={"egress_class": "operator_lan"},
        )
        assert third.id != first.id
        assert len(store.violations()) == 2

    def test_identical_violation_on_another_run_lands_in_its_own_ledger(self, project):
        """``dedupe_key`` is UNIQUE across the whole table, so keying it on
        (promise fingerprint, observed hash) ALONE made run B's
        structurally identical violation return run A's row — and since
        ``violations()`` joins through ``routes``, B's ledger read EMPTY. A
        truth-recording surface silently lost truth, and the more identical
        the fleet's failures the more of them vanished. The subject is part
        of the identity of the event."""
        store_a = RouteStore.for_run(project, 1)
        set_a, route_a = _seed_head(store_a)
        store_b = RouteStore.for_run(project, 2)
        set_b, route_b = _seed_head(store_b)
        observed = {"egress_class": "third_party_api"}

        violation_a = store_a.record_violation(
            route_id=route_a.id,
            promise_set_id=set_a.id,
            promise=PROMISES[0],
            observed=observed,
        )
        violation_b = store_b.record_violation(
            route_id=route_b.id,
            promise_set_id=set_b.id,
            promise=PROMISES[0],  # byte-identical promise AND observed facts
            observed=observed,
        )

        assert violation_b.id != violation_a.id
        assert violation_b.route_id == route_b.id
        assert [v.id for v in store_a.violations()] == [violation_a.id]
        assert [v.id for v in store_b.violations()] == [violation_b.id]
        # Dedupe still holds WITHIN each subject.
        assert (
            store_b.record_violation(
                route_id=route_b.id,
                promise_set_id=set_b.id,
                promise=PROMISES[0],
                observed=observed,
            ).id
            == violation_b.id
        )
        assert len(store_b.violations()) == 1


class TestStandingConsent:
    def test_minted_once_with_deployment_actor(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        try:
            standing = ConsentRegistry(p).standing_consents()
            assert len(standing) == 1
            consent = standing[0]
            assert consent.standing_policy == STANDING_COST_POLICY
            assert consent.actor.startswith("deployment:")
            assert consent.subject_kind is None
            assert consent.action_identity_hash is None
            actor = consent.actor
        finally:
            p.close()
        # Stable across reopens: same single row, same principal.
        p = Project(tmp_path / "t.frisket")
        try:
            standing = ConsentRegistry(p).standing_consents()
            assert len(standing) == 1
            assert standing[0].actor == actor
            assert p.get_meta("instance_principal") == actor
        finally:
            p.close()


class TestConsentAdvanceHeadCoherence:
    def test_successor_set_and_route_land_in_one_transaction(self, project):
        store = RouteStore.for_run(project, 1)
        set_one, route_one = _seed_head(store)
        db = project.db
        db.execute("BEGIN IMMEDIATE")
        try:
            consent = store.record_consent(
                action_identity_hash="a" * 64,
                promise_set_hash="b" * 64,
                actor="deployment:test",
                txn=db,
            )
            set_two = store.append_promise_set(
                promises=PROMISES,
                predecessor_id=set_one.id,
                consent_id=consent.id,
                txn=db,
            )
            route_two = store.append_route(
                promise_set_id=set_two.id,
                engine="faster_whisper",
                options={"size": "base"},
                target_snapshot=SNAPSHOT,
                operator="self",
                egress_class="none",
                region=None,
                credential_source="org_key",
                cost_posture="org_key",
                predecessor_id=route_one.id,
                txn=db,
            )
        except BaseException:
            db.rollback()
            raise
        db.commit()
        head = store.head()
        assert head is not None
        head_route, head_set = head
        assert head_route.id == route_two.id
        assert head_set.id == set_two.id
        assert head_route.promise_set_id == head_set.id
        assert head_set.consent_id == consent.id

    def test_rollback_leaves_prior_head(self, project):
        store = RouteStore.for_run(project, 1)
        set_one, route_one = _seed_head(store)
        db = project.db
        db.execute("BEGIN IMMEDIATE")
        set_two = store.append_promise_set(
            promises=PROMISES, predecessor_id=set_one.id, txn=db
        )
        store.append_route(
            promise_set_id=set_two.id,
            engine="faster_whisper",
            options={},
            target_snapshot=SNAPSHOT,
            operator="self",
            egress_class="none",
            region=None,
            credential_source="org_key",
            cost_posture="org_key",
            predecessor_id=route_one.id,
            txn=db,
        )
        db.rollback()
        head = store.head()
        assert head is not None
        assert head[0].id == route_one.id
        assert head[1].id == set_one.id


class TestObserveBindingDivergence:
    def test_no_divergence_returns_same_route_and_deduped_epoch(self, project):
        store = RouteStore.for_run(project, 1)
        _, route = _seed_head(store)
        observed = {
            "credential_source": "none",
            "device": "cpu",
            "revision": "abc",
        }
        db = project.db
        db.execute("BEGIN IMMEDIATE")
        route_id, epoch_id = observe_binding_divergence(db, route, observed)
        db.commit()
        assert route_id == route.id
        db.execute("BEGIN IMMEDIATE")
        route_id2, epoch_id2 = observe_binding_divergence(db, route, observed)
        db.commit()
        assert (route_id2, epoch_id2) == (route_id, epoch_id)

    def test_divergence_appends_successor_epoch_and_violation(self, project):
        store = RouteStore.for_run(project, 1)
        promise_set, route = _seed_head(store)
        observed = {"egress_class": "third_party_api", "device": "cuda"}
        db = project.db
        db.execute("BEGIN IMMEDIATE")
        successor_id, epoch_id = observe_binding_divergence(db, route, observed)
        db.commit()
        assert successor_id != route.id
        routes = _routes(project, store)
        assert [r["seq"] for r in routes] == [1, 2]
        successor = routes[1]
        assert successor["id"] == successor_id
        assert successor["predecessor_id"] == route.id
        assert successor["promise_set_id"] == promise_set.id  # same set
        assert successor["egress_class"] == "third_party_api"
        assert successor["credential_source"] == route.credential_source
        epoch_route = project.db.execute(
            "SELECT route_id FROM binding_epochs WHERE id=?", (epoch_id,)
        ).fetchone()[0]
        assert epoch_route == successor_id  # epoch under the successor
        violations = store.violations()
        assert len(violations) == 1
        assert violations[0].route_id == route.id
        # Re-observation dedupes everything (successor, epoch, violation).
        db.execute("BEGIN IMMEDIATE")
        successor_id2, epoch_id2 = observe_binding_divergence(db, route, observed)
        db.commit()
        assert (successor_id2, epoch_id2) == (successor_id, epoch_id)
        assert [r["seq"] for r in _routes(project, store)] == [1, 2]
        assert len(store.violations()) == 1


class TestCurrentSchemaRefusal:
    def test_old_target_snapshot_is_refused(self, project):
        store = RouteStore.for_run(project, 42)
        _promise_set, route = _seed_head(store)
        project.db.execute(
            "UPDATE routes SET target_snapshot_json=? WHERE id=?",
            ('{"resolver":"static-v1","target_id":"local"}', route.id),
        )
        project.db.commit()

        with pytest.raises(ValueError, match="target snapshot must contain exactly"):
            store.head()

    def test_current_promise_rows_with_stale_hash_are_refused(self, project):
        store = RouteStore.for_run(project, 42)
        promise_set, _route = _seed_head(store)
        project.db.execute(
            "UPDATE promise_sets SET promise_set_hash=? WHERE id=?",
            ("f" * 64, promise_set.id),
        )
        project.db.commit()

        with pytest.raises(
            RouteStoreError,
            match="persisted promise-set hash does not match its row material",
        ):
            store.head()

    def test_bundle_carrying_the_retired_columns_still_opens(self, tmp_path):
        """``promise_sets.coverage_envelope_json`` and
        ``promise_sets.resume_enqueue_state`` are absent from the canonical
        SCHEMA. The migration posture is additive (ADD COLUMN guards +
        a CREATE ... IF NOT EXISTS replay), so a bundle written before either
        deletion keeps the column — it must simply go UNREAD: the replay is
        a no-op against the existing table, appends that no longer name the
        column leave it NULL, and every reader still decodes."""
        path = tmp_path / "legacy.frisket"
        p = Project.create(path, name="legacy")
        p.db.execute("ALTER TABLE promise_sets ADD COLUMN coverage_envelope_json TEXT")
        p.db.execute("ALTER TABLE promise_sets ADD COLUMN resume_enqueue_state TEXT")
        p.db.commit()
        p.close()

        p = Project(path)  # runs open-time migrations against the wider table
        try:
            store = RouteStore.for_run(p, 7)
            promise_set = store.append_promise_set(
                promises=PROMISES, predecessor_id=None
            )
            assert store.head() is None  # no route yet; the set decoded fine
            assert promise_set.promises == PROMISES
            assert not hasattr(promise_set, "coverage_envelope")
            assert not hasattr(promise_set, "resume_enqueue_state")
            leftover = p.db.execute(
                "SELECT coverage_envelope_json, resume_enqueue_state "
                "FROM promise_sets WHERE id=?",
                (promise_set.id,),
            ).fetchone()
            # written by nothing, read by nothing
            assert tuple(leftover) == (None, None)
        finally:
            p.close()


class TestCanonicalHash:
    """F3/F10: the store has NO local hash implementation — it imports the
    one family-registry helper. These pin the strict rules through the
    store's re-exported constructions."""

    def test_rejects_floats(self):
        with pytest.raises(ValueError):
            content_hash("frisket.test.v1", {"cost": 0.5})

    def test_key_order_insensitive_and_domain_prefixed(self):
        a = content_hash("frisket.test.v1", {"a": 1, "b": "x"})
        b = content_hash("frisket.test.v1", {"b": "x", "a": 1})
        # Domain separation, asserted between two REGISTERED families —
        # "frisket.other.v1" used to be minted on the spot by this very line
        # (E-2: content_hash now refuses unregistered domains).
        c = content_hash("frisket.promise_row.v1", {"a": 1, "b": "x"})
        assert a == b
        assert a != c

    def test_store_hashes_are_the_shared_implementation(self):
        from frisket.engine.store.execution_routes import (
            promise_fingerprint as store_fingerprint,
            promise_set_hash as store_set_hash,
        )
        from frisket.execution.promises import (
            promise_row_fingerprint,
            promise_rows_hash,
        )

        assert store_set_hash(PROMISES) == promise_rows_hash(PROMISES)
        assert store_fingerprint(PROMISES[0]) == promise_row_fingerprint(PROMISES[0])


class TestStaleObserverCannotDisplaceAdvancedHead:
    """F1 (the initial schema): a delayed observer still holding a stale route can never
    displace a head the consent chain has advanced past — observation
    re-reads the head under the caller's transaction and re-evaluates
    against it."""

    def _advance_consent(self, project, store, set_one, route_one):
        db = project.db
        db.execute("BEGIN IMMEDIATE")
        try:
            set_two = store.append_promise_set(
                promises=PROMISES, predecessor_id=set_one.id, txn=db
            )
            route_two = store.append_route(
                promise_set_id=set_two.id,
                engine="faster_whisper",
                options={"size": "base"},
                target_snapshot=SNAPSHOT,
                operator="self",
                egress_class="none",
                region=None,
                credential_source="org_key",
                cost_posture="org_key",
                predecessor_id=route_one.id,
                txn=db,
            )
        except BaseException:
            db.rollback()
            raise
        db.commit()
        return set_two, route_two

    def test_matching_observation_reuses_current_head(self, project):
        store = RouteStore.for_run(project, 1)
        set_one, route_one = _seed_head(store)
        set_two, route_two = self._advance_consent(project, store, set_one, route_one)
        # The stale observer reports facts that MATCH the consent-bound head:
        # reuse — no append, epoch under the current head.
        db = project.db
        db.execute("BEGIN IMMEDIATE")
        observed = {"credential_source": "org_key", "device": "cpu"}
        route_id, epoch_id = observe_binding_divergence(db, route_one, observed)
        db.commit()
        assert route_id == route_two.id
        head_route, head_set = store.head()
        assert head_route.id == route_two.id
        assert head_set.id == set_two.id  # the advanced pair still heads
        assert [r["seq"] for r in _routes(project, store)] == [1, 2]

    def test_diverging_observation_appends_after_current_head(self, project):
        store = RouteStore.for_run(project, 1)
        set_one, route_one = _seed_head(store)
        set_two, route_two = self._advance_consent(project, store, set_one, route_one)
        # The stale observer reports facts diverging from the CURRENT head:
        # the successor descends from the head (never from the stale route)
        # and keeps the head's consent-bound promise set.
        db = project.db
        db.execute("BEGIN IMMEDIATE")
        # ``egress_class`` diverges, and the seeded promise set carries an
        # egress_class row — so this exercises a REAL compiled promise, not
        # the implicit eq-promise the store used to synthesize from the route
        # row's own fact column; implicit promises are no longer synthesized.
        observed = {"egress_class": "third_party_api", "device": "cpu"}
        successor_id, _epoch_id = observe_binding_divergence(db, route_one, observed)
        db.commit()
        routes = _routes(project, store)
        assert [r["seq"] for r in routes] == [1, 2, 3]
        successor = routes[2]
        assert successor["id"] == successor_id
        assert successor["predecessor_id"] == route_two.id  # NOT route_one
        assert successor["promise_set_id"] == set_two.id  # current set kept
        # The violation is recorded against the head's promise set.
        violations = store.violations()
        assert violations and violations[0].promise_set_id == set_two.id

    def test_a_diverged_fact_no_promise_covers_records_without_a_violation(
        self, project
    ):
        """Only a COMPILED promise row can be violated.

        The seeded set promises ``egress_class`` and nothing else, so an
        observed ``credential_source`` that diverges from the pin still gets
        its successor route and its epoch — the divergence is recorded — but
        no ``route_violations`` row, because there is no claim to have
        broken. The store used to fabricate an eq-promise out of the route
        row's own fact column, which fingerprinted against nothing any reader
        could find in ``promise_sets``.
        """
        store = RouteStore.for_run(project, 1)
        set_one, route_one = _seed_head(store)
        assert [p["field"] for p in set_one.promises] == ["egress_class"]
        db = project.db
        db.execute("BEGIN IMMEDIATE")
        observed = {"credential_source": "platform_key"}
        successor_id, epoch_id = observe_binding_divergence(db, route_one, observed)
        db.commit()
        routes = _routes(project, store)
        assert [r["seq"] for r in routes] == [1, 2]
        assert routes[1]["id"] == successor_id
        assert routes[1]["credential_source"] == "platform_key"  # truth corrected
        assert epoch_id is not None
        assert store.violations() == []


class TestStandingConsentPolicyParams:
    """F2: standing authority is reproducible from persisted artifacts."""

    def test_mint_records_threshold_and_content_identity(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        try:
            standing = ConsentRegistry(p).standing_consents()
            assert len(standing) == 1
            params = standing[0].policy_params
            assert params["currency"] == "USD"
            assert params["threshold_usd"] == "2"  # the documented default
            assert len(params["policy_content_id"]) == 64
            principal = instance_principal(p)
            assert standing_cost_threshold(standing, principal=principal) is not None
        finally:
            p.close()

    def test_knob_change_mints_successor_and_keeps_history(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FRISKET_COST_CONSENT_USD", raising=False)
        p = Project.create(tmp_path / "t.frisket", name="t")
        p.close()
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "5")
        p = Project(tmp_path / "t.frisket")  # open reconciles (mint input)
        try:
            standing = ConsentRegistry(p).standing_consents()
            assert len(standing) == 2  # successor + auditable history
            principal = instance_principal(p)
            assert standing_cost_threshold(standing, principal=principal) == 5
            thresholds = sorted(c.policy_params["threshold_usd"] for c in standing)
            assert thresholds == ["2", "5"]
        finally:
            p.close()

    def test_reopen_without_change_mints_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "2")
        p = Project.create(tmp_path / "t.frisket", name="t")
        p.close()
        for _ in range(3):
            p = Project(tmp_path / "t.frisket")
            p.close()
        p = Project(tmp_path / "t.frisket")
        try:
            assert len(ConsentRegistry(p).standing_consents()) == 1
        finally:
            p.close()

    def test_reconcile_entry_point_is_callable_directly(self, project, monkeypatch):
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
        minted = ensure_standing_cost_consent(project)
        assert minted.policy_params["threshold_usd"] == "0"
        principal = instance_principal(project)
        standing = ConsentRegistry(project).standing_consents()
        assert standing_cost_threshold(standing, principal=principal) == 0

    @pytest.mark.parametrize("raw", ["nan", "NaN", "Infinity", "-Infinity", "-1"])
    def test_non_finite_or_negative_knob_mints_zero_coverage(
        self, project, monkeypatch, raw
    ):
        """``NaN``/``Infinity`` PARSE cleanly as Decimals but state no
        threshold. Treating them as the $1 DEFAULT would let a garbage
        (or hostile) env value silently minted a dollar of standing coverage.
        A knob that states no threshold now states ZERO."""
        from frisket.engine.store.execution_routes import standing_cost_threshold_env

        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", raw)
        assert standing_cost_threshold_env() == 0
        minted = ensure_standing_cost_consent(project)
        assert minted.policy_params["threshold_usd"] == "0"

    def test_absent_knob_still_falls_back_to_the_documented_default(self, monkeypatch):
        """Absence means "the operator did not configure this", which is what
        the default is for — distinct from a knob that states nonsense."""
        from frisket.engine.store.execution_routes import standing_cost_threshold_env

        monkeypatch.delenv("FRISKET_COST_CONSENT_USD", raising=False)
        assert standing_cost_threshold_env() == 2

    def test_reconcile_refuses_inside_a_caller_owned_transaction(self, project):
        """This function ends with an unconditional ``db.commit()``. Inside
        someone else's open transaction that would durably commit
        THEIR partial writes as a side effect of reconciling a policy row —
        so it refuses rather than papering over the misuse."""
        from frisket.engine.store.execution_routes import RouteStoreError

        db = project.db
        db.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(RouteStoreError, match="idle connection"):
                ensure_standing_cost_consent(project)
        finally:
            db.rollback()


class TestInstallationPrincipal:
    """F5: the principal lives OUTSIDE the bundle; bundle meta is a cache
    that loses when it disagrees."""

    def test_minted_once_at_the_workspace_root(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        p = Project.create(ws / "a.frisket", name="a")
        q = Project.create(ws / "b.frisket", name="b")
        try:
            assert instance_principal(p) == instance_principal(q)
            assert (ws / "installation_id").is_file()
        finally:
            p.close()
            q.close()

    def test_restored_bundle_gets_the_new_installs_principal(self, tmp_path):
        import shutil

        ws_one = tmp_path / "one"
        ws_two = tmp_path / "two"
        ws_one.mkdir()
        ws_two.mkdir()
        p = Project.create(ws_one / "a.frisket", name="a")
        original = instance_principal(p)
        p.close()
        shutil.copytree(ws_one / "a.frisket", ws_two / "a.frisket")
        restored = Project(ws_two / "a.frisket")
        try:
            migrated = instance_principal(restored)
            assert migrated != original  # the NEW install's principal
            # The cache follows the installation file, not the bundle.
            assert restored.get_meta("instance_principal") == migrated
            # ...and the open re-minted standing consent for this principal.
            standing = ConsentRegistry(restored).standing_consents()
            actors = {c.actor for c in standing}
            assert migrated in actors
            assert standing_cost_threshold(standing, principal=migrated) is not None
        finally:
            restored.close()

    def test_foreign_standing_consent_never_covers(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        try:
            standing = ConsentRegistry(p).standing_consents()
            assert (
                standing_cost_threshold(standing, principal="deployment:someone-else")
                is None
            )
        finally:
            p.close()
