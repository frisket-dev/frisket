"""Transaction boundary and small identity stores for the open team server."""

from __future__ import annotations

from types import TracebackType

import sqlalchemy as sa

from frisket.team.identity_store import IdentityStore
from frisket.team.schema import record_audit


class AuditStore:
    def __init__(self, cx: sa.Connection):
        self.cx = cx

    def append(
        self,
        *,
        user_id: int | None,
        organization_id: int | None,
        action: str,
        detail: str = "",
    ) -> None:
        record_audit(
            self.cx,
            user_id=user_id,
            organization_id=organization_id,
            action=action,
            detail=detail,
        )


class TeamUnitOfWork:
    def __init__(self, engine: sa.Engine, *, organization_id: int):
        self.engine = engine
        self.organization_id = organization_id

    def __enter__(self) -> "TeamUnitOfWork":
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.auth = IdentityStore(
            self.connection,
            provision_organization_id=self.organization_id,
        )
        self.audit = AuditStore(self.connection)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self.transaction.commit() if exc_type is None else self.transaction.rollback()
        finally:
            self.connection.close()
