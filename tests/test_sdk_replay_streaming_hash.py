from __future__ import annotations

import hashlib
import json
from typing import Any

from frisket.sdk.replay import (
    output_column_result_value_hash,
    output_column_value_hash,
)


class _PagedProject:
    def __init__(self, values: dict[int, Any]) -> None:
        self.values = values
        self.calls: list[tuple[list[int], bool]] = []

    def get_values(
        self,
        _sheet_id: int,
        _column_id: int,
        *,
        row_ids: list[int],
        apply_edits: bool = True,
    ) -> dict[int, Any]:
        self.calls.append((list(row_ids), apply_edits))
        return {row_id: self.values.get(row_id) for row_id in row_ids}


def _legacy_hash(values: dict[int, Any], row_ids: list[int]) -> str:
    ordered = [
        {"row_id": int(row_id), "value": values.get(int(row_id))} for row_id in row_ids
    ]
    encoded = json.dumps(
        ordered, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_output_value_hash_is_byte_compatible_and_page_bounded() -> None:
    row_ids = list(range(1, 10_001))
    values = {
        row_id: {"details": "x" * 1024, "nested": [row_id, None]} for row_id in row_ids
    }
    project = _PagedProject(values)

    actual = output_column_value_hash(project, sheet_id=1, column_id=2, row_ids=row_ids)

    assert actual == _legacy_hash(values, row_ids)
    assert max(len(page) for page, _apply_edits in project.calls) == 128
    assert all(apply_edits for _page, apply_edits in project.calls)


def test_result_value_hash_uses_the_same_streaming_bytes_without_edits() -> None:
    row_ids = [3, 1, 3]
    values = {1: "one", 3: {"value": "three"}}
    project = _PagedProject(values)

    actual = output_column_result_value_hash(
        project, sheet_id=1, column_id=2, row_ids=row_ids
    )

    assert actual == _legacy_hash(values, row_ids)
    assert project.calls == [(row_ids, False)]
