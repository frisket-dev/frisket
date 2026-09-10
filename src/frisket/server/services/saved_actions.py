"""Saved-action contracts shared by local storage and action artifacts."""

from __future__ import annotations

from typing import Any

from frisket.authoring.action_metadata import canonical_action_kind
from frisket.authoring.recipe_registry import REMOVED_ACTION_KEYS


class SavedActionSpecError(ValueError):
    """A saved-action write did not carry the post-reset v1 dialect."""


def require_saved_action_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Require the post-reset snake_case saved-action wire contract."""

    forbidden = sorted(REMOVED_ACTION_KEYS.intersection(spec))
    if forbidden:
        raise SavedActionSpecError(
            "saved action spec uses removed keys: " + ", ".join(forbidden)
        )
    action_kind = spec.get("action_kind")
    if not isinstance(action_kind, str) or not action_kind.strip():
        raise SavedActionSpecError("saved action spec requires a nonblank action_kind")
    if "." not in action_kind or canonical_action_kind(action_kind) != action_kind:
        raise SavedActionSpecError(f"action_kind '{action_kind}' is not canonical")
    return dict(spec)


def saved_action_template(entry: dict[str, Any]) -> dict[str, Any]:
    spec = require_saved_action_spec(entry.get("spec") or {})
    return {
        "schema_version": "frisket.saved_action_template.v1",
        "id": entry.get("id"),
        "name": entry.get("name", ""),
        "action_kind": spec.get("action_kind", "custom"),
        "action_name": spec.get("action_name", spec.get("action_kind", "custom")),
        "spec": spec,
    }
