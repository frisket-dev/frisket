from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from frisket.engine.executor.action_specs import ResolvedAction
# --- resolved is threaded into the receipt builder / write skeleton --------


def test_build_receipt_and_outputs_accepts_resolved() -> None:
    from frisket.sdk.maprunner import build_receipt_and_outputs

    params = inspect.signature(build_receipt_and_outputs).parameters
    assert "resolved" in params, (
        "build_receipt_and_outputs must accept resolved so present() can see "
        "resolved input facts"
    )


# --- ResolvedAction.snapshot is JSON-only, deterministic, redacted ---------


def test_resolved_action_snapshot_is_json_and_deterministic() -> None:
    resolved = ResolvedAction(
        sheet_id=3,
        row_ids=(2, 1, 3),
        input_columns={"b": {"id": 2}, "a": {"id": 1}},
        facts={"note": "ok"},
    )
    snap1 = resolved.snapshot()
    snap2 = resolved.snapshot()
    assert snap1 == snap2
    import json

    # round-trips as JSON with stable key order
    assert json.loads(json.dumps(snap1, sort_keys=True)) == snap1
    assert snap1["sheet_id"] == 3
    assert snap1["row_ids"] == [2, 1, 3]


def test_resolved_action_snapshot_rejects_unserializable_facts() -> None:
    resolved = ResolvedAction(facts={"path": Path("/tmp/x")})
    with pytest.raises(TypeError):
        resolved.snapshot()

    resolved_bytes = ResolvedAction(facts={"blob": b"\x00\x01"})
    with pytest.raises(TypeError):
        resolved_bytes.snapshot()


def test_from_resolve_dict_keeps_full_facts_and_coerces_defensively() -> None:
    # The full resolve dict is preserved verbatim in .facts (writers read it);
    # the lifted typed fields coerce defensively and never crash on stray values.
    resolved = ResolvedAction.from_resolve_dict(
        {
            "input_column_ids": {"a": 1},
            "row_ids": [3, "4", None, True, "x"],
            "sheet_id": "7",
            "blob_refs": [{"blob": "sha256:z"}],
        }
    )
    # facts is the untouched dict
    assert resolved.facts["input_column_ids"] == {"a": 1}
    assert resolved.facts["row_ids"] == [3, "4", None, True, "x"]
    assert resolved.facts["blob_refs"] == [{"blob": "sha256:z"}]
    # lifted typed fields: "4" -> 4, None/True(bool)/"x" dropped; sheet_id "7" -> 7
    assert resolved.row_ids == (3, 4)
    assert resolved.sheet_id == 7


def test_from_resolve_dict_handles_missing_and_bad_typed_fields() -> None:
    r = ResolvedAction.from_resolve_dict({"input_column_ids": {}})
    assert r.row_ids == ()
    assert r.sheet_id is None
    # a bool sheet_id is not treated as an int
    assert ResolvedAction.from_resolve_dict({"sheet_id": True}).sheet_id is None


def test_resolved_action_snapshot_rejects_non_finite_floats() -> None:
    # NaN/Infinity are not valid JSON; strict dumps must reject them so a queued
    # snapshot can never carry a non-standard float.
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            ResolvedAction(facts={"x": bad}).snapshot()
