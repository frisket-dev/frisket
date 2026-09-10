from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from frisket.contracts.action import ReceiptIO


@dataclass
class Provenance:
    """The receipt parts an op presents from facts. `output_refs` feed both the
    receipt outputs and the host-built ActionOutputs.

    `named_result_refs` is the typed "list column -> child table" seam: array-typed
    output columns advertise a feedable `named_result` ref (built via
    `frisket.executor.recordsets.feedable_named_result_ref`) that may feed
    derive.table_from_list. They appear as extra receipt outputs (keyed by `route`)
    and as extra `named_result` ActionOutputs, alongside the column outputs. A general
    axis (map.ner, map.extract); the host projects them so no op needs a write hook
    just to emit them. Empty for ops that produce no list columns."""

    inputs: list[ReceiptIO]
    output_refs: list[dict[str, Any]]
    provider_use: list[dict[str, Any]]
    evidence: list[Any]  # list[ReceiptEvidence]
    status: str
    errors: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    named_result_refs: list[dict[str, Any]] = field(default_factory=list)
