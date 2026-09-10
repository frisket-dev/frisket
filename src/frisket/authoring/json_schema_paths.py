"""Pure schema projection shared by typed authoring and recordset consumers."""

from __future__ import annotations

import copy
from typing import Any


def named_result_schema_for_path(
    schema: dict[str, Any], path: str
) -> dict[str, Any] | None:
    if path == "$":
        return copy.deepcopy(schema)
    if not path.startswith("$."):
        return None
    current: Any = schema
    for part in path[2:].split("."):
        if not isinstance(current, dict):
            return None
        if current.get("type") == "array":
            current = current.get("items")
        if not isinstance(current, dict):
            return None
        properties = current.get("properties")
        if not isinstance(properties, dict):
            return None
        current = properties.get(part)
        if not isinstance(current, dict):
            return None
    return copy.deepcopy(current)
