from __future__ import annotations

from frisket.engine.jobs.queue_migrations import _grant_runtime_privileges
from frisket.engine.jobs.queue_provision import RUN_QUEUE_RUNTIME_ROLE


class _StatementRecorder:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: object) -> None:
        self.statements.append(str(statement))


def test_runtime_role_sequence_grant_is_read_only_backup_capable() -> None:
    connection = _StatementRecorder()

    _grant_runtime_privileges(connection)

    sequence_grants = [
        statement
        for statement in connection.statements
        if " ON ALL SEQUENCES IN SCHEMA " in statement
    ]
    assert sequence_grants == [
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "
        f"{RUN_QUEUE_RUNTIME_ROLE}"
    ]
