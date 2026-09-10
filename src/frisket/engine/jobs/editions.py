"""Worker edition registration.

An EDITION is a worker composition: it decides which ports (credential /
admission / settlement — see `frisket.jobs.ports`) the open worker runs with.
The open team edition needs none of them beyond the open defaults, so it is
just `register_production_handlers`. Any other edition's ports are supplied
by an external composition package.

This tree must import, test and start with that external package PHYSICALLY
ABSENT, so it cannot name a
private module — not at import time, not lazily inside a function, not through
a literal `importlib.import_module(...)` naming a private path. Instead the
external edition REGISTERS itself under the `frisket.worker_editions`
entry-point group; explicit constructor ports and entry points preserve the
boundary without monkey-patching. This module resolves whatever is registered.

Consequence, and it is the intended one: an edition that is not installed
cannot be composed. `load_worker_edition` returns None and the caller fails
closed — it never falls back to a weaker composition.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

WORKER_EDITION_GROUP = "frisket.worker_editions"

# An externally-composed edition (funding settlement + hosted code-execution
# admission + spend-capped credentials). Registered by an external package.
CLOUD_EDITION = "cloud"


def load_worker_edition(name: str) -> Callable[..., Any] | None:
    """The registered composition for `name`, or None when it is not installed."""
    from importlib.metadata import entry_points

    for entry_point in entry_points(group=WORKER_EDITION_GROUP):
        if entry_point.name == name:
            return entry_point.load()
    return None


def available_worker_editions() -> list[str]:
    """Every worker edition registered in this install (diagnostics)."""
    from importlib.metadata import entry_points

    return sorted(
        entry_point.name for entry_point in entry_points(group=WORKER_EDITION_GROUP)
    )
