"""Public review payload projection helpers."""

from __future__ import annotations

from typing import Any

from frisket.authoring.action_metadata import action_metadata_for_action_kind


def _public_review_action_payload(
    item: dict[str, Any], action_metadata: dict[str, str]
) -> dict[str, Any]:
    payload = dict(item)
    payload.pop("action_kind", None)
    payload["action_kind"] = action_metadata["action_kind"]
    payload["action_name"] = action_metadata["action_name"]
    return payload


def public_review_action_payload(item: dict[str, Any]) -> dict[str, Any]:
    return _public_review_action_payload(
        item, action_metadata_for_action_kind(item.get("action_kind"))
    )


def public_review_action_payloads(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    metadata_by_kind: dict[str, dict[str, str]] = {}
    for item in items:
        raw_kind = str(item.get("action_kind") or "").strip()
        if raw_kind not in metadata_by_kind:
            metadata_by_kind[raw_kind] = action_metadata_for_action_kind(raw_kind)
    return [
        _public_review_action_payload(
            item,
            metadata_by_kind[str(item.get("action_kind") or "").strip()],
        )
        for item in items
    ]
