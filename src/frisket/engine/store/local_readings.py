"""Receipts for page readings an import does on this machine.

A reading on this machine costs nothing and sends nothing anywhere, so it
has no provider receipt of its own. Each import pass records its readings
in one receipt instead: the engine and settings, and every page it read.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue

from frisket.contracts.action import ActionResultStatus, Receipt, ReceiptIO
from frisket.engine.store.receipts import ReceiptStore


def local_reading_receipt_id(kind: str, *identity: object) -> str:
    """The one receipt id a pass's readings of one kind are recorded under."""
    named = json.dumps([kind, *identity], separators=(",", ":"), default=str)
    return "receipt_" + hashlib.sha256(f"local-reading:{named}".encode()).hexdigest()


def local_reading_status(statuses: Iterable[str]) -> ActionResultStatus:
    """A pass's status from its pages': a pass nobody finished is cancelled."""
    found = set(statuses)
    if found == {"completed"}:
        return "completed"
    if "completed" in found:
        return "partial"
    return "failed" if "failed" in found else "cancelled"


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalReading:
    """What one import pass read on this machine, and how."""

    action_kind: str
    engine: str
    service: str
    settings: Mapping[str, JsonValue]
    pages: Sequence[JsonValue]
    status: ActionResultStatus
    change_ids: Sequence[int] = ()


def record_local_reading(
    project: Any,
    reading: LocalReading,
    *,
    receipt_id: str,
    project_id: str,
    import_receipt_id: str,
) -> None:
    """Record once that an import read these pages here, at no cost."""
    receipts = ReceiptStore(project)
    if receipts.find_by_id(receipt_id) is not None:
        return
    receipts.insert_finished(
        Receipt(
            receipt_id=receipt_id,
            project_id=project_id,
            action_id="act_" + receipt_id.removeprefix("receipt_")[:24],
            action_kind=reading.action_kind,
            change_ids=list(reading.change_ids),
            status=reading.status,
            params_hash=hashlib.sha256(
                json.dumps(dict(reading.settings), sort_keys=True).encode()
            ).hexdigest(),
            inputs=[
                ReceiptIO(
                    name="import",
                    ref={"kind": "import", "receipt_id": import_receipt_id},
                )
            ],
            provider_use=[
                {
                    "service": reading.service,
                    "provider": "local",
                    "engine": reading.engine,
                    "credential_source": "local",
                    "external_api": False,
                    "cost_actual": 0.0,
                    "model_call_count": len(reading.pages),
                    "request_count": 0,
                }
            ],
            value={"settings": dict(reading.settings), "pages": list(reading.pages)},
        )
    )


__all__ = [
    "LocalReading",
    "local_reading_receipt_id",
    "local_reading_status",
    "record_local_reading",
]
