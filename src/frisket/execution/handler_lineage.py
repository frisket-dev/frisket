"""Record the exact queue handler that starts an execution attempt.

This task-local scope carries recovery identity and its borrowed queue. It does not grant model
or publication permission; those still require the normal attempt/claim fences.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class HandlerLineage:
    job_id: int
    authority_id: str
    queue: Any = field(default=None, repr=False, compare=False)


_current: ContextVar[HandlerLineage | None] = ContextVar(
    "handler_lineage", default=None
)


@contextmanager
def record_handler_lineage(*, job_id: int, authority_id: str, queue: Any = None):
    """Enter only at the worker's trusted claimed-handler boundary."""
    if type(job_id) is not int or job_id < 1 or not authority_id:
        raise ValueError("handler lineage requires its claimed job and authority")
    token = _current.set(HandlerLineage(job_id, authority_id, queue))
    try:
        yield
    finally:
        _current.reset(token)


def current_handler_lineage() -> HandlerLineage | None:
    return _current.get()
