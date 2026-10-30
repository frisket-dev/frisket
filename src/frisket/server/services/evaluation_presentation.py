"""Bound display values while keeping authoritative score/storage/export inputs exact."""

from __future__ import annotations

from copy import deepcopy

from frisket.server.exports.sheet_csv import csv_cell
from frisket.server.services.evaluation_values import value_hash

CELL_BYTE_LIMIT = 64 * 1024
DISPLAY_BYTE_BUDGET = 512 * 1024


def bound(result):
    result = deepcopy(result)
    remaining = DISPLAY_BYTE_BUDGET

    def present(value):
        nonlocal remaining
        text = csv_cell(value)
        size = len(text.encode("utf-8"))
        if size <= CELL_BYTE_LIMIT and size <= remaining:
            remaining -= size
            return None
        preview = text[: min(2048, max(0, remaining // 4))]
        remaining -= len(preview.encode("utf-8"))
        return {
            "preview": preview,
            "total_characters": len(text),
            "total_bytes": size,
            "truncated": True,
        }

    if "expected" in result:
        result["expected_bounded"] = {}
        for row_id, fields in result["expected"].items():
            for key, value in fields.items():
                metadata = present(value)
                if metadata:
                    result["expected_bounded"].setdefault(row_id, {})[key] = metadata
                    fields[key] = None
    variants = result.get("variants", [result])
    for variant in variants:
        for fields in variant["rows"].values():
            for cell in fields.values():
                if cell["state"] not in {"value", "null"}:
                    continue
                metadata = present(cell.get("value"))
                if metadata:
                    cell["value_hash"] = value_hash(cell.get("value"))
                    cell["value"] = None
                    cell["bounded_value"] = metadata
    return result
