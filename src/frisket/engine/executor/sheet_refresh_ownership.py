"""One target lease for deterministic and model-backed derived refresh writers."""

from datetime import datetime, timedelta, timezone
from frisket.actions.types import TableError
from frisket.engine.store.leases import lease_held


def require_refresh_target(project, sheet_id, parent_op_id, *, claim_token=None):
    target = project.db.execute(
        "SELECT s.parent_op_id,s.refresh_claim_token,s.refresh_lease_expires_at FROM sheets s "
        "JOIN sheets parent ON parent.id=s.parent_sheet_id AND parent.hidden=0 WHERE s.id=? AND s.hidden=0",
        (sheet_id,),
    ).fetchone()
    if target is None or target["parent_op_id"] != parent_op_id:
        raise TableError(
            "invalid_input_ref", "The refresh target or its source is unavailable"
        )
    if target["refresh_claim_token"] != claim_token and lease_held(
        target["refresh_claim_token"],
        target["refresh_lease_expires_at"],
        datetime.now(timezone.utc),
    ):
        raise TableError("sheet_refresh_busy", "Another refresh is already in progress")


def acquire_refresh_claim(project, *, sheet_id, parent_op_id, token):
    if not project.db.in_transaction:
        raise RuntimeError("refresh ownership requires the publication transaction")
    require_refresh_target(project, sheet_id, parent_op_id, claim_token=token)
    project.db.execute(
        "UPDATE sheets SET refresh_claim_token=?,refresh_lease_expires_at=? WHERE id=?",
        (
            token,
            (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            sheet_id,
        ),
    )


def require_refresh_writer(project, *, sheet_id, parent_op_id, op_id, token):
    from frisket.engine.store.changes import ChangeStore
    from frisket.engine.store.source_dependencies import require_execution_sources
    from frisket.engine.store.purge import require_publication_targets

    target = project.db.execute(
        "SELECT parent_op_id,refresh_claim_token,hidden FROM sheets WHERE id=?",
        (sheet_id,),
    ).fetchone()
    if (
        target is None
        or target["hidden"]
        or target["parent_op_id"] != parent_op_id
        or target["refresh_claim_token"] != token
    ):
        raise TableError("stale_refresh", "The refresh no longer owns its target")
    ChangeStore(project).start(op_id)
    ChangeStore(project).require_publication(op_id)
    require_execution_sources(project, op_id)
    require_publication_targets(project.db, sheet_id=sheet_id)
    project.db.execute(
        "UPDATE sheets SET refresh_lease_expires_at=? WHERE id=?",
        ((datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), sheet_id),
    )


def release_refresh_claim(project, sheet_id, token):
    project.db.execute(
        "UPDATE sheets SET refresh_claim_token=NULL,refresh_lease_expires_at=NULL WHERE id=? AND refresh_claim_token=?",
        (sheet_id, token),
    )
