from __future__ import annotations

import hashlib
import json
from typing import Any

from frisket.sdk.replay import (
    generated_column_result_value_hash,
    generated_column_value_hash,
)


class _PagedProject:
    def __init__(self, values: dict[int, Any]) -> None:
        self.values = values
        self.calls: list[list[int]] = []

    def get_values(
        self,
        _sheet_id: int,
        _column_id: int,
        *,
        row_ids: list[int],
    ) -> dict[int, Any]:
        self.calls.append(list(row_ids))
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

    actual = generated_column_value_hash(
        project, sheet_id=1, column_id=2, row_ids=row_ids
    )

    assert actual == _legacy_hash(values, row_ids)
    assert max(len(page) for page in project.calls) == 128


def test_saved_result_hash_is_page_bounded_and_byte_compatible(monkeypatch) -> None:
    from types import SimpleNamespace
    from frisket.engine.store.runs import RunResultStore

    row_ids = [3, 1, 3] * 100
    values = {1: "one", 3: "three"}
    coordinates_seen = []

    def saved(self, coordinates):
        coordinates_seen.append(coordinates)
        return {
            key: {"value": values[key[1]], "publication_effect": "publish_value"}
            for key in coordinates
        }

    monkeypatch.setattr(RunResultStore, "decoded_result_rows", saved)
    project = SimpleNamespace(
        db=SimpleNamespace(
            execute=lambda *args: SimpleNamespace(fetchone=lambda: {"type": "text"})
        )
    )
    actual = generated_column_result_value_hash(
        project, run_id=7, sheet_id=1, column_id=2, row_ids=row_ids
    )
    assert actual == _legacy_hash(values, row_ids)
    assert max(map(len, coordinates_seen)) == 128
    assert all(
        run == 7 and column == 2 for page in coordinates_seen for run, _, column in page
    )
