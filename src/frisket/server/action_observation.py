"""Optional deployment observation of completed action dispatch, never authority."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable


@dataclass(frozen=True)
class ActionObservation:
    project_id: str
    action_kind: str
    receipt_id: str
    run_id: int | None
    job_id: int | None
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: float | None
    replayed: bool
    total_rows: int | None
    completed_rows: int | None
    failed_rows: int | None


ActionObserver = Callable[[ActionObservation, Any], None]
