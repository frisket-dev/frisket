"""Unit tests for the shared media receipt/replay helpers in `frisket/sdk/media.py`."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from frisket.contracts.action import ActionError
from frisket.sdk.media import (
    media_output_column_value_hash,
    media_replay_row_ids,
    media_successful_result_row_ids,
    media_text_hash,
)


# ---------------------------------------------------------------- hash/replay


def test_media_text_hash_is_prefixed_sha256() -> None:
    text = "https://example.com/audio.mp3"
    expected = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert media_text_hash(text) == expected


class _FakeValuesProject:
    def __init__(self, values: dict[int, Any]) -> None:
        self._values = values

    def get_values(
        self, sheet_id: int, column_id: int, *, row_ids: list[int]
    ) -> dict[int, Any]:
        return {row_id: self._values.get(row_id) for row_id in row_ids}


def test_media_generated_column_value_hash_deterministic_and_value_sensitive() -> None:
    base = _FakeValuesProject({1: "a", 2: {"blob": "sha256:x"}})
    first = media_output_column_value_hash(
        base, sheet_id=1, column_id=2, row_ids=[1, 2]
    )
    second = media_output_column_value_hash(
        base, sheet_id=1, column_id=2, row_ids=[1, 2]
    )
    assert first == second
    changed = _FakeValuesProject({1: "a", 2: {"blob": "sha256:y"}})
    assert (
        media_output_column_value_hash(changed, sheet_id=1, column_id=2, row_ids=[1, 2])
        != first
    )


@pytest.mark.parametrize(
    "row_ids",
    [
        None,  # missing
        [],  # empty
        [1, "2"],  # non-int
        [1, True],  # bool masquerading as int
        [1, 0],  # non-positive
        [1, -2],  # negative
        [1, 1],  # duplicate
    ],
)
def test_media_replay_row_ids_rejects_invalid_refs(row_ids: Any) -> None:
    receipt = SimpleNamespace(action_kind="media.video_frames", receipt_id="r-1")
    ref: dict[str, Any] = {} if row_ids is None else {"row_ids": row_ids}
    result = media_replay_row_ids(ref, receipt=receipt, output_name="frames")
    assert isinstance(result, ActionError)
    assert result.code == "stale_replay"
    assert result.action_kind == "media.video_frames"
    assert result.details == {"receipt_id": "r-1", "output": "frames"}


def test_media_replay_row_ids_passes_valid_refs_through() -> None:
    receipt = SimpleNamespace(action_kind="media.extract_faces", receipt_id="r-2")
    assert media_replay_row_ids(
        {"row_ids": [3, 1, 2]}, receipt=receipt, output_name="faces"
    ) == [3, 1, 2]


# ---------------------------------------------------------------- result rows


class _FakeDbProject:
    """Just enough of Project for media_successful_result_row_ids."""

    def __init__(self, successful_row_ids: list[int]) -> None:
        self._successful = successful_row_ids
        self.db = self
        self.executed: list[tuple[str, list[Any]]] = []

    def execute(self, sql: str, params: list[Any]) -> "_FakeDbProject":
        self.executed.append((sql, params))
        return self

    def fetchall(self) -> list[dict[str, int]]:
        return [{"row_id": row_id} for row_id in self._successful]


def test_media_successful_result_row_ids_preserves_input_order() -> None:
    project = _FakeDbProject(successful_row_ids=[2, 5])
    result = media_successful_result_row_ids(
        project, run_id=7, column_id=9, row_ids=[5, 3, 2]
    )
    assert result == [5, 2]
    sql, params = project.executed[0]
    assert (
        "outcome NOT IN ('model_error', 'invalid_output', 'row_error', 'not_completed')"
        in sql
    )
    assert params == [7, 9, 5, 3, 2]


def test_media_successful_result_row_ids_empty_input_short_circuits() -> None:
    project = _FakeDbProject(successful_row_ids=[])
    assert (
        media_successful_result_row_ids(project, run_id=1, column_id=1, row_ids=[])
        == []
    )
    assert project.executed == []
