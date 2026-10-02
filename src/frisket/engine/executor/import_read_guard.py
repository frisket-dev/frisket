"""Action-read refusal for sheets with unfinished progressive imports."""

from __future__ import annotations

from typing import Any

from frisket.actions.types import TableError
from frisket.engine.store.import_sessions import active_import_session


def require_action_sheet_readable(project: Any, sheet_id: int) -> None:
    session = active_import_session(project.db, int(sheet_id))
    if session is not None:
        raise TableError(
            "import_in_progress",
            "Actions are unavailable while this sheet is importing.",
            details={
                "sheet_id": int(sheet_id),
                "session_id": session.id,
                "state": session.state,
            },
        )


__all__ = ["require_action_sheet_readable"]
