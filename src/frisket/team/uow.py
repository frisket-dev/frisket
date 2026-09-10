"""Transaction boundary and small identity stores for the open team server."""

from __future__ import annotations

from types import TracebackType

import sqlalchemy as sa

from frisket.team.identity_store import IdentityStore
from frisket.team.schema import audit_log


class AuditStore:
    def __init__(self, cx: sa.Connection):
        self.cx = cx

    def append(
        self, *, user_id: int | None, org_id: int | None, action: str, detail: str = ""
    ) -> None:
        self.cx.execute(
            audit_log.insert().values(
                user_id=user_id, org_id=org_id, action=action, detail=detail
            )
        )


class TeamUnitOfWork:
    def __init__(self, engine: sa.Engine, *, org_id: int):
        self.engine = engine
        self.org_id = org_id

    def __enter__(self) -> "TeamUnitOfWork":
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.auth = IdentityStore(
            self.connection,
            provision_org_id=self.org_id,
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
