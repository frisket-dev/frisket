"""Public review payload projection helpers."""

from __future__ import annotations

from typing import Any

from frisket.authoring.action_metadata import action_metadata_for_action_kind


def public_review_action_payload(item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item)
    action_metadata = action_metadata_for_action_kind(payload.pop("action_kind", None))
    payload["action_kind"] = action_metadata["action_kind"]
    payload["action_name"] = action_metadata["action_name"]
    return payload
