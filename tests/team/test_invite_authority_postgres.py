"""Real-Postgres races around organization invitation authority.

These tests coordinate two real transactions with events rather than timing
guesses. They are deliberately gated: SQLite cannot prove PostgreSQL advisory
lock ordering or the visibility of the redeem/create interleaving.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from typing import Any

import pytest
import sqlalchemy as sa

from frisket.team.admin_browser_service import AdminMembershipService
from frisket.team.app import TeamProjectAccess
from frisket.team.identity_store import IdentityStore
from frisket.team.invite_service import InviteConflict, InviteForbidden, InviteService
from frisket.team.schema import audit_log, memberships, orgs, pending_invites, users


PG_URL = os.environ.get("FRISKET_PG_TEST_URL")
_TABLES = (orgs, users, memberships, pending_invites, audit_log)
_OWNER = {"id": 1, "email": "owner@example.com", "auth": "session"}


@pytest.fixture
def pg_invite_engine() -> Any:
    if not PG_URL:
        pytest.skip("FRISKET_PG_TEST_URL not set (real-postgres tests gated)")
    engine = sa.create_engine(PG_URL, future=True, pool_size=5, max_overflow=0)
    for table in reversed(_TABLES):
        table.drop(engine, checkfirst=True)
    for table in _TABLES:
        table.create(engine, checkfirst=True)
    try:
        yield engine
    finally:
        for table in reversed(_TABLES):
            table.drop(engine, checkfirst=True)
        engine.dispose()


def _seed_org(
    engine: sa.Engine, *, pending_email: str | None = None
) -> tuple[int, int, int]:
    with engine.begin() as cx:
        org_id = cx.execute(
            orgs.insert().values(name="Invite race").returning(orgs.c.id)
        ).scalar_one()
        owner_id = cx.execute(
            users.insert()
            .values(email="owner@example.com", default_org_id=org_id)
            .returning(users.c.id)
        ).scalar_one()
        co_owner_id = cx.execute(
            users.insert()
            .values(email="co-owner@example.com", default_org_id=org_id)
            .returning(users.c.id)
        ).scalar_one()
        cx.execute(
            memberships.insert(),
            [
                {"user_id": owner_id, "org_id": org_id, "role": "owner"},
                {"user_id": co_owner_id, "org_id": org_id, "role": "owner"},
            ],
        )
        if pending_email is not None:
            cx.execute(
                pending_invites.insert().values(
                    email=pending_email,
                    org_id=org_id,
                    role="member",
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
    return int(org_id), int(owner_id), int(co_owner_id)


def test_postgres_demotion_serializes_before_owner_invite_create(
    pg_invite_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued demotion wins before the same actor may create an invite."""
    import frisket.team.admin_browser_service as admin_module
    import frisket.team.invite_service as invite_module

    org_id, owner_id, co_owner_id = _seed_org(pg_invite_engine)
    membership = AdminMembershipService(pg_invite_engine, org_id=org_id)
    invites = InviteService(
        pg_invite_engine,
        org_id=org_id,
        access=TeamProjectAccess(pg_invite_engine, org_id),
    )
    demotion_precommit, allow_demotion_commit, create_waiting = (
        Event(),
        Event(),
        Event(),
    )
    demotion_done, create_done = Event(), Event()
    result: dict[str, Any] = {}
    original_membership_lock = admin_module.locked_transaction
    original_invite_lock = invite_module.locked_transaction

    @contextmanager
    def announce_demotion_lock(engine: sa.Engine, *, lock_scope: Any = None):
        with original_membership_lock(engine, lock_scope=lock_scope) as cx:
            try:
                yield cx
            finally:
                if lock_scope == ("org-membership", org_id):
                    demotion_precommit.set()
                    assert allow_demotion_commit.wait(5)

    @contextmanager
    def announce_invite_lock(engine: sa.Engine, *, lock_scope: Any = None):
        if lock_scope == ("org-membership", org_id):
            create_waiting.set()
        with original_invite_lock(engine, lock_scope=lock_scope) as cx:
            yield cx

    monkeypatch.setattr(admin_module, "locked_transaction", announce_demotion_lock)
    monkeypatch.setattr(invite_module, "locked_transaction", announce_invite_lock)

    def demote() -> None:
        try:
            membership.update_role(
                actor={**_OWNER, "id": co_owner_id}, user_id=owner_id, role="member"
            )
            result["demotion"] = "ok"
        except BaseException as exc:  # pragma: no cover - surfaced below
            result["demotion"] = exc
        finally:
            demotion_done.set()

    def create() -> None:
        try:
            invites.create_org_invite(
                actor_user_id=owner_id, email="queued@example.com", role="member"
            )
            result["create"] = "ok"
        except BaseException as exc:
            result["create"] = exc
        finally:
            create_done.set()

    # The demotion has changed the row but still holds the real advisory scope
    # before its commit. The create path now attempts that same PostgreSQL
    # lock; releasing the demotion makes it observe the committed role inside
    # its transaction, without relying on advisory-lock queue order or timing.
    demotion_thread = Thread(target=demote, name="demote-owner")
    demotion_thread.start()
    assert demotion_precommit.wait(5)
    create_thread = Thread(target=create, name="create-invite")
    create_thread.start()
    assert create_waiting.wait(5)
    allow_demotion_commit.set()
    demotion_thread.join(timeout=5)
    create_thread.join(timeout=5)
    assert demotion_done.is_set() and create_done.is_set()
    assert result["demotion"] == "ok"
    assert isinstance(result["create"], InviteForbidden)

    with pg_invite_engine.connect() as cx:
        assert (
            cx.execute(
                sa.select(memberships.c.role).where(memberships.c.user_id == owner_id)
            ).scalar_one()
            == "member"
        )
        assert (
            cx.execute(
                sa.select(sa.func.count())
                .select_from(pending_invites)
                .where(pending_invites.c.email == "queued@example.com")
            ).scalar_one()
            == 0
        )
        assert (
            cx.execute(
                sa.select(sa.func.count())
                .select_from(audit_log)
                .where(audit_log.c.action == "org_invite_set")
            ).scalar_one()
            == 0
        )


