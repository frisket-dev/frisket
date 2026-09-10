from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .structured import (
    StructuredCompleter,
    StructuredRequest,
    UnsupportedMechanismError,
)
from .structured_capabilities import JSON_LOOSE, CapabilityRow
from .types import LLMError, SchemaViolation

# A trivial, fixed schema -- the probe's only question is "did this wire
# mechanism work at all", not "is this model smart".
PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
PROBE_MESSAGES: list[dict[str, Any]] = [
    {"role": "user", "content": 'Respond with exactly: {"ok": true}'}
]

_PROBE_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS structured_capability_probes (
  model TEXT NOT NULL,
  mechanism TEXT NOT NULL,
  supported INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  probed_at TEXT NOT NULL,
  PRIMARY KEY (model, mechanism)
);
"""


@dataclass(frozen=True)
class ProbeRecord:
    model: str
    mechanism: str
    supported: bool
    note: str
    probed_at: str

    def as_capability_row(self) -> CapabilityRow:
        """A recorded probe -> a :class:`CapabilityRow` keyed on the LITERAL
        model id (no wildcards) -- specificity-wins any glob in
        ``structured_capabilities.resolve()`` for free: its resolution order
        already ranks a full literal match above every glob (max
        non-wildcard segments + max literal-prefix length -- no change to
        that algorithm needed, "the capability table's resolution order
        already reserves the slot"). An UNSUPPORTED probe still becomes a
        row (the standing conservative default, ``JSON_LOOSE``) so a repeat
        ``resolve()`` records definite negative knowledge instead of falling
        through to silence."""
        return CapabilityRow(
            provider=self.model.split("/", 1)[0],
            model_pattern=self.model,
            structured_mode=self.mechanism if self.supported else JSON_LOOSE,
            additional_properties_false=False,
            nullable_optional_convention=False,
            source=f"probe:{self.model}:{self.mechanism}:{self.probed_at}"
            + (f" ({self.note})" if self.note else ""),
        )


class ProbeStore:
    """Sidecar sqlite store for recorded probe results -- mirrors
    ``llm/cache.py::ResponseCache``'s pattern (a rebuildable sidecar db) but
    is its OWN file/table, deliberately separate from ``project.cache.db``:
    a probe fact is about a MODEL's wire capability (provider-scoped, not
    project-scoped -- "openrouter/openai/gpt-5 accepts native_strict" is true
    for every project that ever calls it), whereas the response cache is
    keyed per-request per-project. Sharing one table would conflate two
    different rebuildable-sidecar lifetimes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(_PROBE_TABLE_SCHEMA)

    def get(self, model: str, mechanism: str) -> ProbeRecord | None:
        row = self.db.execute(
            "SELECT model, mechanism, supported, note, probed_at "
            "FROM structured_capability_probes WHERE model=? AND mechanism=?",
            (model, mechanism),
        ).fetchone()
        if row is None:
            return None
        return ProbeRecord(row[0], row[1], bool(row[2]), row[3], row[4])

    def all_for_model(self, model: str) -> list[ProbeRecord]:
        rows = self.db.execute(
            "SELECT model, mechanism, supported, note, probed_at "
            "FROM structured_capability_probes WHERE model=?",
            (model,),
        ).fetchall()
        return [ProbeRecord(r[0], r[1], bool(r[2]), r[3], r[4]) for r in rows]

    def record(
        self, model: str, mechanism: str, supported: bool, note: str = ""
    ) -> ProbeRecord:
        probed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.db.execute(
            "INSERT INTO structured_capability_probes "
            "(model, mechanism, supported, note, probed_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(model, mechanism) DO UPDATE SET "
            "supported=excluded.supported, note=excluded.note, "
            "probed_at=excluded.probed_at",
            (model, mechanism, int(supported), note, probed_at),
        )
        self.db.commit()
        return ProbeRecord(model, mechanism, supported, note, probed_at)

    def close(self) -> None:
        self.db.close()


async def probe_mechanism(
    router: Any,
    model: str,
    mechanism: str,
    *,
    store: ProbeStore,
    note: str = "",
) -> ProbeRecord:
    """THE opt-in probe. Sends exactly ONE trivial
    known-schema request under ``mechanism`` -- an explicit
    ``StructuredRequest(method=mechanism, repair_attempts=0)`` (fail-hard: a
    probe wants a clean "did this mechanism work" signal, not "how many
    corrective turns did it take to coax a valid answer") -- through a
    FRESH, probe-store-less :class:`~llm.structured.StructuredCompleter` (so
    the probe itself is never influenced by a prior probe's recording),
    records supported/unsupported to ``store``, and returns. NEVER retries
    with a different mechanism on failure -- one candidate mechanism per
    call, by design (record-don't-experiment: a failed probe records
    ``unsupported``, i.e. the conservative json_loose fallback stays in
    force, not an automatic cascade through the other three)."""
    req = StructuredRequest(
        model=model,
        messages=list(PROBE_MESSAGES),
        schema=PROBE_SCHEMA,
        method=mechanism,
        repair_attempts=0,
    )
    try:
        await StructuredCompleter(router).complete(req)
    except (SchemaViolation, LLMError, UnsupportedMechanismError) as e:
        return store.record(
            model, mechanism, supported=False, note=note or str(e)[:300]
        )
    return store.record(model, mechanism, supported=True, note=note)
