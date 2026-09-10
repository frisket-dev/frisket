"""Shared error envelopes for action-family executors."""

from __future__ import annotations

from typing import Any

from frisket.contracts.action import ActionError, Receipt


def receipt_stale_replay_error(
    receipt: Receipt,
    message: str,
    *,
    field: str | None = None,
    details: dict[str, Any] | None = None,
    include_receipt_id: bool = True,
    **context: Any,
) -> ActionError:
    """Build the invariant stale-replay envelope for a receipt."""
    merged_details = {"receipt_id": receipt.receipt_id} if include_receipt_id else {}
    merged_details.update(details or {})
    merged_details.update(context)
    return ActionError(
        code="stale_replay",
        message=message,
        action_kind=receipt.action_kind,
        field=field,
        details=merged_details,
    )
