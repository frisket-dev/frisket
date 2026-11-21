"""Model-call order is recorded only by ``(created_at, rowid)``.

``created_at`` has one-second resolution and call ids are random, so calls
recorded in the same second are ordered by the table's implicit rowid. These
pin that ``Project.compact``'s VACUUM keeps that order, read back through the
production reader after a reopen. Ids sort opposite to insertion and deleted
rows leave rowid gaps, so an id-ordered or gap-renumbering copy goes red.
"""

from __future__ import annotations
from contextlib import closing

from pathlib import Path
from typing import Any

from frisket.ai.models.metadata import ModelCallMeta
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore

_CALL_COUNT = 30


def _fact(call_id: str) -> dict[str, Any]:
    fact = ModelCallMeta.provider_call(
        capability="llm.complete",
        engine="mock/model-a",
        provider="mock",
        provider_kind="chat_api",
        model_ids=["model-a"],
        credential_source="platform_key",
        provider_reported_cost_usd=0.001,
        provider_cost_usd=0.001,
        cost_source="pricing_data",
        units={"tokens_in": 10, "tokens_out": 2},
        warnings=[],
        duration_ms=None,
    ).as_dict()
    fact["id"] = call_id
    return fact


def _record_calls_in_one_second(project: Project) -> list[str]:
    recorded = [f"call-{_CALL_COUNT - index:03d}" for index in range(_CALL_COUNT)]
    store = RunResultStore(project)
    for call_id in recorded:
        store.write_unscoped_model_calls([_fact(call_id)], row_id=None, column_id=None)
    project.db.execute("UPDATE model_calls SET created_at='2026-01-01 00:00:00'")
    removed = recorded[::3]
    project.db.executemany(
        "DELETE FROM model_calls WHERE id=?", [(call_id,) for call_id in removed]
    )
    project.db.commit()
    kept = [call_id for call_id in recorded if call_id not in removed]
    assert [row["id"] for row in store.model_calls()] == kept
    assert sorted(kept) != kept
    return kept


def test_compaction_vacuum_keeps_model_call_order(tmp_path: Path) -> None:
    path = tmp_path / "order.frisket"
    project = Project.create(path, name="Order")
    kept = _record_calls_in_one_second(project)

    summary = project.compact(force=True)
    assert summary["skipped"] is False
    project.close()

    with closing(Project(path)) as reopened:
        assert [row["id"] for row in RunResultStore(reopened).model_calls()] == kept