def test_postgres_claim_between_invite_check_and_upsert_rolls_back_shadow(
    pg_invite_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real redemption cannot leave a pending shadow after a re-issue."""
    import frisket.team.invite_service as invite_module

    email = "claim-race@example.com"
    org_id, owner_id, _ = _seed_org(pg_invite_engine, pending_email=email)
    invites = InviteService(
        pg_invite_engine,
        org_id=org_id,
        access=TeamProjectAccess(pg_invite_engine, org_id),
    )
    before_upsert, allow_upsert, create_done = Event(), Event(), Event()
    result: dict[str, Any] = {}
    original_upsert = invite_module.atomic_upsert

    def pause_before_upsert(*args: Any, **kwargs: Any) -> None:
        before_upsert.set()
        assert allow_upsert.wait(5)
        original_upsert(*args, **kwargs)

    monkeypatch.setattr(invite_module, "atomic_upsert", pause_before_upsert)

    def create() -> None:
        try:
            invites.create_org_invite(
                actor_user_id=owner_id, email=email, role="member"
            )
            result["create"] = "ok"
        except BaseException as exc:
            result["create"] = exc
        finally:
            create_done.set()

    creator = Thread(target=create, name="reissue-invite")
    creator.start()
    assert before_upsert.wait(5)
    # This is the ordinary identity claim path, in a separate real transaction.
    with pg_invite_engine.begin() as cx:
        claimed = IdentityStore(cx).ensure_user_for_login(email)
    assert claimed["email"] == email
    allow_upsert.set()
    creator.join(timeout=5)
    assert create_done.is_set()
    assert isinstance(result["create"], InviteConflict)
    assert result["create"].status_code == 409

    with pg_invite_engine.connect() as cx:
        assert (
            cx.execute(
                sa.select(sa.func.count())
                .select_from(
                    memberships.join(users, users.c.id == memberships.c.user_id)
                )
                .where(users.c.email == email, memberships.c.org_id == org_id)
            ).scalar_one()
            == 1
        )
        assert (
            cx.execute(
                sa.select(sa.func.count())
                .select_from(pending_invites)
                .where(pending_invites.c.email == email)
            ).scalar_one()
            == 0
        )
        assert (
            cx.execute(
                sa.select(sa.func.count())
                .select_from(audit_log)
                .where(
                    audit_log.c.action == "org_invite_set", audit_log.c.detail == email
                )
            ).scalar_one()
            == 0
        )
